#!/usr/bin/env python3
"""CGV 예매 오픈 감시기 — 용산아이파크몰 IMAX '오디세이'

동작 원리:
  1. 주기적으로 CGV 공개 API에서 극장의 '예매 가능 날짜 목록'을 조회 (사이클당 1회)
  2. 새로 열린 날짜가 있으면 그 날짜의 상영 스케줄을 조회
  3. 관심 영화 + 관심 관 조건에 맞는 새 회차가 있으면 디스코드 웹훅으로 알림

IP 차단 방지 설계 (회사망 보호):
  - 기본 10분 간격 폴링 + 랜덤 지터
  - 평상시 사이클당 요청 1개 (날짜 목록만), 새 날짜가 있을 때만 추가 조회
  - 스케줄 요청 사이 2.5~4.5초 간격, 사이클당 요청 수 상한
  - 하루 총 요청 수 상한 (초과 시 자정까지 자동 휴지)
  - HTTP 403(차단 신호) 감지 시 2시간 자동 휴지 + 디스코드 경고
  - 실패 시 재시도 없음 (다음 사이클로 넘어감), 연속 실패 시 간격 자동 확대
"""
import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from curl_cffi import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "cgv_watch.log"

API_BASE = "https://cgv.co.kr/api/v1/booking"
BOOKING_PAGE = "https://cgv.co.kr/cnm/movieBook/cinema"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}

log = logging.getLogger("cgv_watch")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def default_state():
    return {
        "baseline_done": False,
        "known_dates": [],      # 이미 인지한 예매 가능 날짜
        "checked_dates": [],    # 스케줄을 한 번이라도 조회한 날짜
        "seen_keys": [],        # 이미 알림/기록한 회차 키 (날짜|시각|영화|관)
        "cycle": 0,
        "consecutive_failures": 0,
        "request_count_date": "",
        "request_count": 0,
        "block_warned": False,
    }


class BlockedError(Exception):
    """403 등 차단으로 의심되는 응답."""


class DailyCapReached(Exception):
    """하루 요청 상한 도달."""


class Watcher:
    def __init__(self, config):
        self.cfg = config
        self.state = load_json(STATE_PATH, default_state())

    # ---------- HTTP ----------
    def _count_request(self):
        today = date.today().isoformat()
        if self.state["request_count_date"] != today:
            self.state["request_count_date"] = today
            self.state["request_count"] = 0
        if self.state["request_count"] >= self.cfg["daily_request_cap"]:
            raise DailyCapReached()
        self.state["request_count"] += 1

    def api_get(self, path, params):
        self._count_request()
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API_BASE}/{path}?{qs}"
        r = requests.get(url, headers=HEADERS, impersonate="chrome", timeout=20)
        if r.status_code == 403:
            raise BlockedError(f"HTTP 403 from {path}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} from {path}")
        body = r.json()
        if body.get("statusCode") != 0:
            raise RuntimeError(f"API error {body.get('statusCode')}: {body.get('statusMessage')}")
        return body["data"]

    def get_open_dates(self):
        data = self.api_get("searchSiteScnscYmdListBySite",
                            {"coCd": "A420", "siteNo": self.cfg["site_no"]})
        return [d["scnYmd"] for d in (data or [])]

    def get_schedule(self, ymd):
        data = self.api_get("searchMovScnInfo",
                            {"coCd": "A420", "siteNo": self.cfg["site_no"],
                             "scnYmd": ymd, "rtctlScopCd": "08"})
        return data or []

    # ---------- 필터 ----------
    def match(self, showing):
        movie = (showing.get("expoProdNm") or "") + " " + (showing.get("movNm") or "")
        screen = showing.get("scnsNm") or ""
        movie_ok = any(k in movie for k in self.cfg["movie_keywords"])
        screen_ok = any(k in screen for k in self.cfg["screen_keywords"])
        return movie_ok and screen_ok

    @staticmethod
    def showing_key(s):
        return f'{s["scnYmd"]}|{s.get("scnsrtTm", "")}|{s.get("prodNo", "")}|{s.get("scnsNo", "")}'

    # ---------- 디스코드 ----------
    def send_discord(self, content):
        # 우선순위: 환경변수(DISCORD_WEBHOOK_URL, GitHub Actions 시크릿용) > config.json
        webhook = os.environ.get("DISCORD_WEBHOOK_URL") or self.cfg.get("discord_webhook_url", "")
        content = content[:1900]
        if not webhook:
            log.info("[웹훅 미설정] 보낼 메시지:\n%s", content)
            return
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "cgv-watch/1.0"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            log.info("디스코드 알림 전송 완료")
        except Exception as e:
            log.error("디스코드 전송 실패: %s", e)

    @staticmethod
    def fmt_time(t):
        t = (t or "").zfill(4)
        return f"{t[:2]}:{t[2:]}"

    @staticmethod
    def fmt_date(ymd):
        d = datetime.strptime(ymd, "%Y%m%d")
        return d.strftime("%Y-%m-%d(%a)")

    def format_alert(self, showings, header):
        lines = [header]
        by_date = {}
        for s in showings:
            by_date.setdefault(s["scnYmd"], []).append(s)
        for ymd in sorted(by_date):
            lines.append(f"\n📅 **{self.fmt_date(ymd)}**")
            for s in sorted(by_date[ymd], key=lambda x: x.get("scnsrtTm", "")):
                seats = s.get("frSeatCnt")
                seat_txt = f" (잔여 {seats}석)" if seats not in (None, "") else ""
                lines.append(f"  · {self.fmt_time(s.get('scnsrtTm'))} {s.get('expoProdNm')} — {s.get('scnsNm')}{seat_txt}")
        lines.append(f"\n🎟️ 예매: {BOOKING_PAGE}")
        return "\n".join(lines)

    # ---------- 사이클 ----------
    def prune_past(self, dates_now):
        today = date.today().strftime("%Y%m%d")
        self.state["known_dates"] = [d for d in self.state["known_dates"] if d >= today and d in dates_now]
        self.state["checked_dates"] = [d for d in self.state["checked_dates"] if d >= today]
        self.state["seen_keys"] = [k for k in self.state["seen_keys"] if k.split("|", 1)[0] >= today]

    def run_cycle(self):
        st = self.state
        st["cycle"] += 1
        dates = self.get_open_dates()
        log.info("사이클 %d: 예매 가능 날짜 %d개 (오늘 요청 %d개째)", st["cycle"], len(dates), st["request_count"])

        new_dates = [d for d in dates if d not in st["known_dates"]]
        to_check = list(new_dates)

        # 정기 스윕: 아직 조회 못 한 날짜를 주기적으로 확인 (관 추가 편성 대비)
        if st["baseline_done"] and st["cycle"] % self.cfg["sweep_every_n_cycles"] == 0:
            to_check += [d for d in dates if d not in st["checked_dates"] and d not in to_check]
        if not st["baseline_done"]:
            to_check = list(dates)

        cap = self.cfg["max_schedule_requests_per_cycle"]
        deferred = to_check[cap:]
        to_check = to_check[:cap]
        if deferred:
            log.info("요청 상한으로 %d개 날짜는 다음 사이클로 미룸", len(deferred))

        alerts = []
        baseline_records = []
        for i, ymd in enumerate(to_check):
            if i > 0:
                time.sleep(2.5 + random.random() * 2)
            schedule = self.get_schedule(ymd)
            targets = [s for s in schedule if self.match(s)]
            first_time_checked = ymd not in st["checked_dates"]
            is_new_date = ymd in new_dates
            for s in targets:
                key = self.showing_key(s)
                if key in st["seen_keys"]:
                    continue
                st["seen_keys"].append(key)
                if not st["baseline_done"]:
                    baseline_records.append(s)
                elif is_new_date or not first_time_checked:
                    alerts.append(s)   # 새로 열린 날짜이거나, 기존 날짜에 회차 추가 → 알림
                else:
                    baseline_records.append(s)  # 옛 날짜 첫 조회 → 조용히 기록만
            if ymd not in st["checked_dates"]:
                st["checked_dates"].append(ymd)
            if targets:
                log.info("  %s: 대상 회차 %d개", ymd, len(targets))

        # known_dates 갱신
        if not st["baseline_done"]:
            # 베이스라인: 지금 열려 있는 날짜는 전부 '기존 날짜' — 미확인분은 스윕이 조용히 채움
            st["known_dates"] = list(dates)
        else:
            # 확인 완료한 새 날짜만 추가 (상한으로 미룬 새 날짜는 다음 사이클에 다시 '새 날짜'로 인식)
            st["known_dates"] = [d for d in st["known_dates"] if d in dates] + \
                                [d for d in new_dates if d in to_check]

        if alerts:
            movie = ", ".join(self.cfg["movie_keywords"])
            msg = self.format_alert(
                alerts, f"🚨 **예매 오픈 알림** — CGV {self.cfg['site_name']} / {movie}")
            self.send_discord(msg)

        if not st["baseline_done"]:
            st["baseline_done"] = True
            header = (f"✅ **감시 시작** — CGV {self.cfg['site_name']} / "
                      f"{', '.join(self.cfg['movie_keywords'])} ({', '.join(self.cfg['screen_keywords'])})\n"
                      f"현재 예매 가능한 회차:")
            msg = self.format_alert(baseline_records, header) if baseline_records \
                else header + "\n(아직 없음 — 오픈되면 알려드릴게요)"
            self.send_discord(msg)

        self.prune_past(dates)
        st["consecutive_failures"] = 0
        st["block_warned"] = False
        self.save()

    def save(self):
        STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 메인 루프 ----------
    def loop(self, once=False):
        interval = self.cfg["poll_interval_sec"]
        while True:
            sleep_sec = interval + random.random() * 90
            try:
                self.run_cycle()
            except BlockedError as e:
                log.error("차단 의심 응답: %s — 2시간 휴지", e)
                if not self.state["block_warned"]:
                    self.send_discord("⚠️ CGV가 요청을 차단했어요(403). 2시간 쉬었다가 재시도합니다. "
                                      "반복되면 스크립트를 멈추고 집에서 돌리는 걸 권장해요.")
                    self.state["block_warned"] = True
                self.state["consecutive_failures"] += 1
                self.save()
                sleep_sec = 2 * 3600
            except DailyCapReached:
                log.warning("하루 요청 상한(%d) 도달 — 자정까지 휴지", self.cfg["daily_request_cap"])
                now = datetime.now()
                sleep_sec = (24 - now.hour) * 3600 - now.minute * 60 + 120
            except Exception as e:
                self.state["consecutive_failures"] += 1
                n = self.state["consecutive_failures"]
                self.save()
                log.error("사이클 실패(%d연속): %s", n, e)
                if n >= 3:
                    sleep_sec = min(interval * 2 ** (n - 2), 2 * 3600)
                    if n == 3:
                        self.send_discord(f"⚠️ CGV 조회가 3회 연속 실패했어요: {e}\n간격을 늘려서 계속 시도합니다.")
            if once:
                break
            log.info("다음 사이클까지 %d초 대기", int(sleep_sec))
            time.sleep(sleep_sec)


def main():
    parser = argparse.ArgumentParser(description="CGV 예매 오픈 감시기")
    parser.add_argument("--once", action="store_true", help="1사이클만 실행하고 종료")
    args = parser.parse_args()

    setup_logging()
    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("config.json이 없습니다. config.example.json을 복사해서 만들어주세요.")
        sys.exit(1)
    if not (os.environ.get("DISCORD_WEBHOOK_URL") or config.get("discord_webhook_url")):
        log.warning("discord_webhook_url이 비어 있어요 — 알림은 로그로만 출력됩니다.")

    Watcher(config).loop(once=args.once)


if __name__ == "__main__":
    main()
