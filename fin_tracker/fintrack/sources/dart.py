"""DART(OpenDART) 수집·해석.

조회 순서(모든 함수 공통)
1) 작업 폴더 raw/dart/ 에 보관 원본이 있으면 그것을 쓴다(네트워크 없음).
2) 없으면 인증키로 OpenDART를 한 번 호출하고 원본을 보관한다.
3) 둘 다 안 되면 '미확보'로 돌려준다. 숫자를 지어내지 않는다.
인증키는 secret.get_dart_key()로만 읽고, 로그·오류 문구·파일명에 넣지 않는다.
"""
import gzip, io, json, os, re, time, zipfile
import xml.etree.ElementTree as ET

from ..secret import get_dart_key

BASE = "https://opendart.fss.or.kr/api/"
SOURCE = "DART 사업보고서"
SOURCE_PARTIAL = "DART 분기·반기보고서"
REPORTS = {"11011": "사업보고서", "11012": "반기보고서", "11013": "1분기보고서", "11014": "3분기보고서"}
PERIOD = {"11013": "Q1", "11012": "H1", "11014": "Q3"}

ACCOUNTS = [
    ("revenue", ("IS", "CIS"), ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익", "매출"]),
    ("op", ("IS", "CIS"), ["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)", "영업손익"]),
    ("ni_owner", ("IS", "CIS"), ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
     ["지배기업의소유주에게귀속되는당기순이익", "지배기업소유주지분", "지배기업의소유주지분"]),
    ("eps", ("IS", "CIS"), ["ifrs-full_BasicEarningsLossPerShare"], ["기본주당이익", "기본주당순이익", "기본주당순손익", "기본주당이익(손실)"]),
    ("eq_owner", ("BS",), ["ifrs-full_EquityAttributableToOwnersOfParent"], ["지배기업의소유주에게귀속되는자본", "지배기업소유주지분"]),
    ("eq", ("BS",), ["ifrs-full_Equity"], ["자본총계"]),
    ("liab", ("BS",), ["ifrs-full_Liabilities"], ["부채총계"]),
    ("issued_capital", ("BS",), ["ifrs-full_IssuedCapital"], ["자본금"]),
]
FLOW = {"revenue", "op", "ni_owner", "eps"}   # 손익(기간) 항목. 분기 보고서에서는 누적값을 쓴다
UNITS = {"eps": "원"}
COLS = (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2))


class DartError(Exception):
    pass


def _get(path, params, binary=False):
    key = get_dart_key()
    if not key:
        raise DartError("DART 인증키가 없습니다(화면 오른쪽 위 '설정'에서 등록)")
    import requests
    try:
        r = requests.get(BASE + path, params={"crtfc_key": key, **params}, timeout=40)
        r.raise_for_status()
    except Exception as e:
        raise DartError(f"OpenDART 호출 실패: {type(e).__name__}") from None
    if binary:
        return r.content
    d = r.json()
    st = d.get("status")
    if st == "013":
        return {"status": "013", "list": []}            # 자료 없음
    if st != "000":
        raise DartError(f"OpenDART 응답 {st}: {d.get('message')}")
    time.sleep(0.15)                                     # 호출 간격(분당 한도 보호)
    return d


def _cache(w, name, fetch):
    p = os.path.join(w.rawdir("dart"), name)
    if os.path.exists(p):
        return json.load(gzip.open(p, "rt", encoding="utf-8")), "raw-cache"
    d = fetch()
    if d.get("list"):
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    return d, "api"


# ---------- 기업 목록 ----------
def corp_list(w, max_age_days=30):
    """[{corp_code, name, stock_code}] 상장사만. 30일마다 새로 받는다."""
    p = os.path.join(w.rawdir("dart"), "corpCode.json.gz")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_days * 86400:
        return json.load(gzip.open(p, "rt", encoding="utf-8"))
    try:
        z = zipfile.ZipFile(io.BytesIO(_get("corpCode.xml", {}, binary=True)))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except DartError:
        if os.path.exists(p):
            return json.load(gzip.open(p, "rt", encoding="utf-8"))
        raise
    out = [{"corp_code": e.findtext("corp_code"), "name": e.findtext("corp_name"), "stock_code": e.findtext("stock_code").strip()}
           for e in root.iter("list") if (e.findtext("stock_code") or "").strip()]
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def search(w, q):
    q = q.strip()
    lst = corp_list(w)
    if re.fullmatch(r"\d{6}", q):
        return [c for c in lst if c["stock_code"] == q]
    ql = q.replace(" ", "").lower()
    exact = [c for c in lst if c["name"].replace(" ", "").lower() == ql]
    part = [c for c in lst if ql in c["name"].replace(" ", "").lower() and c not in exact]
    return (exact + sorted(part, key=lambda c: len(c["name"])))[:15]


# ---------- 재무제표 ----------
def statement(w, stock_code, corp_code, year, reprt="11011", fs_div="CFS"):
    """반환: (json 또는 None, 채널, 설명). 연결이 없으면 별도(OFS)로 한 번 더 시도."""
    for fs in ((fs_div, "OFS") if fs_div == "CFS" else (fs_div,)):
        name = f"{stock_code}_{fs}_{reprt}_{year}.json.gz"
        try:
            d, ch = _cache(w, name, lambda: _get("fnlttSinglAcntAll.json", {
                "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt, "fs_div": fs}))
        except DartError as e:
            return None, "api", str(e)
        if d.get("list"):
            return d, ch, f"{name}" + ("" if fs == "CFS" else " (연결 없음 → 별도 기준)")
    return None, "api", f"{year} {REPORTS[reprt]} 없음"


def plan_reports(needed_years):
    """필요한 연도를 덮는 사업보고서 연도 목록(보고서 하나가 3개년을 덮음)."""
    left, plan = sorted(set(needed_years), reverse=True), []
    while left:
        y = left[0]
        plan.append(y)
        left = [x for x in left if x < y - 2]
    return plan


def _num(s):
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _find(lst, sj, ids, names, col):
    for it in lst:
        aid = it.get("account_id", "").replace("ifrs_", "ifrs-full_", 1)
        if it.get("sj_div") in sj and aid in ids and _num(it.get(col)) is not None:
            return _num(it.get(col))
    for it in lst:  # 표준계정 코드가 없는 옛 보고서: 계정명으로
        nm = re.sub(r"\s", "", it.get("account_nm", ""))
        if it.get("sj_div") in sj and nm in names and _num(it.get(col)) is not None:
            return _num(it.get(col))
    return None


def parse(d):
    """사업보고서 → ({연도: {항목: 값}}, 접수번호, 기준(CFS/OFS))"""
    lst = d.get("list", [])
    if not lst:
        return {}, None, None
    year, rcept = int(lst[0]["bsns_year"]), lst[0]["rcept_no"]
    fs = "OFS" if any(it.get("fs_div") == "OFS" for it in lst[:3]) else "CFS"
    out = {}
    for col, off in COLS:
        row = {item: v for item, sj, ids, names in ACCOUNTS if (v := _find(lst, sj, ids, names, col)) is not None}
        if row:
            out[year - off] = row
    return out, rcept, fs


def parse_partial(d):
    """분기·반기 보고서 → ({항목: 값}, 접수번호). 손익은 누적(thstrm_add_amount), 재무상태는 기말."""
    lst = d.get("list", [])
    if not lst:
        return {}, None
    row = {}
    for item, sj, ids, names in ACCOUNTS:
        v = None
        if item in FLOW:
            v = _find(lst, sj, ids, names, "thstrm_add_amount")
        if v is None:
            v = _find(lst, sj, ids, names, "thstrm_amount")
        if v is not None:
            row[item] = v
    return row, lst[0]["rcept_no"]


def latest_partial(w, stock_code, corp_code, year):
    """올해 가장 최근 분기·반기 보고서. 반환: (보고서코드, json, 채널) 또는 (None, None, 설명)"""
    last_err = ""
    for reprt in ("11014", "11012", "11013"):
        name = f"{stock_code}_CFS_{reprt}_{year}.json.gz"
        p = os.path.join(w.rawdir("dart"), name)
        d, ch, desc = statement(w, stock_code, corp_code, year, reprt)
        if d:
            return reprt, d, ch
        last_err = desc
    return None, None, last_err


# ---------- 주식 수 ----------
def shares(w, stock_code, corp_code, year, reprt="11011"):
    """주식의 총수 현황 → {'common': 발행 보통주, 'treasury': 보통주 자기주식, 'pref': 우선주, 'rcept': 접수번호}"""
    name = f"{stock_code}_shares_{reprt}_{year}.json.gz"
    try:
        d, ch = _cache(w, name, lambda: _get("stockTotqySttus.json", {
            "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt}))
    except DartError as e:
        return None, "api", str(e)
    res = {}
    for it in d.get("list", []):
        se = re.sub(r"\s", "", it.get("se", ""))
        if se.startswith("보통"):
            res["common"] = _num(it.get("istc_totqy"))
            res["treasury"] = _num(it.get("tesstk_co")) or 0
            res["rcept"] = it.get("rcept_no")
        elif se.startswith("우선"):
            res["pref"] = _num(it.get("istc_totqy"))
    return (res or None), ch, name


# ---------- 정정 공시 ----------
def corrections(corp_code, bgn, end):
    """사업보고서 정정 공시 목록. 키가 없거나 막히면 (None, 사유)."""
    found, page = [], 1
    try:
        while True:
            d = _get("list.json", {"corp_code": corp_code, "bgn_de": bgn, "end_de": end,
                                   "pblntf_ty": "A", "page_no": page, "page_count": 100})
            found += [x for x in d.get("list", []) if "정정" in x.get("report_nm", "") and "사업보고서" in x.get("report_nm", "")]
            if page >= int(d.get("total_page", 1) or 1):
                break
            page += 1
    except DartError as e:
        return None, str(e)
    return found, "ok"
