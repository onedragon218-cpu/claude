"""WiseReport(네이버 증권 기업정보 화면 제공처, FnGuide 계열 자료) 기업개요 공개 페이지.

공개 페이지 한 장(c1010001)을 일반 조회로 읽고, 화면에 인쇄된 값만 해석한다.
- 현재가·시가총액·발행주식수 (직전 거래일 기준)
- 직전 연도 실적(A)과 올해 컨센서스(E)의 EPS·BPS, 추정 기관 수, 목표주가
로그인·차단 우회·내부 호출 흉내는 하지 않는다. 그래서 2년 뒤까지의 연도별 컨센서스 표는
이 경로로는 얻지 못한다(그 표는 페이지가 별도 내부 호출로 불러온다).
"""
import glob, gzip, os, re
from datetime import date

URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
SOURCE = "WiseReport 기업개요(FnGuide 컨센서스)"


def raw_dir(root):
    return os.path.join(root, "data", "raw", "wisereport")


def latest_raw(root, code):
    files = sorted(glob.glob(os.path.join(raw_dir(root), f"{code}_c1010001_*.html.gz")))
    return files[-1] if files else None


def fetch(root, code, today=None, rawdir=None):
    """공개 페이지를 한 번 받아 보관한다. 반환: (경로 또는 None, 설명)"""
    import requests
    today = today or date.today().strftime("%Y%m%d")
    try:
        r = requests.get(URL.format(code=code), headers={"User-Agent": "fintrack/0.2 (personal research tool; Windows)"}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return None, f"조회 실패: {type(e).__name__}: {e}"[:300]
    p = os.path.join(rawdir or raw_dir(root), f"{code}_c1010001_{today}.html.gz")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(r.text)
    return p, "공개 페이지 1회 조회 후 보관"


def _n(s):
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse(path):
    from bs4 import BeautifulSoup
    html = gzip.open(path, "rt", encoding="utf-8", errors="ignore").read()
    soup = BeautifulSoup(html, "lxml")
    text = {i: t.get_text(" ", strip=True) for i, t in enumerate(soup.find_all("table"))}
    out = {"raw_file": os.path.basename(path)}
    dates = re.findall(r"(20\d\d)\.(\d\d)\.(\d\d)", html)
    if dates:  # 페이지에 가장 많이 찍힌 날짜 = 시세 기준일
        from collections import Counter
        y, m, d = Counter(dates).most_common(1)[0][0]
        out["as_of"] = f"{y}-{m}-{d}"
    for t in text.values():
        if "시가총액" in t and "발행주식수" in t:
            m = re.search(r"주가/전일대비/수익률\s*([\d,]+)\s*원", t)
            if m: out["price"] = _n(m.group(1))
            m = re.search(r"시가총액\s*([\d,]+)\s*억원", t)
            if m: out["market_cap_eok"] = _n(m.group(1))
            m = re.search(r"발행주식수/유동비율\s*([\d,]+)\s*주", t)
            if m: out["shares"] = _n(m.group(1))
        if "주요지표" in t and "(E)" in t and "EPS" in t:
            m = re.search(r"(\d{4})/12\(A\)\s*(\d{4})/12\(E\)", t)
            if m: out["year_a"], out["year_e"] = int(m.group(1)), int(m.group(2))
            m = re.search(r"EPS\s*(-?[\d,]+)원\s*(-?[\d,]+)원", t)
            if m: out["eps_a"], out["eps_e"] = _n(m.group(1)), _n(m.group(2))
            m = re.search(r"BPS\s*(-?[\d,]+)원\s*(-?[\d,]+)원", t)
            if m: out["bps_a"], out["bps_e"] = _n(m.group(1)), _n(m.group(2))
            m = re.search(r"회계기준\s*(\S+)\s*(\S+)", t)
            if m: out["basis_e"] = "CFS" if m.group(2) == "연결" else "OFS"
        if t.startswith("투자의견컨센서스"):
            nums = re.findall(r"-?[\d,]+(?:\.\d+)?", t.split("추정기관수", 1)[-1])
            if len(nums) >= 5:
                out["opinion"], out["target_price"], out["eps_e_check"], out["per_e"], out["n_estimates"] = [_n(x) for x in nums[:5]]
    return out
