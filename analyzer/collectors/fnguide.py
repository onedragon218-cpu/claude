"""FnGuide 기업정보 메인 화면(공개 페이지)의 'Financial Highlight(연결, 연간)' 표에서 컨센서스 평균을 읽는다.

- 로그인·유료 화면은 쓰지 않는다. 공개 페이지가 막히면 '미확보'로 기록하고 끝낸다.
- 이 표는 증권사 추정치의 평균이며 증권사별 값과 추정기관 수는 없다(그 칸은 공란).
- 주의: 이 개발 환경에서는 FnGuide 접속이 막혀 있어 실제 페이지로 시험하지 못했다.
  GitHub Actions에서 처음 실행할 때 결과를 꼭 확인할 것.
"""
import html
import re
import urllib.request

UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
URL = "https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp?pGB=1&gicode=A{code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=101&stkGb=701"

ROWS = {  # 표의 행 이름 → (지표, 단위)
    "매출액": ("revenue", "억원"),
    "영업이익": ("op_income", "억원"),
    "영업이익률": ("op_margin", "%"),
    "EPS": ("eps", "원"),
    "BPS": ("bps", "원"),
    "ROE": ("roe", "%"),
    "부채비율": ("debt_ratio", "%"),
}


class ConsensusUnavailable(Exception):
    pass


def _cells(row_html):
    cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S)
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in cells]


def parse(page):
    m = re.search(r'id="highlight_D_Y".*?<table.*?>(.*?)</table>', page, re.S)
    if not m:
        raise ConsensusUnavailable("Financial Highlight(연결/연간) 표를 찾지 못했습니다.")
    rows = re.findall(r"<tr.*?</tr>", m.group(1), re.S)
    header = None
    out = []
    for r in rows:
        c = _cells(r)
        if header is None:
            if sum(1 for x in c if re.search(r"20\d\d/\d\d", x)) >= 3:
                header = c
            continue
        label = re.sub(r"\(.*?\)", "", c[0]).replace(" ", "") if c else ""
        if label not in ROWS:
            continue
        metric, unit = ROWS[label]
        vals = c[1:]
        hdr = [h for h in header if re.search(r"20\d\d/\d\d", h)]
        for h, v in zip(hdr, vals[-len(hdr):]):
            if "(E)" not in h and "(P)" not in h:
                continue             # 실적 열은 건너뛴다(실적은 DART에서 가져옴)
            year = int(re.search(r"(20\d\d)/", h).group(1))
            try:
                num = float(v.replace(",", ""))
            except ValueError:
                continue
            out.append({"target_year": year, "metric": metric, "value": num, "unit": unit,
                        "kind": "잠정" if "(P)" in h else "추정"})
    if header is None:
        raise ConsensusUnavailable("표 머리글(연도)을 읽지 못했습니다.")
    return out


def fetch(code):
    url = URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            final = r.geturl()
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        raise ConsensusUnavailable(f"FnGuide 접속 실패: {e}") from e
    if "Error" in final or "NotFound" in final:
        raise ConsensusUnavailable(f"FnGuide가 오류 페이지로 보냈습니다: {final}")
    return url, parse(page)
