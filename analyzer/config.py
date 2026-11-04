"""분석 기간, 지표 정의, 경로 등 공통 설정."""
import datetime as _dt
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir():
    """데이터 폴더. 1) 환경변수 ANALYZER_DATA 2) 사용자 설정 파일(윈도우 설치 시 %APPDATA%\\corp-analyzer\\settings.json)
    3) 프로그램 폴더의 data. 윈도우에서는 프로그램을 새 버전으로 바꿔도 DB가 지워지지 않도록 프로그램 폴더 밖에 둔다."""
    if os.environ.get("ANALYZER_DATA"):
        return os.environ["ANALYZER_DATA"]
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    f = os.path.join(base, "corp-analyzer", "settings.json")
    if os.path.exists(f):
        try:
            import json
            with open(f, encoding="utf-8-sig") as fh:
                d = json.load(fh).get("data_dir")
            if d:
                return d
        except (OSError, ValueError):
            pass
    return os.path.join(ROOT, "data")


DATA_DIR = _data_dir()
DB_PATH = os.environ.get("ANALYZER_DB", os.path.join(DATA_DIR, "analyzer.db"))
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed")

# 재수집 간격(일). 이 기간 안에는 같은 요청을 다시 보내지 않는다.
TTL_DAYS = {
    "dart_report": 36500,      # 확정 공시는 다시 받지 않음(정정 공시는 공시목록 점검으로 따로 잡음)
    "dart_report_missing": 7,  # 공시가 아직 없던 연도는 7일 뒤 다시 확인
    "dart_disclosures": 1,     # 신규·정정 공시 목록 점검
    "dart_shares": 36500,
    "price_yearend": 36500,    # 지난 연도 말 종가는 바뀌지 않음
    "price_asof": 1,           # 분석 기준일 주가
    "consensus": 7,            # 컨센서스 스냅숏
}


def periods(base_year=None):
    """기준 연도로 분석 기간을 계산한다. 기본값은 실행 시점의 연도."""
    by = int(base_year or _dt.date.today().year)
    return {
        "base_year": by,
        "past": list(range(by - 10, by)),
        "current": by,
        "future": [by + 1, by + 2],
        "all": list(range(by - 10, by + 3)),
    }


# 7개 표시 지표
METRICS = [
    # key, 이름, 단위
    ("revenue", "매출액", "억원"),
    ("op_margin", "영업이익률", "%"),
    ("eps", "EPS", "원"),
    ("bps", "BPS", "원"),
    ("roe", "ROE", "%"),
    ("debt_ratio", "부채비율", "%"),
    ("market_cap", "시가총액", "억원"),
]
METRIC_KEYS = [m[0] for m in METRICS]
METRIC_NAME = {m[0]: m[1] for m in METRICS}
METRIC_UNIT = {m[0]: m[2] for m in METRICS}

# 원천 계정(원 단위로 저장)
ACCOUNTS = {
    "acc_revenue": "매출액(원)",
    "acc_op": "영업이익(원)",
    "acc_ni": "당기순이익(원)",
    "acc_ni_owner": "지배주주 순이익(원)",
    "acc_assets": "자산총계(원)",
    "acc_liab": "부채총계(원)",
    "acc_eq": "자본총계(원)",
    "acc_eq_owner": "지배주주 자본(원)",
    "acc_cash_only": "현금및현금성자산(원)",
    "acc_cash": "현금및현금성자산+단기금융상품(원)",
    "acc_debt": "차입금·사채·리스부채(원)",
    "acc_ocf": "영업활동 현금흐름(원)",
    "acc_cfi": "투자활동 현금흐름(원)",
    "acc_cff": "재무활동 현금흐름(원)",
    "acc_fx": "현금 환율변동효과(원)",
    "acc_eps_basic": "기본주당순이익(원, 보통주)",
    "shares_common": "보통주 발행주식수(주, 연말)",
    "shares_pref": "우선주 발행주식수(주, 연말)",
    "treasury_common": "보통주 자기주식수(주, 연말)",
    "treasury_pref": "우선주 자기주식수(주, 연말)",
    "shares_derived": "역산 주식수(지배순이익/EPS, 참고용)",
    "price_close": "보통주 종가(원)",
}

STATUSES = ["UNVERIFIED", "VERIFIED", "CONFLICT", "CORRECTED", "STALE", "MISSING"]

SOURCE_RANK = {"DART": 1, "FnGuide": 2, "KRX": 3, "KIS": 3, "Naver": 4, "manual": 5, "derived": 9}
