# 기업 장기 재무 추이 (fin_tracker) — 한솔제지 샘플

과거 10년 확정 실적 + 올해 + 2년 뒤까지의 컨센서스를 모아 7개 지표(매출액·영업이익률·EPS·BPS·ROE·부채비율·시가총액)를
**통합 그래프 1개 + 개별 그래프 7개**로 보여 주는 프로그램입니다. 한 번 검증한 값은 SQLite에 쌓아 두고 다시 조회하지 않습니다.

- 화면: `output/213500.html` (브라우저로 바로 열림, 인터넷 없어도 그래프 표시)
- DB: `data/fintrack.sqlite`
- 기준일: 2026-11-04 실행, 시세·컨센서스는 2026-11-03 종가 기준

## 실행

```bash
cd fin_tracker
pip install requests beautifulsoup4 lxml      # 처음 한 번
python3 -m fintrack run 213500                # 기준 연도 = 올해(자동)
python3 -m fintrack run 213500 --base-year 2027
python3 -m fintrack status 213500             # DB 보유 현황
python3 -m unittest discover -s tests         # 재사용·버전관리 시험
```

| 옵션 | 하는 일 |
|---|---|
| `--base-year` | 기준 연도 변경. 실적 = 기준−10 ~ 기준−1, 전망 = 기준 ~ 기준+2 |
| `--refresh` | 컨센서스 갱신 주기(7일)를 무시하고 새로 조회 |
| `--reverify` | 저장값 검사 다시 실행(조회는 안 함) |
| `--offline` | 네트워크 없이 보관 원본과 DB만 사용 |

환경변수 `DART_API_KEY`(OpenDART 무료 인증키)를 넣으면, DB와 보관 원본에 없는 연도의 사업보고서를 API로 받고
사업보고서 정정 공시도 확인합니다.

## 다른 종목 분석

1. `data/companies.json`에 한 줄 추가: `"005930": {"name": "삼성전자", "corp_code": "00126380", "market": "KOSPI", "face_value": 100}`
2. `DART_API_KEY` 설정 후 `python3 -m fintrack run 005930`
3. 연말 종가는 `data/manual/<종목>_prices.json` 형식으로 넣기(현재는 자동 수집기 없음, 아래 한계 참고)

## 구조

| 경로 | 역할 |
|---|---|
| `fintrack/sources/dart.py` | DART 사업보고서 수집·해석. 보고서 1개가 3개년을 덮으므로 필요한 최소 보고서만 고름 |
| `fintrack/sources/wisereport.py` | WiseReport 기업개요 공개 페이지 1장에서 시세·올해 컨센서스(EPS·BPS) 해석 |
| `fintrack/pipeline.py` | 수집 계획: DB에 VERIFIED가 있으면 건너뜀(`skipped-reuse`) |
| `fintrack/validate.py` | 검증 10종(아래) |
| `fintrack/metrics.py` | 원값 → 7개 지표. 계산식·출처·상태를 값마다 붙임 |
| `fintrack/db.py` | 저장소. 덮어쓰기 없이 버전 추가, 변경 이력, 조회 기록 |
| `fintrack/dashboard.py`, `template.html` | 화면 생성 |
| `data/raw/` | 받은 원본 그대로(gzip). 다시 계산할 때 재조회 불필요 |
| `data/manual/` | 이 세션에서 자동 수집이 막혀 손으로 넣은 값과 그 출처 |

### DB 표
- `observations`: 모든 원값. 키 = 종목·연도·기간·항목·종류(ACTUAL/ACTUAL_PARTIAL/CONSENSUS/MARKET/REFERENCE)·출처·기준일.
  값이 바뀌면 새 버전 행을 넣고 이전 행은 `is_current=0`으로 남김. 상태: UNVERIFIED·VERIFIED·CONFLICT·CORRECTED·STALE·MISSING
- `change_log`: 이전 값 → 새 값, 이유
- `fetch_log`: 실행마다 어떤 출처를 조회했고 무엇을 재사용했는지
- `validation_results`, `runs`

### 검증 항목
1. 출처 추적(접수번호) 2. 자본 항등식 3. EPS·순이익·주식수 관계 4. 영업이익률·부채비율·ROE 재계산(산식 표시)
5. 시가총액 = 주가 × 주식수 6. 다른 출처와 교차검증(2025 EPS·BPS ↔ WiseReport) 7. 전년 대비 ±50% 급변
8. 정정 공시(키 있을 때) 9. 실적·전망 혼합 금지 10. 컨센서스 현실성(반기 실제값 대비)·표본 수·출처 간 차이

## 한솔제지(213500) 결과 요약

| 구분 | 확보 | 상태 |
|---|---|---|
| 2016~2025 실적 7개 지표 | 전부 | 매출·이익·자본 계정 88건 VERIFIED (DART 사업보고서 4개) |
| 2025 EPS 163원, BPS | WiseReport와 대조 | 일치(BPS 차이 −0.10%, 자사주 차감 여부) |
| 2026 반기 실적 | 영업이익률 5.38%, BPS 30,465원, 부채비율 192.4% | VERIFIED |
| 2026 시가총액 | 6,610원 × 23,800,576주 = 1,573억 | VERIFIED |
| 2026E EPS 1,073원 | WiseReport(FnGuide), 2곳 평균 | VERIFIED, 표본 적음 경고 |
| 2026E BPS 25,136원 | WiseReport | **CONFLICT**: 6월 말 실제 BPS보다 17.5% 낮음 |
| 2026E 매출·영업이익·ROE, 2027~2028 전부 | 검증된 출처 없음 | MISSING (빈칸 유지) |
| 이전 대화의 psy-invest 전망치 | 13건 | UNVERIFIED(원문 미확인). 2026E EPS 2,813원은 WiseReport와 +162% 차이 → CONFLICT. 화면에서 '미검증 전망 함께 보기'를 켜야 보임 |

## 알려진 한계 (이 세션 기준)
- **2027~2028 컨센서스 미확보.** FnGuide 연도별 컨센서스 표는 공개 페이지가 내부 호출로 불러오는데, 이 호출을 흉내 내는 것은 사이트의
  자동 조회 차단을 우회하는 방식이라 하지 않았습니다. 네이버 증권·증권사 리포트 PDF 서버·한국거래소는 이 세션 네트워크에서 막혀 있었습니다.
- **과거 시가총액은 계산값.** 연말 수정주가(KIS) × 자본금÷액면가. 2016→2017 사이 자본금이 1,007억→1,190억으로 늘어 2016~2017 값은
  한국거래소 공식 시가총액과 다를 수 있습니다(UNVERIFIED).
- BPS는 자사주를 빼지 않은 주식수로 나눴습니다(WiseReport와 0.1% 차이).
- 정정 공시 확인은 `DART_API_KEY`가 있을 때만 실행됩니다. 이번 실행에서는 미실행(WARN).
- 연말 종가 자동 수집기는 아직 없습니다(KIS·KRX 연결 필요).
