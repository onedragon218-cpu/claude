from datetime import date

CONSENSUS_STALE_DAYS = 7      # 이보다 오래된 컨센서스만 자동으로 새로 조회(갱신 버튼은 항상 조회)
YOY_WARN = 0.5                 # 전년 대비 ±50% 넘으면 '급변' 표시(상태는 바꾸지 않음)
TOL_BPS = 0.005                # BPS 교차검증 허용 오차 0.5%
TOL_MATCH = 0.005              # FnGuide 실적 열 ↔ DART 대조 허용 오차 0.5%
TOL_MCAP = 0.01                # 시가총액 = 종가 × 주식수 허용 오차 1%

METRICS = [  # (키, 이름, 단위)
    ("revenue", "매출액", "억원"),
    ("op_margin", "영업이익률", "%"),
    ("eps", "EPS", "원"),
    ("bps", "BPS", "원"),
    ("roe", "ROE", "%"),
    ("debt_ratio", "부채비율", "%"),
    ("market_cap", "시가총액", "억원"),
]
REQUIRED_ACCOUNTS = ["revenue", "op", "ni_owner", "eps", "eq_owner", "eq", "liab"]
BASE_ACCOUNTS = REQUIRED_ACCOUNTS + ["issued_capital"]
TRUSTED = ("FnGuide", "WiseReport")      # 기본 화면에 쓰는 전망 출처(앞쪽 우선)


def periods(base_year=None):
    """기준 연도에서 분석 기간을 만든다. 실행 연도가 바뀌면 자동으로 따라간다."""
    b = base_year or date.today().year
    return {"base": b, "actual": list(range(b - 10, b)), "future": [b + 1, b + 2], "helper": [b - 11]}
