"""FnGuide 기업정보(공개 페이지) 컨센서스 화면에서 연간 추정치 평균을 읽는다.

- 2026-11 FnGuide 개편: 옛 주소(comp.fnguide.com/SVO2/ASP/SVD_Main.asp)는 모든 회사가 '페이지 없음'으로 넘어감.
  새 주소 wcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd={코드}(gicode는 무시되고 기본 회사가 나옴) — 표 값이 페이지 안 JSON(perforTrend)으로 들어 있음
  (머리글 VAL1~6 = 실적 3년 + 추정 3년, EP_CHK='E'가 추정). 단위: 금액 억원, EPS·BPS 원.
- 영업이익률 = 영업이익 ÷ 매출, ROE = 지배순이익 ÷ 평균 지배자본(전년 말·그해 말), 부채비율 = 부채 ÷ 자본(그해 말) — 화면 값이 아니라 계산.
- 로그인·유료 화면은 쓰지 않는다. 막히면 '미확보'로 기록하고 끝낸다.
"""
import json
import re
import urllib.request

UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
URL = "https://wcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd={code}"   # gicode는 무시되고 삼성전자가 나옴 — cmp_cd로 지정

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


ROWS_JSON = {"매출액": "rev", "영업이익": "op", "당기순이익(지배)": "ni", "자본총계(지배)": "eq", "자본총계": "eq_all", "부채총계": "debt",
             "EPS": "eps", "BPS": "bps"}


def _num(v):
    try:
        return None if v in (None, "") else float(str(v).replace(",", ""))
    except ValueError:
        return None


def parse(page):
    """새 화면의 perforTrend JSON → [{target_year, metric, value, unit, kind}]"""
    m = re.search(r"perforTrend\s*:\s*(\{.*?\})\s*,\s*\n\s*perforTrendChart", page, re.S)
    if not m:
        raise ConsensusUnavailable("컨센서스 표(perforTrend)를 찾지 못했습니다.")
    try:
        d = json.loads(m.group(1))
    except ValueError as e:
        raise ConsensusUnavailable(f"컨센서스 표를 읽지 못했습니다: {e}") from None
    head = d.get("header") or []
    rows = {}
    for r in d.get("data") or []:
        key = ROWS_JSON.get(str(r.get("NAME") or "").strip())
        if key and r.get("LVL") == 1 and key not in rows:
            rows[key] = r
    cols = [(h.get("CD"), int(str(h.get("YYMM"))[:4]), str(h.get("EP_CHK") or "").strip()) for h in head
            if re.match(r"20\d\d/12", str(h.get("YYMM") or ""))]
    g = lambda k, cd: _num((rows.get(k) or {}).get(cd))
    out = []
    for i, (cd, year, ep) in enumerate(cols):
        if ep not in ("E", "P"):
            continue                 # 실적 열은 건너뛴다(실적은 DART에서 가져옴)
        kind = "잠정" if ep == "P" else "추정"
        rev, op, ni, eq, eqa, debt = g("rev", cd), g("op", cd), g("ni", cd), g("eq", cd), g("eq_all", cd), g("debt", cd)
        prev_eq = g("eq", cols[i - 1][0]) if i > 0 else None
        vals = {"revenue": (rev, "억원"), "op_income": (op, "억원"), "eps": (g("eps", cd), "원"), "bps": (g("bps", cd), "원"),
                "op_margin": (op / rev * 100 if op is not None and rev else None, "%"),
                "roe": (ni / ((eq + prev_eq) / 2) * 100 if ni is not None and eq and prev_eq else
                        (ni / eq * 100 if ni is not None and eq else None), "%"),
                "debt_ratio": (debt / eqa * 100 if debt is not None and eqa else None, "%")}
        for metric, (v, unit) in vals.items():
            if v is not None:
                out.append({"target_year": year, "metric": metric, "value": round(v, 4), "unit": unit, "kind": kind})
    if not out:
        raise ConsensusUnavailable("표는 읽었지만 추정치(E) 값이 없음")
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
    m = re.search(r"cmp_cd:\s*'([0-9A-Z]+)'", page)
    if m and m.group(1) != code:      # 다른 회사 화면이 오면 그 값을 넣지 않음(잘못된 컨센서스 방지)
        raise ConsensusUnavailable(f"FnGuide가 다른 회사({m.group(1)}) 화면을 돌려줌")
    return url, parse(page)


# ---------------------------------------------------------------- Snapshot 'Financial Highlight' 연간(ROE·ROA 실적·추정) — 2026-11-25
HIGHLIGHT_URL = "https://wcomp.fnguide.com/CompanyInfo/getSnpFinancial"


def fetch_highlight(code):
    """FnGuide 기업정보 Snapshot → Financial Highlight → '연간'(실적 5년 + 추정 3년)의 ROE·ROA.
    FnGuide 정의: ROA = 당기순이익 ÷ 평균 자산총계, ROE = 지배순이익 ÷ 평균 지배자본(앱 계산과 같음).
    반환: (주소, 추정치 목록[{target_year, metric('roe_fg'|'roa'|'revenue_fg'|'op_income_fg'|'eps_fg'), value, unit, kind}], 실적 {연도: {'roa','roe_fg'}})
    revenue_fg·op_income_fg·eps_fg(2026-11-26): WiseReport 컨센서스와 비교해 차이가 크면 '추정 불확실' 신호용(계산에는 안 씀)"""
    import urllib.parse
    last_err = None
    for consol in ("C", "D"):                 # 연결 → 없으면 별도
        url = HIGHLIGHT_URL + "?" + urllib.parse.urlencode({"cmp_cd": code, "consol_typ": consol, "freq_typ": "Y"})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                                                   "Referer": f"https://wcomp.fnguide.com/CompanyInfo/Snapshot?cmp_cd={code}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:      # noqa: BLE001
            last_err = e
            continue
        ds = d.get("dataset") or d
        rows = {str(x.get("NAME") or "").strip(): x for x in ds.get("data") or []}
        if "ROA" not in rows and "ROE" not in rows:
            continue
        est, act = [], {}
        for h in ds.get("header") or []:
            m = re.match(r"(20\d\d)/12", str(h.get("YYMM") or ""))
            if not m:
                continue
            y, cd, e = int(m.group(1)), h.get("CD"), str(h.get("EP_CHK") or "").strip()
            for name, metric in (("ROA", "roa"), ("ROE", "roe_fg"), ("매출액", "revenue_fg"), ("영업이익(발표기준)", "op_income_fg"),
                                 ("영업이익", "op_income_fg"), ("EPS", "eps_fg")):
                if metric == "op_income_fg" and name == "영업이익" and _num((rows.get("영업이익(발표기준)") or {}).get(cd)) is not None:
                    continue                      # 발표기준 값이 있으면 그것(추정 칸은 '영업이익'이 비고 발표기준만 있는 경우가 많음)
                v = _num((rows.get(name) or {}).get(cd))
                if v is None:
                    continue
                if e in ("E", "P"):
                    unit = "%" if metric in ("roa", "roe_fg") else ("원" if metric == "eps_fg" else "억원")
                    est.append({"target_year": y, "metric": metric, "value": v, "unit": unit, "kind": "잠정" if e == "P" else "추정",
                                "basis": "연결" if consol == "C" else "별도"})
                elif metric in ("roa", "roe_fg"):
                    act.setdefault(y, {})[metric] = v
        return url, est, act
    raise ConsensusUnavailable(f"FnGuide Financial Highlight를 읽지 못함: {last_err}" if last_err else "FnGuide Financial Highlight에 ROE·ROA 줄이 없음")
