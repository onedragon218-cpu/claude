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
FLOW = {"acc_revenue", "acc_op", "acc_ni", "acc_ni_owner", "acc_eps_basic"}


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
            for key in MAP:
                v = _find(lst, key, col)
                if v is not None:
                    out.append((y, "FY", key, v, cname))
    else:
        for key in MAP:
            col = "thstrm_add_amount" if key in FLOW else "thstrm_amount"
            v = _find(lst, key, col)
            if v is None and key in FLOW:   # 1분기 보고서는 누적 열이 비어 있을 수 있음
                v = _find(lst, key, "thstrm_amount")
            if v is not None:
                out.append((year, period, key, v, "당기누적" if key in FLOW else "당기말"))
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


def api_key():
    k = os.environ.get("DART_API_KEY", "").strip()
    if not k:
        raise DartUnavailable("DART_API_KEY 환경변수가 없습니다.")
    return k


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


def corp_code(stock_code):
    """종목코드 → DART 고유번호. 전체 목록을 한 번 받아 data/corp_codes.json 에 저장해 두고 재사용."""
    cache = os.path.join(config.ROOT, "data", "corp_codes.json")
    if os.path.exists(cache):
        m = json.load(open(cache, encoding="utf-8"))
        if stock_code in m:
            return m[stock_code]
    raw = _get("corpCode.xml")
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read(z.namelist()[0]).decode("utf-8")
    m = {}
    for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
        sc = re.search(r"<stock_code>\s*(\d{6})\s*</stock_code>", blk)
        if sc:
            m[sc.group(1)] = {"corp_code": re.search(r"<corp_code>(\d+)</corp_code>", blk).group(1),
                              "name": re.search(r"<corp_name>(.*?)</corp_name>", blk).group(1).strip()}
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    json.dump(m, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    if stock_code not in m:
        raise DartUnavailable(f"{stock_code}: DART 고유번호를 찾지 못했습니다.")
    return m[stock_code]


def fetch_statement(corp, year, reprt="11011", fs_div="CFS"):
    return get_json("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt, fs_div=fs_div)


def fetch_shares(corp, year, reprt="11011"):
    return get_json("stockTotqySttus.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt)


def fetch_disclosures(corp, bgn_de, end_de=None):
    """정기공시(A) 목록. 정정 공시는 report_nm 앞에 [기재정정] 등이 붙는다."""
    out, page = [], 1
    while True:
        p = dict(corp_code=corp, bgn_de=bgn_de, pblntf_ty="A", page_no=page, page_count=100)
        if end_de:
            p["end_de"] = end_de
        d = get_json("list.json", **p)
        out += d.get("list") or []
        if page >= int(d.get("total_page") or 1):
            return out
        page += 1
