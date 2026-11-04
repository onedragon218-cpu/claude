"""네이버 기업정보(WiseReport, navercomp.wisereport.co.kr) 컨센서스 수집기.

- 공개 화면 '컨센서스'(c1050001)가 쓰는 자료 주소(c1050001_data.aspx)를 일반 요청으로 받는다.
  로그인·쿠키·브라우저 흉내는 쓰지 않는다.
- 응답: 연도별 매출액·영업이익·당기순이익(억원), EPS·BPS(원), PER·PBR(배), ROE(%), EV/EBITDA.
  머리글이 'YYYY.12(E)'인 행이 증권사 추정치 평균(컨센서스), '(A)'는 실적이다.
- 추정기관 수와 목표주가는 기업개요 화면(c1010001)의 '투자의견 컨센서스' 표에서 읽는다(현재 연도 기준).
- 2026-11-04 이 개발 환경에서 한솔제지·메디톡스로 실제 수신을 확인했다.
"""
import datetime as dt
import html
import json
import re
import urllib.request

UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
BASE = "https://navercomp.wisereport.co.kr/v2/company/"
DATA_URL = BASE + "ajax/c1050001_data.aspx?flag=2&cmp_cd={code}&finGubun=MAIN&frq=0&sDT={d}&chartType=svg"
PAGE_URL = BASE + "c1010001.aspx?cmp_cd={code}"

FIELDS = {  # JSON 키 → (지표, 단위)
    "SALES": ("revenue", "억원"),
    "OP": ("op_income", "억원"),
    "NP": ("net_income", "억원"),
    "EPS": ("eps", "원"),
    "BPS": ("bps", "원"),
    "PER": ("per", "배"),
    "PBR": ("pbr", "배"),
    "ROE": ("roe", "%"),
    "EV": ("ev_ebitda", "배"),
}


class ConsensusUnavailable(Exception):
    pass


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        raise ConsensusUnavailable(f"WiseReport 접속 실패: {e}") from e


def parse_data(text):
    """c1050001_data.aspx 응답 → (추정치 목록, 실적 목록, 재무기준)"""
    try:
        rows = json.loads(text).get("JsonData") or []
    except ValueError as e:
        raise ConsensusUnavailable(f"응답이 JSON이 아님: {text[:80]!r}") from e
    est, act, basis = [], [], None
    for r in rows:
        m = re.match(r"(\d{4})\.(\d{2})\((A|E|P)\)", str(r.get("YYMM", "")))
        if not m:
            continue
        year, kind = int(m.group(1)), m.group(3)
        basis = r.get("MAIN") or basis
        vals = {}
        for k, (metric, unit) in FIELDS.items():
            v = _num(r.get(k))
            if v is not None:
                vals[metric] = (v, unit)
        if "revenue" in vals and "op_income" in vals and vals["revenue"][0]:
            vals["op_margin"] = (vals["op_income"][0] / vals["revenue"][0] * 100, "%")
        target = est if kind in ("E", "P") else act
        for metric, (v, unit) in vals.items():
            target.append({"target_year": year, "metric": metric, "value": v, "unit": unit,
                           "kind": "추정" if kind == "E" else ("잠정" if kind == "P" else "실적"),
                           "basis": basis})
    return est, act, basis


def parse_opinion(page):
    """기업개요 화면의 '투자의견 컨센서스' 표 → {opinion, target_price, eps, per, n}"""
    for t in re.findall(r"<table.*?</table>", page, re.S):
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
        if "추정기관수" in txt and "목표주가" in txt:
            nums = re.findall(r"-?[\d,]+(?:\.\d+)?", txt.split("추정기관수", 1)[1])
            vals = [_num(x) for x in nums]
            if len(vals) >= 5:
                return {"opinion": vals[0], "target_price": vals[1], "eps": vals[2], "per": vals[3], "n": int(vals[4])}
    return None


def fetch(code, as_of=None):
    """반환: (출처 URL, 추정치 목록). 추정치에는 추정기관 수(n_estimates)를 붙인다."""
    d = (as_of or dt.date.today()).strftime("%Y%m%d")
    url = DATA_URL.format(code=code, d=d)
    est, act, basis = parse_data(_get(url))
    if not est and not act:
        raise ConsensusUnavailable("WiseReport에 이 종목의 컨센서스 자료가 없음")
    op = None
    try:
        op = parse_opinion(_get(PAGE_URL.format(code=code)))
    except ConsensusUnavailable:
        pass
    if op:
        # 추정기관 수는 가장 가까운 추정 연도(보통 현재 연도)에 대한 값이다
        first = min((e["target_year"] for e in est), default=None)
        for e in est:
            if e["target_year"] == first:
                e["n_estimates"] = op["n"]
        if first is not None and op.get("target_price"):
            est.append({"target_year": first, "metric": "target_price", "value": op["target_price"], "unit": "원",
                        "kind": "추정", "basis": basis, "n_estimates": op["n"]})
    return url, est, act
