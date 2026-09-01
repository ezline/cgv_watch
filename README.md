# CGV 예매 오픈 감시기

CGV 천호 IMAX관의 특정 영화(기본: 오디세이) 예매가 열리면 디스코드로 알려주는 개인용 스크립트.

## 실행 방식 A: GitHub Actions

1. GitHub에 **public 저장소**를 만들고 이 폴더를 푸시
   (private도 되지만 무료 Actions 시간 2,000분/월 제한에 걸리므로 크론을 30분 간격으로 늘려야 함)
2. 저장소 → Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `DISCORD_WEBHOOK_URL`, Value: 디스코드 웹훅 URL
3. 끝. 15분마다 자동 실행되고, 상태는 `state.json` 커밋으로 유지됩니다.
   Actions 탭에서 `CGV watch` 워크플로를 **Run workflow**로 즉시 1회 실행해 디스코드에 "감시 시작" 메시지가 오는지 확인하세요.

> ⚠️ GitHub 러너는 데이터센터 IP라 Cloudflare에 차단될 가능성이 있습니다.
> 차단되면 디스코드로 403 경고가 오니, 그때는 VPS로 이전하세요.

## 실행 방식 B: 로컬/서버 상시 실행

### 설치

```bash
python3 -m pip install curl_cffi
cp config.example.json config.json
```

`config.json`의 `discord_webhook_url`에 웹훅 URL을 넣어주세요.
(디스코드 → 내 서버 → 채널 설정 ⚙️ → 연동 → 웹후크 → 새 웹후크 → URL 복사)

### 실행

```bash
# 1회 테스트
python3 cgv_watch.py --once

# 상시 실행 (맥이 잠들면 폴링도 멈추므로 caffeinate 권장)
caffeinate -is python3 cgv_watch.py >> watch.out 2>&1 &
```

## 설정 (config.json)

| 키 | 의미 | 기본값 |
|---|---|---|
| `movie_keywords` | 영화 제목에 포함될 키워드 (OR) | `["오디세이"]` |
| `screen_keywords` | 관 이름에 포함될 키워드 (OR) | `["IMAX"]` |
| `site_no` | 극장 코드 (천호=0199, 용산아이파크몰=0013) | `"0199"` |
| `poll_interval_sec` | 폴링 간격(초). 600 미만 비권장 | `600` |
| `max_schedule_requests_per_cycle` | 사이클당 스케줄 조회 상한 | `8` |
| `daily_request_cap` | 하루 총 요청 상한 (초과 시 자정까지 휴지) | `400` |

## IP 차단 방지 장치

- 10분 간격 + 랜덤 지터, 평상시 사이클당 요청 1개
- 요청 간 2.5~4.5초 간격, 사이클/일일 요청 상한
- 403 감지 시 2시간 자동 휴지 + 디스코드 경고, 재시도 없음

극장 코드는 `https://cgv.co.kr/api/v1/content/site/searchAllRegionAndSite?coCd=A420`에서 확인 가능.
