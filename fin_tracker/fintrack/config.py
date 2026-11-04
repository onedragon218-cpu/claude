import json, os
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "fintrack.sqlite")
CONSENSUS_STALE_DAYS = 7      # 이보다 오래된 컨센서스 스냅샷만 새로 조회
YOY_WARN = 0.5                 # 전년 대비 ±50% 넘으면 '급변' 경고(상태는 바꾸지 않음)
TOL_BPS = 0.005                # BPS 교차검증 허용 오차 0.5% (자사주 차감 여부 차이)
TOL_MCAP = 0.001               # 시가총액 재계산 허용 오차 0.1%

METRICS = [  # (키, 이름, 단위)
    ("revenue", "매출액", "억원"),
    ("op_margin", "영업이익률", "%"),
    ("eps", "EPS", "원"),
    ("bps", "BPS", "원"),
    ("roe", "ROE", "%"),
    ("debt_ratio", "부채비율", "%"),
    ("market_cap", "시가총액", "억원"),
]
BASE_ACCOUNTS = ["revenue", "op", "ni_owner", "eps", "eq_owner", "eq", "liab", "issued_capital"]


def periods(base_year=None):
    """기준 연도에서 분석 기간을 만든다. 실행 연도가 바뀌면 자동으로 따라간다."""
    b = base_year or date.today().year
    return {"base": b, "actual": list(range(b - 10, b)), "future": [b + 1, b + 2],
            "helper": [b - 11]}  # ROE 평균자본 계산용 전년도


def companies():
    with open(os.path.join(ROOT, "data", "companies.json"), encoding="utf-8") as f:
        return json.load(f)
