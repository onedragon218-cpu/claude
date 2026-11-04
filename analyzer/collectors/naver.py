"""네이버 금융 종목 메인 화면(공개 페이지)의 '기업실적분석' 표에서 컨센서스를 읽는다.

- 표의 연간 열 중 머리글에 (E)가 붙은 열이 증권사 추정치(컨센서스, 제공: FnGuide/WISEfn)다.
- 보통 현재 연도 1개 열만 (E)이고, 다음 연도가 함께 나오기도 한다. 3년 치는 FnGuide 쪽이 더 많다.
- 로그인·유료 화면은 쓰지 않는다. 실패하면 '미확보'로 기록한다.
- 주의: 이 개발 환경에서는 네이버 접속이 막혀 있어, 실제 화면 구조를 본떠 만든 표로만 시험했다.
"""
import html
import re
import urllib.request

UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
URL = "https://finance.naver.com/item/main.naver?code={code}"

ROWS = {  # 행 이름(공백·괄호 제거) → (지표, 단위)
    "매출액": ("revenue", "억원"),
    "영업이익": ("op_income", "억원"),
    "당기순이익": ("net_income", "억원"),
    "영업이익률": ("op_margin", "%"),
    "ROE": ("roe", "%"),
    "ROE지배주주": ("roe", "%"),
    "부채비율": ("debt_ratio", "%"),
    "EPS": ("eps", "원"),
    "BPS": ("bps", "원"),
}


class ConsensusUnavailable(Exception):
    pass


def _cells(row_html):
    out = []
    for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S):
        out.append(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip())
    return out


def parse(page):
    m = re.search(r'class="section cop_analysis".*?<table[^>]*>(.*?)</table>', page, re.S)
    if not m:
        raise ConsensusUnavailable("네이버 '기업실적분석' 표를 찾지 못했습니다.")
    tbl = m.group(1)
    head = re.search(r"<thead>(.*?)</thead>", tbl, re.S)
    body = re.search(r"<tbody>(.*?)</tbody>", tbl, re.S)
    if not head or not body:
        raise ConsensusUnavailable("표 머리글/본문을 읽지 못했습니다.")
    # 날짜 머리글 줄: 'YYYY.MM' 이 여러 개 있는 줄. 앞쪽 연속 구간이 연간, 뒤쪽이 분기
    dates = None
    for tr in re.findall(r"<tr.*?</tr>", head.group(1), re.S):
        c = _cells(tr)
        if sum(1 for x in c if re.search(r"20\d\d\.\d\d", x)) >= 3:
            dates = c
            break
    if dates is None:
        raise ConsensusUnavailable("연도 머리글을 찾지 못했습니다.")
    annual = []  # (열 위치, 연도, 추정 여부)
    last_month = None
    for i, h in enumerate(dates):
        mt = re.search(r"(20\d\d)\.(\d\d)", h)
        if not mt:
            continue
        y, mo = int(mt.group(1)), mt.group(2)
        if annual and (y <= annual[-1][1]):   # 연도가 다시 작아지면 분기 구간 시작
            break
        if last_month and mo != last_month:
            break
        last_month = mo
        annual.append((i, y, "(E)" in h.replace(" ", "")))
    out = []
    for tr in re.findall(r"<tr.*?</tr>", body.group(1), re.S):
        c = _cells(tr)
        if not c:
            continue
        label = re.sub(r"[\s()（）]", "", c[0])
        label = re.sub(r"(원|배|%)$", "", label)
        if label not in ROWS:
            continue
        metric, unit = ROWS[label]
        vals = c[1:]
        for pos, (i, y, est) in enumerate(annual):
            if not est or pos >= len(vals):
                continue
            v = vals[pos].replace(",", "").strip()
            try:
                num = float(v)
            except ValueError:
                continue
            out.append({"target_year": y, "metric": metric, "value": num, "unit": unit, "kind": "추정"})
    return out


def fetch(code):
    url = URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            cs = r.headers.get_content_charset() or "euc-kr"
    except Exception as e:
        raise ConsensusUnavailable(f"네이버 접속 실패: {e}") from e
    page = raw.decode(cs if cs.lower() not in ("ks_c_5601-1987",) else "cp949", "replace")
    return url, parse(page)
