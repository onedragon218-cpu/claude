"""DART(금융감독원 전자공시) 수집기.

- parse_statement(): DART '단일회사 전체 재무제표'(fnlttSinglAcntAll) 응답 JSON을 계정 값으로 바꾼다.
  API로 받은 응답과, 다른 경로로 받아 저장한 같은 형식의 JSON 파일 모두 처리한다.
- fetch_*(): OpenDART API 호출. 환경변수 DART_API_KEY 가 필요하다(무료, opendart.fss.or.kr 에서 발급).
"""
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile

from .. import config

API = "https://opendart.fss.or.kr/api/"
UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
REPRT_PERIOD = {"11011": "FY", "11012": "H1", "11013": "Q1", "11014": "Q3"}
REPRT_NAME = {"11011": "사업보고서", "11012": "반기보고서", "11013": "1분기보고서", "11014": "3분기보고서"}

# 계정 → (재무제표 구분, 표준 계정 ID 후보, 계정명 후보)
MAP = {
    "acc_revenue": (("IS", "CIS"), ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익"]),
    "acc_op": (("IS", "CIS"), ["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)"]),
    "acc_ni": (("IS", "CIS"), ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "acc_ni_owner": (("IS", "CIS"), ["ifrs-full_ProfitLossAttributableToOwnersOfParent"], ["지배기업소유주지분", "지배기업의소유주"]),
    "acc_eps_basic": (("IS", "CIS"), ["ifrs-full_BasicEarningsLossPerShare"], ["기본주당이익", "기본주당순이익", "기본주당이익(손실)", "기본주당순손익", "보통주기본주당순손익"]),
    "acc_assets": (("BS",), ["ifrs-full_Assets"], ["자산총계"]),
    "acc_liab": (("BS",), ["ifrs-full_Liabilities"], ["부채총계"]),
    "acc_eq": (("BS",), ["ifrs-full_Equity"], ["자본총계"]),
    "acc_eq_owner": (("BS",), ["ifrs-full_EquityAttributableToOwnersOfParent"], ["지배기업소유주지분", "지배기업의소유주에게귀속되는자본"]),
}
# 가치평가(DCF·ROIC·WACC)용 계정
MAP.update({
    "acc_pretax": (("IS", "CIS"), ["ifrs-full_ProfitLossBeforeTax"], ["법인세비용차감전순이익", "법인세비용차감전순이익(손실)"]),
    "acc_tax": (("IS", "CIS"), ["ifrs-full_IncomeTaxExpenseContinuingOperations"], ["법인세비용", "법인세비용(수익)", "법인세수익(비용)"]),
    "acc_ocf": (("CF",), ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름", "영업활동으로인한현금흐름"]),
    "acc_capex_ppe": (("CF",), ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"], ["유형자산의취득", "유형자산취득"]),
    "acc_capex_int": (("CF",), ["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"], ["무형자산의취득", "무형자산취득"]),
    # 이자지급: 영업활동 분류(FCFF 계산 때 되더함)와 재무활동 분류를 나눠 둔다. 표준코드가 없으면 영업활동으로 본다
    "acc_int_paid": (("CF",), ["ifrs-full_InterestPaidClassifiedAsOperatingActivities"], ["이자의지급", "이자지급", "이자의지급(영업)"]),
    "acc_int_paid_fin": (("CF",), ["ifrs-full_InterestPaidClassifiedAsFinancingActivities"], []),
    # 현금 추이·증감 원인(화면 H)
    "acc_cash_only": (("BS",), ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "acc_cfi": (("CF",), ["ifrs-full_CashFlowsFromUsedInInvestingActivities"], ["투자활동현금흐름", "투자활동으로인한현금흐름"]),
    "acc_cff": (("CF",), ["ifrs-full_CashFlowsFromUsedInFinancingActivities"], ["재무활동현금흐름", "재무활동으로인한현금흐름"]),
    "acc_fx": (("CF",), ["ifrs-full_EffectOfExchangeRateChangesOnCashAndCashEquivalents"],
               ["현금및현금성자산에대한환율변동효과", "외화표시현금및현금성자산의환율변동효과", "현금및현금성자산의환율변동효과"]),
})
# 여러 줄을 더해야 하는 계정(재무상태표): 이름 규칙으로 찾는다
SUM = {
    "acc_debt": re.compile(r"(차입금|사채|리스부채|유동성장기부채)"),
    "acc_cash": re.compile(r"^(현금및현금성자산|단기금융상품)$"),
}
SUM_EXCLUDE = re.compile(r"(사채할인|상환할증|전환권조정|대여금|이자|미지급|충당)")
ALL_KEYS = list(MAP) + list(SUM)
ABS_KEYS = {"acc_capex_ppe", "acc_capex_int", "acc_int_paid", "acc_int_paid_fin"}   # 회사마다 부호 표기가 달라 절댓값 사용
CF_KEYS = {"acc_ocf", "acc_capex_ppe", "acc_capex_int", "acc_int_paid", "acc_int_paid_fin", "acc_cfi", "acc_cff", "acc_fx"}
FLOW = {"acc_revenue", "acc_op", "acc_ni", "acc_ni_owner", "acc_eps_basic", "acc_pretax", "acc_tax"} | CF_KEYS
PARSER_VERSION = 3   # 계정을 늘리면 올린다 → 저장해 둔 원본 응답을 네트워크 없이 다시 해석


def _num(s):
    if s in (None, "", "-"):
        return None
    s = str(s).replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _norm_id(aid):
    return (aid or "").replace("ifrs_", "ifrs-full_", 1) if (aid or "").startswith("ifrs_") else (aid or "")


def _find(lst, key, col):
    sjs, ids, names = MAP[key]
    for sj in sjs:                       # 1순위: 표준 계정 ID
        for it in lst:
            if it.get("sj_div") == sj and _norm_id(it.get("account_id")) in ids:
                v = _num(it.get(col))
                if v is not None:
                    return v
    for sj in sjs:                       # 2순위: 계정 이름
        for it in lst:
            if it.get("sj_div") == sj and re.sub(r"\s", "", it.get("account_nm", "")) in names:
                v = _num(it.get(col))
                if v is not None:
                    return v
    return None


def _sum(lst, key, col):
    rx, tot, hit = SUM[key], 0.0, False
    for it in lst:
        if it.get("sj_div") != "BS":
            continue
        nm = re.sub(r"\s", "", it.get("account_nm", ""))
        if rx.search(nm) and not SUM_EXCLUDE.search(nm):
            v = _num(it.get(col))
            if v is not None:
                tot += v; hit = True
    has_bs = any(it.get("sj_div") == "BS" and _num(it.get(col)) is not None for it in lst)
    return tot if (hit or has_bs) else None   # 재무상태표는 있는데 해당 줄이 없으면 0


def _val(lst, key, col):
    v = _sum(lst, key, col) if key in SUM else _find(lst, key, col)
    if key == "acc_int_paid" and v is not None:
        # 이름으로 찾은 '이자지급'이 실제로는 재무활동 분류 줄이면 영업활동 이자로 보지 않는다
        has_op_id = any(_norm_id(it.get("account_id")) == "ifrs-full_InterestPaidClassifiedAsOperatingActivities" for it in lst)
        if not has_op_id and _find(lst, "acc_int_paid_fin", col) is not None:
            v = None
    if v is not None and key in ABS_KEYS:
        v = abs(v)
    return v


def parse_statement(d, fs_div="CFS"):
    """반환: dict(meta=..., values=[(fiscal_year, period, metric, value, col)])"""
    lst = d.get("list") or []
    if not lst:
        return {"meta": {"status": d.get("status"), "message": d.get("message")}, "values": []}
    year = int(lst[0]["bsns_year"])
    reprt = lst[0]["reprt_code"]
    period = REPRT_PERIOD[reprt]
    meta = {"rcept_no": lst[0]["rcept_no"], "bsns_year": year, "reprt_code": reprt, "period": period,
            "basis": fs_div, "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={lst[0]['rcept_no']}"}
    out = []
    if reprt == "11011":
        cols = [("thstrm_amount", year, "당기"), ("frmtrm_amount", year - 1, "전기"), ("bfefrmtrm_amount", year - 2, "전전기")]
        for col, y, cname in cols:
            for key in ALL_KEYS:
                v = _val(lst, key, col)
                if v is not None:
                    out.append((y, "FY", key, v, cname))
    else:
        for key in ALL_KEYS:
            col = "thstrm_add_amount" if (key in FLOW and key not in CF_KEYS) else "thstrm_amount"
            v = _val(lst, key, col)
            if v is None and key in FLOW:   # 1분기 보고서는 누적 열이 비어 있을 수 있음
                v = _val(lst, key, "thstrm_amount")
            if v is not None:
                out.append((year, period, key, v, "당기누적" if key in FLOW else "당기말"))
            if key in FLOW:                 # 전년 같은 기간 누적(최근 12개월 계산용)
                pcol = "frmtrm_amount" if key in CF_KEYS else "frmtrm_add_amount"
                pv = _val(lst, key, pcol)
                if pv is not None:
                    out.append((year - 1, period, key, pv, "전년동기누적"))
    return {"meta": meta, "values": out}


def parse_shares(d):
    """주식의 총수 현황(stockTotqySttus) 응답 → {'common':..., 'pref':..., 'treasury_common':..., ...}"""
    res = {}
    for it in d.get("list") or []:
        se = re.sub(r"\s", "", it.get("se", ""))
        kind = "common" if "보통" in se else "pref" if "우선" in se else None
        if kind is None:
            continue
        issued = _num(it.get("istc_totqy"))
        treasury = _num(it.get("tesstk_co"))
        if issued is not None:
            res[kind] = issued
        if treasury is not None:
            res["treasury_" + kind] = treasury
    if d.get("list"):
        res["rcept_no"] = d["list"][0].get("rcept_no")
    return res


# ---------------- OpenDART API ----------------
class DartUnavailable(Exception):
    pass


def key_file():
    """저장소 밖의 사용자 폴더(윈도우: %APPDATA%\\corp-analyzer)에 키를 둔다. 저장소에 커밋될 일이 없다."""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "dart_api_key.txt")


def api_key():
    """1순위 환경변수 DART_API_KEY, 2순위 키 파일."""
    k = os.environ.get("DART_API_KEY", "").strip()
    if not k and os.path.exists(key_file()):
        with open(key_file(), encoding="utf-8") as f:
            k = f.read().strip()
    if not k:
        raise DartUnavailable("DART_API_KEY가 없습니다(환경변수 또는 키 파일).")
    return k


def has_key():
    try:
        api_key()
        return True
    except DartUnavailable:
        return False


def _get(path, **params):
    params["crtfc_key"] = api_key()
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception as e:  # 네트워크 차단 등
        raise DartUnavailable(f"DART 접속 실패: {e}") from e


def get_json(path, **params):
    d = json.loads(_get(path, **params).decode("utf-8"))
    if d.get("status") not in ("000", "013"):   # 013 = 조회된 데이터 없음
        raise DartUnavailable(f"DART 오류 {d.get('status')}: {d.get('message')}")
    return d


def corp_list(refresh_days=30, allow_download=True):
    """상장사 전체 목록 {종목코드: {corp_code, name}}. data/corp_codes.json 에 저장해 두고,
    30일이 지났거나 없으면(키가 있을 때) 다시 받는다. 키가 없으면 저장본만 쓴다."""
    import time
    cache = os.path.join(config.DATA_DIR, "corp_codes.json")
    fresh = os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < refresh_days * 86400
    if os.path.exists(cache) and (fresh or not allow_download or not has_key()):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    if not has_key():
        return {}
    try:
        raw = _get("corpCode.xml")
    except DartUnavailable:
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        raise
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read(z.namelist()[0]).decode("utf-8")
    m = {}
    for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
        sc = re.search(r"<stock_code>\s*([0-9][0-9A-Z]{5})\s*</stock_code>", blk)
        if sc:
            m[sc.group(1)] = {"corp_code": re.search(r"<corp_code>(\d+)</corp_code>", blk).group(1),
                              "name": re.search(r"<corp_name>(.*?)</corp_name>", blk).group(1).strip()}
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return m


def corp_code(stock_code):
    """종목코드 → DART 고유번호."""
    m = corp_list()
    if stock_code not in m:
        m = corp_list(refresh_days=0)      # 신규 상장일 수 있으니 한 번 새로 받아 본다
    if stock_code not in m:
        raise DartUnavailable(f"{stock_code}: DART 고유번호를 찾지 못했습니다.")
    return m[stock_code]


def fetch_statement(corp, year, reprt="11011", fs_div="CFS"):
    return get_json("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt, fs_div=fs_div)


def fetch_shares(corp, year, reprt="11011"):
    return get_json("stockTotqySttus.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt)


def fetch_main_accounts(corp, year, reprt="11011"):
    """단일회사 주요계정(fnlttSinglAcnt): 자본금·자본총계 등(연결·별도 모두)"""
    return get_json("fnlttSinglAcnt.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt)


def fetch_disclosures(corp, bgn_de, end_de=None, pblntf_ty="A", max_pages=10):
    """공시 목록. pblntf_ty: A=정기공시, I=거래소공시(관리종목·매매거래정지·상장폐지 등). 정정 공시는 [기재정정] 등이 붙는다."""
    out, page = [], 1
    while page <= max_pages:
        p = dict(corp_code=corp, bgn_de=bgn_de, pblntf_ty=pblntf_ty, page_no=page, page_count=100)
        if end_de:
            p["end_de"] = end_de
        d = get_json("list.json", **p)
        out += d.get("list") or []
        if page >= int(d.get("total_page") or 1):
            return out
        page += 1
    return out
