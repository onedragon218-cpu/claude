"""DART 사업보고서(연결 전체 재무제표) 수집·해석.

조회 순서
1) data/raw/dart/<종목>_<CFS>_<보고서>_<연도>.json.gz 원본 보관본이 있으면 그것을 쓴다(네트워크 없음).
2) 없고 환경변수 DART_API_KEY가 있으면 OpenDART API(fnlttSinglAcntAll)를 한 번 호출하고 원본을 보관한다.
3) 둘 다 안 되면 '미확보'로 돌려준다. 숫자를 지어내지 않는다.

한 사업보고서에는 당기·전기·전전기 3개년이 들어 있으므로, 필요한 연도를 덮는 최소한의 보고서만 고른다.
"""
import gzip, json, os, re

API = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
SOURCE = "DART 사업보고서"

# (항목, 재무제표 구분, 표준계정 ID들, 계정명 보조)
ACCOUNTS = [
    ("revenue", ("IS", "CIS"), ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익"]),
    ("op", ("IS", "CIS"), ["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)"]),
    ("ni_owner", ("IS", "CIS"), ["ifrs-full_ProfitLossAttributableToOwnersOfParent"], ["지배기업의소유주에게귀속되는당기순이익", "지배기업소유주지분"]),
    ("eps", ("IS", "CIS"), ["ifrs-full_BasicEarningsLossPerShare"], ["기본주당이익", "기본주당순이익", "기본주당순손익", "기본주당이익(손실)"]),
    ("eq_owner", ("BS",), ["ifrs-full_EquityAttributableToOwnersOfParent"], ["지배기업의소유주에게귀속되는자본", "지배기업소유주지분"]),
    ("eq", ("BS",), ["ifrs-full_Equity"], ["자본총계"]),
    ("liab", ("BS",), ["ifrs-full_Liabilities"], ["부채총계"]),
    ("issued_capital", ("BS",), ["ifrs-full_IssuedCapital"], ["자본금"]),
]
UNITS = {"eps": "원"}
COLS = (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2))


def raw_path(root, stock_code, year, fs_div="CFS", reprt="11011"):
    return os.path.join(root, "data", "raw", "dart", f"{stock_code}_{fs_div}_{reprt}_{year}.json.gz")


def plan_reports(needed_years):
    """필요한 연도를 덮는 사업보고서 연도 목록(최신 보고서부터, 보고서 하나가 3개년을 덮는다고 보고)."""
    left, plan = sorted(set(needed_years), reverse=True), []
    while left:
        y = left[0]
        plan.append(y)
        left = [x for x in left if x < y - 2]
    return plan


def load(root, stock_code, corp_code, year, fs_div="CFS", reprt="11011"):
    """반환: (json 또는 None, 채널, 설명)"""
    p = raw_path(root, stock_code, year, fs_div, reprt)
    if os.path.exists(p):
        return json.load(gzip.open(p)), "raw-cache", os.path.relpath(p, root)
    key = os.environ.get("DART_API_KEY")
    if not key:
        return None, "api", "DART_API_KEY 없음 → 미확보"
    import requests
    try:
        r = requests.get(API, params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": str(year),
                                      "reprt_code": reprt, "fs_div": fs_div}, timeout=30)
        d = r.json()
    except Exception as e:  # 네트워크 차단 등
        return None, "api", f"호출 실패: {type(e).__name__}: {e}"[:300]
    if d.get("status") != "000":
        return None, "api", f"DART 응답 {d.get('status')} {d.get('message')}"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return d, "api", "OpenDART 호출 후 원본 보관"


def _num(s):
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def parse(d):
    """DART 응답 → {연도: {항목: 값}}, 접수번호"""
    lst = d.get("list", [])
    if not lst:
        return {}, None
    year, rcept = int(lst[0]["bsns_year"]), lst[0]["rcept_no"]
    out = {}
    for col, off in COLS:
        row = {}
        for item, sj, ids, names in ACCOUNTS:
            hit = None
            for it in lst:
                aid = it.get("account_id", "").replace("ifrs_", "ifrs-full_", 1)
                if it.get("sj_div") in sj and aid in ids and _num(it.get(col)) is not None:
                    hit = _num(it.get(col)); break
            if hit is None:  # 표준계정 코드가 없는 옛 보고서: 계정명으로 찾는다
                for it in lst:
                    nm = re.sub(r"\s", "", it.get("account_nm", ""))
                    if it.get("sj_div") in sj and nm in names and _num(it.get(col)) is not None:
                        hit = _num(it.get(col)); break
            if hit is not None:
                row[item] = hit
        if row:
            out[year - off] = row
    return out, rcept


LIST_API = "https://opendart.fss.or.kr/api/list.json"


def corrections(corp_code, bgn, end):
    """사업보고서 정정 공시 목록. DART_API_KEY가 없거나 호출이 막히면 None."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return None, "DART_API_KEY 없음"
    import requests
    found, page = [], 1
    try:
        while True:
            d = requests.get(LIST_API, params={"crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn, "end_de": end,
                                               "pblntf_ty": "A", "page_no": page, "page_count": 100}, timeout=30).json()
            if d.get("status") not in ("000",):
                break
            found += [x for x in d.get("list", []) if "정정" in x.get("report_nm", "") and "사업보고서" in x.get("report_nm", "")]
            if page >= int(d.get("total_page", 1)):
                break
            page += 1
    except Exception as e:
        return None, f"호출 실패: {type(e).__name__}"
    return found, "ok"
