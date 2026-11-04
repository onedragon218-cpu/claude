"""FnGuide 기업 메인 화면(Snapshot) 수집·해석.

브라우저로 보는 공개 화면 한 장(SVD_Main)을 일반 조회로 받는다. 로그인·차단 우회는 하지 않는다.
읽는 곳
- Financial Highlight(연결·연간): 최근 확정 실적(A)과 향후 3년 전망(E). 매출액·영업이익·지배순이익·자본·부채·EPS·BPS·ROE·부채비율
- 시세현황: 종가·시가총액·발행주식수, 기준일
- 컨센서스 요약: 추정기관수, 목표주가
조회는 '갱신' 때만, 종목당 한 장. 받은 원본은 작업 폴더 raw/fnguide/에 날짜별로 보관한다.
화면 구조가 바뀌어 읽지 못하면 원본은 남기고 '미확보'로 돌려준다.
"""
import gzip, os, re
from collections import Counter
from datetime import date

SOURCE = "FnGuide 컨센서스"
URL = ("https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp?pGB=1&gicode=A{code}"
       "&cID=&MenuYn=Y&ReportGB=&NewMenuID=101&stkGb=701")
DEFAULT_UA = "fintrack/0.2 (personal research tool; Windows)"

ROWS = {  # 화면 행 이름(공백 제거) → 항목
    "매출액": "revenue", "영업수익": "revenue", "순영업수익": "revenue",
    "영업이익": "op", "당기순이익": "ni", "지배주주순이익": "ni_owner",
    "자산총계": "assets", "부채총계": "liab", "자본총계": "eq", "지배주주지분": "eq_owner", "자본금": "issued_capital",
    "부채비율": "debt_ratio", "영업이익률": "op_margin", "ROE": "roe",
    "EPS": "eps", "BPS": "bps", "DPS": "dps", "PER": "per", "PBR": "pbr", "발행주식수": "shares",
}
AMOUNT = {"revenue", "op", "ni", "ni_owner", "assets", "liab", "eq", "eq_owner", "issued_capital"}  # 억원


def raw_path(w, code, day=None):
    day = day or date.today().strftime("%Y%m%d")
    return os.path.join(w.rawdir("fnguide"), f"{code}_SVD_Main_{day}.html.gz")


def fetch(w, code, user_agent=None):
    """반환: (경로 또는 None, 설명)"""
    import requests
    p = raw_path(w, code)
    if os.path.exists(p):
        return p, "오늘 받은 원본 재사용"
    try:
        r = requests.get(URL.format(code=code), headers={"User-Agent": user_agent or DEFAULT_UA}, timeout=30, allow_redirects=False)
    except Exception as e:
        return None, f"조회 실패: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"FnGuide 응답 {r.status_code}" + (" (오류 페이지로 이동)" if r.is_redirect else "")
    r.encoding = r.apparent_encoding or "utf-8"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(r.text)
    return p, "공개 화면 1회 조회 후 보관"


def _num(s):
    s = (s or "").replace(",", "").replace("\xa0", "").strip()
    if s in ("", "-", "N/A", "NA", "완전잠식"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _label(th):
    dt = th.find("dt")
    t = (dt or th).get_text(" ", strip=True)
    t = re.sub(r"\s", "", t)
    t = re.sub(r"\(.*?\)", "", t)          # (원) (보통주) (발표기준) 같은 괄호 제거
    return t


def parse(path):
    from bs4 import BeautifulSoup
    html = gzip.open(path, "rt", encoding="utf-8", errors="ignore").read()
    soup = BeautifulSoup(html, "lxml")
    out = {"raw_file": os.path.basename(path), "years": {}, "market": {}}

    m = re.search(r"기준\s*[:：]?\s*(20\d\d)[./-](\d\d)[./-](\d\d)", html)
    if m:
        out["as_of"] = "-".join(m.groups())
    else:
        ds = re.findall(r"(20\d\d)[./](\d\d)[./](\d\d)", html)
        if ds:
            out["as_of"] = "-".join(Counter(ds).most_common(1)[0][0])

    box, basis = None, None
    for div_id, b in (("highlight_D_Y", "CFS"), ("highlight_B_Y", "OFS")):
        box = soup.find(id=div_id)
        if box and box.find("table"):
            basis = b
            break
    if not box:
        out["error"] = "Financial Highlight 표를 찾지 못함(화면 구조 변경 가능)"
        return out
    out["basis"] = basis
    unit_txt = box.get_text(" ", strip=True)[:400] + " " + (box.find_previous(string=re.compile("단위")) or "")
    share_mult = 1000 if "천주" in unit_txt else 1
    table = box.find("table")

    cols = []
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        ys = [re.search(r"(20\d\d)/(\d\d)\s*(\((E|P)\))?", th.get_text(" ", strip=True)) for th in ths]
        if sum(1 for y in ys if y) >= 3:
            cols = [(int(y.group(1)), y.group(4) or "A") for y in ys if y]
            break
    if not cols:
        out["error"] = "연도 머리글을 찾지 못함"
        return out

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        th = tr.find("th")
        if not th:
            continue
        key = ROWS.get(_label(th))
        if not key:
            continue
        tds = tr.find_all("td")
        vals = []
        for td in tds:
            t = td.get("title") if _num(td.get("title")) is not None else td.get_text(" ", strip=True)
            vals.append(_num(t))
        for (y, kind), v in zip(cols, vals):
            if v is None:
                continue
            yr = out["years"].setdefault(y, {"kind": kind})
            if key in yr:          # 같은 이름 행이 두 번(예: 발표기준)이면 첫 행 유지
                continue
            if key == "shares":
                v *= share_mult
            yr[key] = v

    for t in soup.find_all("table"):
        tx = t.get_text(" ", strip=True)
        if "시가총액" in tx and ("종가" in tx or "발행주식수" in tx) and "price" not in out["market"]:
            m = re.search(r"종가\s*/?\s*전일대비\s*([\d,]+)", tx)
            if m: out["market"]["price"] = _num(m.group(1))
            m = re.search(r"시가총액[^\d]{0,30}?(\d[\d,]*)", tx)
            if m: out["market"]["market_cap_eok"] = _num(m.group(1))
            m = re.search(r"발행주식수[^\d]{0,30}?(\d[\d,]*)", tx)
            if m: out["market"]["shares_common"] = _num(m.group(1))
        if "추정기관수" in tx and "n_estimates" not in out:
            nums = re.findall(r"-?[\d,]+(?:\.\d+)?", tx.split("추정기관수", 1)[-1])
            if len(nums) >= 5:
                out["opinion"], out["target_price"], out["eps_fy1"], out["per_fy1"], out["n_estimates"] = [_num(x) for x in nums[:5]]
    if not out["years"]:
        out["error"] = "표에서 값을 읽지 못함"
    return out
