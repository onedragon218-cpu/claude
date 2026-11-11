"""화면 P「우상향 저평가」 (사용자 지시 2026-11-11)

1) 우상향: 작년(실적) < 올해 < 내년 < 내후년(컨센서스)인 종목. 지표(영업이익률·ROE·매출액·EPS)는 화면에서 고르고 뺄 수 있다.
   - 작년 값은 DART 실적(앱 계산), 올해~내후년은 컨센서스 원래 값 그대로.
   - 최소 증가폭은 선택 입력(매출·EPS는 %, 영업이익률·ROE는 %p). 비우면 조금이라도 오르면 통과.
   - 작년이 적자(0 이하)였다가 오른 경우도 우상향(턴어라운드)으로 본다. 이때 %로 재는 문턱은 적용하지 않는다.
   - 고른 지표의 컨센서스가 하나라도 없으면 제외.
2) 가격 판정: 적정가 = ROE(%) × 1/10 × 보정(앱 공통 규칙 ×0.83, 급하향 ×0.73, 기본 켬) × BPS(컨센서스 그해 값) > 현재 주가
   1차 = 내후년 기준, 2차 = 내년 기준, 3차 = 올해 기준. 화면에는 '몇 년 뒤 기준부터 싼가' 한 칸과 안전 여유(적정가 ÷ 주가 − 1)를 함께 보여 준다.
리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님
"""
from . import db

METRICS = {"op_margin": ("영업이익률", "%p"), "roe": ("ROE", "%p"), "revenue": ("매출액", "%"), "eps": ("EPS", "%")}
FACTOR = 0.83
TIERS = [(2, "1차", "내후년"), (1, "2차", "내년"), (0, "3차", "올해")]   # (올해로부터 몇 년 뒤, 이름, 기준)


def rows(conn, P):
    cy = P["current"]
    years = [cy - 1, cy, cy + 1, cy + 2]
    out = []
    for c in conn.execute("SELECT c.stock_code, c.name FROM companies c WHERE EXISTS "
                          "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code) ORDER BY c.name"):
        code = c["stock_code"]
        fv = lambda y, k, per="FY": (lambda r: None if r is None else r["value"])(db.current_fact(conn, code, y, k, per))
        cons = {(r["target_year"], r["metric"]): dict(r) for r in db.latest_consensus(conn, code)}
        cv = lambda y, k: (cons.get((y, k)) or {"value": None})["value"]
        series = {}
        for m in METRICS:
            vals = [fv(cy - 1, m)]
            for y in years[1:]:
                if m == "op_margin":
                    v = cv(y, "op_margin")
                    if v is None and cv(y, "op_income") is not None and cv(y, "revenue"):
                        v = cv(y, "op_income") / cv(y, "revenue") * 100
                    vals.append(v)
                else:
                    vals.append(cv(y, m))
            series[m] = vals
        asof = max(((cons.get((y, "roe")) or {}).get("as_of") or "" for y in years[1:]), default="") or None
        from . import standards
        Fx = standards.factors(conn, code, P)
        out.append({"code": code, "name": c["name"], "price": fv(cy, "price_close", "ASOF"),
                    "f": [Fx[(y, "eps")][0] for y in years[1:]], "f_why": [Fx[(y, "eps")][1] for y in years[1:]],
                    "series": series, "roe": [cv(y, "roe") for y in years[1:]], "bps": [cv(y, "bps") for y in years[1:]],
                    "as_of": asof})
    return {"years": years, "rows": out, "factor": FACTOR, "metrics": METRICS,
            "disclaimer": "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"}


def rising(vals, kind, th=None):
    """vals = [작년, 올해, 내년, 내후년]. kind '%'면 증가율, '%p'면 증가폭으로 문턱 비교. 반환: (통과?, 사유)"""
    if any(v is None for v in vals):
        return None, "컨센서스 없음"
    for a, b in zip(vals, vals[1:]):
        if not b > a:
            return False, "우상향 아님"
        if th:
            if kind == "%":
                if a > 0 and (b / a - 1) * 100 < th:      # 적자 → 흑자(턴어라운드)는 증가율 문턱 적용 안 함
                    return False, f"증가율 {th}% 미만 구간 있음"
            elif b - a < th:
                return False, f"증가폭 {th}%p 미만 구간 있음"
    return True, None


def fair_prices(row, factor=True):
    """적정가 = ROE × 1/10 × 보정 × BPS. 보정은 앱 공통 규칙(EPS 기준: ×0.83, 3개월 사이 5% 넘게 하향이면 ×0.73)"""
    fs = row.get("f") or [FACTOR] * 3
    return [None if r is None or b is None else r * 0.1 * (f if factor else 1.0) * b for r, b, f in zip(row["roe"], row["bps"], fs)]


def classify(row, use=("op_margin", "roe", "revenue", "eps"), th=None, factor=True):
    """반환: {'status': '통과'|'우상향 아님'|'제외', 'tier': 0(올해부터)|1|2|None, 'fair': [...], 'margin': [...]}"""
    th = th or {}
    for m in use:
        ok, why = rising(row["series"][m], METRICS[m][1], th.get(m))
        if ok is None:
            return {"status": "제외", "why": f"{METRICS[m][0]} {why}"}
        if not ok:
            return {"status": "우상향 아님", "why": f"{METRICS[m][0]} {why}"}
    fair = fair_prices(row, factor)
    if row.get("price") is None or any(v is None for v in fair):
        return {"status": "제외", "why": "주가 또는 ROE·BPS 컨센서스 없음"}
    margin = [v / row["price"] - 1 for v in fair]
    tier = next((i for i in (0, 1, 2) if fair[i] > row["price"]), None)
    return {"status": "통과", "tier": tier, "fair": fair, "margin": margin, "cheap": [v > row["price"] for v in fair]}
