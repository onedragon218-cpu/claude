"""화면 Q「적정 PER 주가」 (지시서 2026-11-14 '화면 R'을 Q 자리로, 틀은 화면 P와 같게 — 사용자 지시)

한 줄: 실제 지배자본 + 보정 컨센서스 순이익으로 올해·내년·내후년 ROE를 이어 구하고, 그해 적정 PER
      = (1 − g/ROE) ÷ (Ke − g) (고든 정당화 PER, 다모다란·CFA 교재)에 보정 EPS를 곱해 연도별 적정 주가를 낸다.
      미래 시점 값은 Ke로 오늘까지 할인한 '오늘가치'로 지금 주가와 비교한다.
성질: ROE > Ke일 때만 성장이 PER을 올린다. ROE = Ke면 성장과 관계없이 PER = 1/Ke, ROE < Ke면 성장할수록 PER이 낮아진다.

같은 재료는 같은 값(새로 만들지 않음)
- 보정계수: standards.factors(화면 G·O·M·P와 같은 규칙: ×0.83, 그해 EPS 3개월 수정률 −5% 미만이면 ×0.73)
  '앱 보정계수' 토글 = calibration.factor(실측 중앙값)
- Ke: valuation._rf(그해 국고채 10년, 없는 해는 최근 값 — ECOS 자동 값 포함) + 베타 × ERP (화면 G·O와 같은 설정)
- 지배자본: facts acc_eq_owner(DART) · 배당금 지급: value5.face_by_year 의 재무제표 원본 'div'
- 컨센 순이익: WiseReport NP. 컨센 EPS × 주식수와 맞아(한솔제지 2026E 255.5억 ÷ 1,073원 ≈ 2,381만 주) 지배주주 기준으로 봄
- 자사주 소각은 반영하지 않는다.
리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님
"""
import datetime as dt

from . import db

DISCLAIMER = "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"
PRINCIPLE = ("ROE가 Ke보다 높을 때만 성장이 PER을 올립니다. ROE = Ke면 성장과 관계없이 PER = 1/Ke, "
             "ROE < Ke면 성장할수록 PER이 낮아집니다.")
DEFAULTS = {"g": 0.03, "g_상한": 0.04, "Ke_g_최소차": 0.02, "배당성향_대체값": 0.25, "가치경로_문턱": 0.10, "r_고정_참고": 0.10}


def settings():
    from .valuation import load_assumptions
    A = load_assumptions()
    s = dict(DEFAULTS, **{k: v for k, v in (A.get("적정PER") or {}).items() if not str(k).startswith("_")})
    return A, s


def clamp_g(g, s=None):
    s = s or DEFAULTS
    return min(max(float(g), 0.0), float(s["g_상한"]))


# ---------------------------------------------------------------- 공식
def fair_per(roe, ke, g, min_gap=0.02, ni=1.0):
    """반환: (적정 PER 또는 None, 판정 못 한 이유 또는 None)"""
    if roe is None or ni is None or ni <= 0:
        return None, "판정 못 함(적자 또는 순이익 컨센 없음)"
    if ke - g < min_gap:
        return None, f"판정 못 함(Ke − g가 {min_gap * 100:.0f}%p 미만 — 값이 폭발)"
    if roe <= g:
        return None, f"판정 못 함(ROE ≤ g — 성장할수록 가치 감소) · 참고 1/Ke = {1 / ke:.1f}"
    return (1 - g / roe) / (ke - g), None


# ---------------------------------------------------------------- 계산(화면 JS의 qCompute와 같은 식)
def compute(data, params=None):
    """data: inputs() 결과. params: g, fixed_r(bool), use_cal(bool). 반환: 행·판정·민감도"""
    _, S = settings()
    p = dict({"g": S["g"], "fixed_r": False, "use_cal": False}, **(params or {}))
    g = clamp_g(p["g"], S)
    today = dt.date.fromisoformat(data["today"])
    price = data.get("price")
    d = data["d"]
    e_start = data.get("E0")
    rows = []
    for yd in data["years"]:
        y = yd["y"]
        k = yd["k_cal"] if (p["use_cal"] and yd.get("k_cal") is not None) else yd["k"]
        ke = S["r_고정_참고"] if p["fixed_r"] else yd["ke"]
        ni = None if yd.get("ni_c") is None else yd["ni_c"] * k
        roe = e_end = None
        if e_start is not None and ni is not None:
            e_end = e_start + ni * (1 - d)
            avg = (e_start + e_end) / 2
            roe = ni / avg if avg > 0 else None
        per, why = fair_per(roe, ke, g, S["Ke_g_최소차"], ni)
        eps = None if yd.get("eps_c") is None else yd["eps_c"] * k
        fair = tv = None
        if per is not None:
            if eps is None or eps <= 0:
                why = "판정 못 함(적자 EPS)" if eps is not None else "판정 못 함(EPS 컨센 없음)"
            else:
                fair = per * eps
        t = max(0.0, (dt.date(y, 1, 1) - today).days / 365.25)
        if fair is not None:
            tv = fair / (1 + ke) ** t
        tags = []
        n = yd.get("n_est")
        wait = n is not None and n <= 1
        if wait:
            tags.append("판정 못 함(원천 대기)")
        elif n is None:
            tags.append("원천 수 미확인")
        if roe is not None and data.get("roe_hist_max") is not None and roe * 100 > data["roe_hist_max"]:
            tags.append("정점 넘음 — 사이클 정점이면 과대평가")
        if abs(k - 0.73) < 1e-9:
            tags.append("컨센 3개월 −5% 넘게 하향 — 깊은 보정")
        if data.get("financial"):
            tags.append("금융: 적정 PBR 기준 권장")
        rows.append({"y": y, "ke": ke, "g": g, "k": k, "ni": ni, "e_start": e_start, "e_end": e_end, "roe": roe,
                     "roe_c": yd.get("roe_c"), "payout": (1 - g / roe) if per is not None else None, "per": per,
                     "per0": 1 / ke, "mkt_per": (price / yd["eps_c"]) if price and yd.get("eps_c") and yd["eps_c"] > 0 else None,
                     "eps_c": yd.get("eps_c"), "eps": eps, "fair": fair, "t": t, "tv": tv,
                     "ratio": (price / tv) if price and tv else None, "n_est": n, "wait": wait, "why": why, "tags": tags})
        e_start = e_end                         # 다음 해 시작 자본(이 해를 못 구하면 이어지지 않음)
    ok = {r["y"]: r["tv"] for r in rows if r["tv"] is not None and not r["wait"]}
    cy = data["years"][0]["y"]
    return {"rows": rows, "path": _path(ok, cy, S["가치경로_문턱"]), "position": _position(price, ok, cy),
            "g": g, "params": p}


def _path(ok, cy, th):
    base = cy if cy in ok else (cy + 1 if cy + 1 in ok else None)
    if base is None or (cy + 2) not in ok or base == cy + 2:
        return {"label": "판정 못 함", "why": "비교할 두 해의 오늘가치가 없음(적자·컨센 없음·원천 대기)"}
    r = ok[cy + 2] / ok[base]
    lab = "가치 상승" if r >= 1 + th else ("평평" if r >= 1 - th else "가치 하락")
    return {"label": lab, "r": r, "why": f"{cy + 2}E 오늘가치 ÷ {base}E 오늘가치 = {r:.2f}"}


def _position(price, ok, cy):
    if not price:
        return {"label": "판정 못 함", "why": "지금 주가 없음"}
    if cy not in ok:
        return {"label": "판정 못 함", "why": f"{cy}E 오늘가치 없음"}
    x = price / ok[cy]
    if x <= 0.8:
        lab = "안전마진 20% 넘음"
    elif x <= 0.9:
        lab = "안전마진 10~20%"
    elif x <= 1.0:
        lab = "안전마진 10% 미만"
    else:
        lab = "올해 적정 위" + (" · 2년 뒤 적정까지 반영" if (cy + 2) in ok and price > ok[cy + 2] else "")
    return {"label": lab, "x": x, "why": f"지금 주가 ÷ {cy}E 오늘가치 = {x:.2f}"}


def sensitivity(data, params=None):
    """내년(cy+1) 오늘가치 — 행 g 2·3·4%, 열 Ke −1%p·기준·+1%p"""
    p = dict(params or {})
    _, S = settings()
    out = []
    for g in (0.02, 0.03, 0.04):
        row = []
        for dk in (-0.01, 0.0, 0.01):
            ys = [dict(y, ke=(S["r_고정_참고"] if p.get("fixed_r") else y["ke"]) + dk) for y in data["years"]]
            row.append(compute(dict(data, years=ys), dict(p, g=g, fixed_r=False))["rows"][1]["tv"])
        out.append(row)
    return out


# ---------------------------------------------------------------- 입력 모으기
def inputs(conn, code, P, A=None, cal=None, today=None, face=None):
    from . import calibration, standards, valuation, value5
    from .goodco import Data
    A = A or settings()[0]
    D = Data(conn, code, P)
    cy = P["current"]
    today = today or dt.date.today()
    # 시작 지배자본: 작년 사업보고서, 없으면 가장 최근 FY
    e0y = cy - 1 if D.v(cy - 1, "acc_eq_owner") is not None else max(
        [y for (y, per, m) in D.f if per == "FY" and m == "acc_eq_owner" and y < cy and D.f[(y, per, m)][0] is not None], default=None)
    E0 = D.eok(e0y, "acc_eq_owner") if e0y else None
    # 실제 배당성향: 최근 3년 배당금 지급 합 ÷ 지배순이익 합(0~1)
    if face is None:
        try:
            face = value5.face_by_year(code)
        except Exception:      # noqa: BLE001
            face = {}
    yrs = [y for y in range((e0y or cy - 1) - 2, (e0y or cy - 1) + 1)]
    divs = [(face.get(y) or {}).get("div") for y in yrs]
    nis = [D.v(y, "acc_ni_owner") for y in yrs]
    if all(x is not None for x in nis) and sum(nis) > 0 and any(x is not None for x in divs):
        d = min(max(sum(x or 0 for x in divs) / sum(nis), 0.0), 1.0)
        d_note = f"{yrs[0]}~{yrs[-1]} 배당금 지급 합 ÷ 지배순이익 합 = {d * 100:.1f}%" + \
            ("" if all(x is not None for x in divs) else " (배당 자료 빈 해는 0으로 봄)")
        d_assumed = False
    else:
        d = float(settings()[1]["배당성향_대체값"])
        d_note = f"자료 없음 또는 3년 지배순이익 합 ≤ 0 → 가정 {d * 100:.0f}%"
        d_assumed = True
    F = standards.factors(conn, code, P)
    beta, erp = A.get("베타", 1.0), A["시장위험프리미엄_ERP"]
    years = []
    for y in [cy] + list(P["future"]):
        rf, rf_note = valuation._rf(A, y)
        kc = None
        try:
            v, why = calibration.factor("eps", y, today=today, cal=cal, measured=True)
            kc = v if why and "보정 자료 없음" not in why else None
        except Exception:      # noqa: BLE001
            kc = None
        years.append({"y": y, "ni_c": D.c(y, "net_income"), "eps_c": D.c(y, "eps"), "roe_c": D.c(y, "roe"),
                      "k": F[(y, "eps")][0], "k_why": F[(y, "eps")][1], "k_cal": kc,
                      "ke": rf + beta * erp, "rf": rf, "rf_note": rf_note, "n_est": D.n_est(y)})
    pr = conn.execute("SELECT value, as_of FROM facts WHERE stock_code=? AND fiscal_year=? AND period='ASOF' "
                      "AND metric='price_close' AND is_current=1", (code, cy)).fetchone()
    hist = [D.v(y, "roe") for y in range(cy - 10, cy)]
    hist = [x for x in hist if x is not None]
    rev = [y for y in D.years if (D.eok(y, "acc_revenue") or 0) > 0]
    return {"today": today.isoformat(), "E0": E0, "E0_year": e0y, "d": d, "d_note": d_note, "d_assumed": d_assumed,
            "years": years, "price": pr["value"] if pr else None, "price_date": pr["as_of"] if pr else None,
            "roe_hist_max": max(hist) if hist else None, "financial": len(rev) < 3,
            "beta": beta, "erp": erp, "cons_asof": D.cons_asof()}


def payload(conn, code, P):
    A, S = settings()
    from . import calibration
    cal = calibration.load()
    today = dt.date.today()
    out_rows = []
    for c in conn.execute("SELECT c.stock_code, c.name FROM companies c WHERE EXISTS "
                          "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code) ORDER BY c.name"):
        try:
            data = inputs(conn, c["stock_code"], P, A, cal, today)
            out_rows.append({"code": c["stock_code"], "name": c["name"], "data": data})
        except Exception as e:     # noqa: BLE001  한 기업이 실패해도 목록은 살린다
            out_rows.append({"code": c["stock_code"], "name": c["name"], "error": f"{type(e).__name__}: {e}"})
    cur = next((r for r in out_rows if r["code"] == code and "data" in r), None)
    check = None
    if cur:
        res = compute(cur["data"])
        check = {"rows": [{k: r[k] for k in ("y", "per", "tv", "roe")} for r in res["rows"]],
                 "path": res["path"]["label"], "position": res["position"]["label"]}
    return {"settings": S, "years": [P["current"]] + list(P["future"]), "rows": out_rows, "code": code,
            "check": check, "principle": PRINCIPLE, "disclaimer": DISCLAIMER}
