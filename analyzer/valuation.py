"""가치평가: S-RIM, DCF(FCFF), 상대가치(PER·PBR·PSR), ROIC, WACC.

원칙
- 연도 y의 가치는 그 연도 말까지 알 수 있던 자료만 쓴다(상대가치 적정 배수도 y까지의 누적 중앙값).
- 계산할 수 없으면 빈칸으로 두고 이유를 적는다. 추정으로 채우지 않는다.
- 가정은 analyzer/assumptions.json 한 곳에서 관리한다.
- 결과는 저장된 재무값으로 언제든 다시 계산되므로 valuations 테이블에 최신 결과만 둔다.
"""
import json
import os
import statistics

from . import db

ASSUMP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assumptions.json")

SCHEMA = """CREATE TABLE IF NOT EXISTS valuations (
  stock_code TEXT, base_year INTEGER, computed_at TEXT, assumptions TEXT, rows TEXT,
  PRIMARY KEY (stock_code, base_year))"""

METHODS = [("srim", "S-RIM"), ("dcf", "DCF(FCFF)"), ("per", "PER 가치"), ("pbr", "PBR 가치"), ("psr", "PSR 가치")]


def load_assumptions():
    with open(ASSUMP_PATH, encoding="utf-8") as f:
        return json.load(f)


def _rf(A, y):
    t = {int(k): v / 100 for k, v in A["무위험수익률_국고채10년"].items() if not k.startswith("_")}
    if y in t:
        return t[y], None
    past = [k for k in t if k <= y]
    k = max(past) if past else min(t)
    return t[k], f"{y}년 국고채 금리 없음 → {k}년 값 사용"


def wavg3(vals):
    pairs = [(v, w) for v, w in zip(vals, (3, 2, 1)) if v is not None]
    if not pairs:
        return None
    return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)


class Book:
    """facts에서 연도·기간별 값을 꺼내는 도우미. 최근 12개월(TTM) 합성도 한다."""

    def __init__(self, conn, code):
        self.rows = {}
        for r in conn.execute("SELECT * FROM facts WHERE stock_code=? AND is_current=1", (code,)):
            self.rows[(r["fiscal_year"], r["period"], r["metric"])] = r

    def v(self, y, k, per="FY"):
        r = self.rows.get((y, per, k))
        return None if r is None else r["value"]

    def ok(self, y, k, per="FY"):
        r = self.rows.get((y, per, k))
        return r is not None and r["status"] in ("VERIFIED", "CORRECTED")

    def latest_interim(self, y):
        for per in ("Q3", "H1", "Q1"):
            if (y, per, "acc_eq_owner") in self.rows:
                return per
        return None

    def ttm(self, y, k, per):
        """최근 12개월 = 전년 연간 + 올해 누적 − 전년 같은 기간 누적"""
        a, b, c = self.v(y - 1, k), self.v(y, k, per), self.v(y - 1, k, per)
        if None in (a, b, c):
            return None
        return a + b - c


def _tax_rate(A, pretax, tax, notes):
    if pretax and pretax > 0 and tax is not None and 0 <= tax / pretax <= 0.5:
        return tax / pretax
    notes.append(f"유효세율 비정상(세전이익 {_eok(pretax)}, 법인세 {_eok(tax)}) → 대체세율 {A['대체세율']:.0%}")
    return A["대체세율"]


def _eok(x):
    return "–" if x is None else f"{x / 1e8:,.0f}억"


def _missing(notes, debt, wacc, roic, fcff_w, nopat_w, mcap, vals):
    """빈칸 이유(화면 툴팁용). 값이 있는 항목은 넣지 않는다."""
    out = {}
    nodebt = "차입금이 재무상태표에 따로 표시되지 않아 계산하지 않음(값을 만들지 않음)"
    if wacc is None:
        out["wacc"] = nodebt if debt is None else ("시가총액(주가·주식 수) 없음" if not mcap else "타인자본비용 자료 없음")
    if roic is None:
        out["roic"] = nodebt if debt is None else "영업이익 또는 투하자본 자료 없음"
    if "dcf" not in vals:
        if debt is None:
            out["dcf"] = nodebt
        elif fcff_w is None:
            out["dcf"] = "FCFF(영업현금흐름+이자×(1−t)−설비투자)가 3년 미만"
        elif not wacc:
            out["dcf"] = "WACC 없음"
        else:
            out["dcf"] = next((n for n in notes if "DCF 지분가치" in n), "자료 부족")
    if "dcf_e" not in vals:
        if debt is None:
            out["dcf_e"] = nodebt
        elif nopat_w is None:
            out["dcf_e"] = "세후 영업이익이 3년 미만"
        elif not wacc:
            out["dcf_e"] = "WACC 없음"
        else:
            out["dcf_e"] = next((n for n in notes if "이익 기준" in n), "자료 부족")
    return out


def estimate_roic_wacc(A, rows, cons, P, F=None):
    """ROIC·WACC 예상값(보수적). 증권사 컨센서스에는 ROIC·WACC가 없어서, 실제 값이 있는 마지막 시점을 기준으로 추정한다.
    - ROIC(예상) = 보정 후 컨센서스 영업이익 × (1 − 세율) ÷ 투하자본
        세율 = 기준 시점 유효세율과 대체세율 중 높은 쪽, 투하자본 = 기준 시점 실제값과 '컨센서스 BPS × 주식 수 + 기준 시점 순차입금' 중 큰 쪽
    - WACC(예상) = 기준 시점 WACC와 '그 연도 금리로 다시 계산한 WACC'(기준 시점 비중·타인자본비용 유지) 중 높은 쪽
    값이 낮게 나오는 쪽(ROIC)·높게 나오는 쪽(WACC)을 골라 판단이 낙관적으로 흐르지 않게 한다."""
    from . import calibration
    base = next((r for r in reversed(rows) if r.get("roic") is not None and r.get("wacc") is not None
                 and r.get("ic") and not r.get("estimated")), None)
    if base is None:
        return
    for fy in [P["current"]] + P["future"]:
        row = next((r for r in rows if r["year"] == fy), None)
        if row is not None and row.get("roic") is not None:
            continue                              # 실제(최근 12개월) 값이 있으면 예상하지 않음
        op = cons.get((fy, "op_income"))
        if op is None:
            continue
        f_op, why = F[(fy, "op_income")] if F else calibration.factor("op_income", fy)
        op_adj = op * f_op * 1e8
        tax = row.get("tax") if row is not None and row.get("tax") is not None else base.get("tax")   # 법정 한계세율
        nopat = op_adj * (1 - tax)
        ic = base["ic"]
        bps, sh = cons.get((fy, "bps")), base.get("shares")
        ic_bps = None
        if bps and sh and base.get("eq") and base.get("eq_owner") and base.get("debt") is not None:
            ic_bps = bps * sh * (base["eq"] / base["eq_owner"]) + (base["debt"] - (base.get("cash") or 0))
            ic = max(ic, ic_bps)
        roic = nopat / ic if ic and ic > 0 else None
        rf, _ = _rf(A, fy)
        ke = rf + A["베타"] * A["시장위험프리미엄_ERP"]
        wacc_now = None
        tax = tax if tax is not None else A["대체세율"]
        if base.get("mcap") and base.get("debt") is not None:
            E, D = base["mcap"] * 1e8, base["debt"]
            kd = base.get("kd") or (rf + A["타인자본비용_가산"])
            wacc_now = E / (D + E) * ke + (D / (D + E) * kd * (1 - tax) if D else 0)
        wacc = max(x for x in (base["wacc"], wacc_now) if x is not None)
        note = (f"ROIC·WACC 예상(보수적 추정): {base['year']}년 실제값 기준. 영업이익 컨센서스 {op:,.0f}억 × 보정 {f_op:.2f}, "
                f"세율 {tax:.0%}, 투하자본 {ic / 1e8:,.0f}억(실제 {base['ic'] / 1e8:,.0f}억"
                + (f", BPS 기준 {ic_bps / 1e8:,.0f}억" if ic_bps else "") + " 중 큰 값), "
                f"WACC {base['wacc']:.1%}(기준) / {wacc_now:.1%}(올해 금리 재계산) 중 높은 값" if wacc_now is not None else "")
        if row is None:
            row = {"year": fy, "kind": "전망(컨센서스)", "price": None, "values": {}, "median": None, "gap": None,
                   "fair": {}, "mult": {}, "notes": [], "caution": True}
            rows.append(row)
            rows.sort(key=lambda r: r["year"])
        row.update(roic=roic, wacc=wacc, ke=row.get("ke", ke), spread=roic - wacc if roic is not None else None,
                   nopat=nopat, ic=ic, tax=tax, estimated=True)
        row["notes"] = (row.get("notes") or []) + [note]


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _one_year(A, bk, y, flows, stocks, prev_stocks, price, mcap, roe_hist, eps, bps, revenue, fair, kind):
    """한 시점의 가치평가. flows/stocks: dict(계정→값)."""
    notes, n_unverified = [], 0
    g, w, erp, beta = A["영구성장률_g"], A["S-RIM_초과이익지속계수_w"], A["시장위험프리미엄_ERP"], A["베타"]
    rf, rf_note = _rf(A, y)
    if rf_note:
        notes.append(rf_note)
    ke = rf + beta * erp
    # 세율: 법정 한계세율(지방세 포함, 과세표준 구간). 실효세율은 참고로만 남김
    t = flows.get("_tax")
    if t is None:
        t = _tax_rate(A, flows.get("acc_pretax"), flows.get("acc_tax"), notes)
    if flows.get("_tax_note"):
        notes.append(f"세율 {t:.1%}: {flows['_tax_note']}")

    debt, cash = stocks.get("acc_debt"), stocks.get("acc_cash")
    int_paid = (flows.get("acc_int_paid") or 0) + (flows.get("acc_int_paid_fin") or 0)
    # 재무상태표에 차입금·사채·리스부채 줄이 없을 때:
    #  - 이자지급이 작으면(이자 ÷ (무위험수익률+가산) < 총자산의 5%) 실제로 차입이 없는 것으로 보고 원본대로 0을 쓴다
    #    (예: 스튜디오드래곤 2019~2021, 리스 이자 등 소액만 있음)
    #  - 이자지급이 크면 차입금이 다른 금융부채 줄에 묶여 있는 것이므로 값을 지어내지 않고 빈칸으로 둔다(예: 메디톡스)
    assets = stocks.get("acc_assets")
    hidden_limit = 0.05 * assets if assets else 0

    def _hidden(d):
        if d != 0 or not int_paid:
            return False
        return int_paid / (rf + A["타인자본비용_가산"]) > hidden_limit

    if _hidden(debt):
        notes.append(f"재무상태표 본문에 차입금 줄이 없는데 이자지급({_eok(int_paid)})이 커서 차입금이 다른 금융부채에 "
                     "묶여 있는 것으로 보임 → 차입금 미식별(값을 만들지 않음)")
        debt = None
    elif debt == 0 and int_paid:
        notes.append(f"차입금·사채 줄 없음, 이자지급 {_eok(int_paid)}은 리스 등 소액 → 원본대로 차입금 0")
    pdebt = prev_stocks.get("acc_debt") if prev_stocks else None
    if _hidden(pdebt):
        pdebt = None
    avg_debt = (debt + pdebt) / 2 if debt is not None and pdebt is not None else debt
    kd = None
    if avg_debt and avg_debt > 0 and int_paid:
        kd = int_paid / avg_debt
        if not (0.01 <= kd <= 0.15):
            notes.append(f"이자지급/평균차입금 {kd:.1%} 비정상 → 무위험수익률+{A['타인자본비용_가산']:.1%}p")
            kd = rf + A["타인자본비용_가산"]
    elif debt:
        kd = rf + A["타인자본비용_가산"]
        notes.append("이자지급 자료 없음 → 타인자본비용 = 무위험수익률+가산")

    # 순차입금: 차입 전부 + 리스(본문에 없으면 주석) − 현금·단기금융 (공통 기준). 차입금 미식별이면 그대로 빈칸
    if debt is not None and stocks.get("_debt_total") is not None:
        debt, cash = stocks["_debt_total"], stocks.get("_cash_total", cash)
        if stocks.get("_lease_added"):
            notes.append(f"리스부채 {_eok(stocks['_lease_added'])}(주석)을 차입금에 더함")
        if stocks.get("_stfin_added"):
            notes.append(f"단기금융상품 {_eok(stocks['_stfin_added'])}(주석)을 현금에 더함")
    nci = stocks.get("_nci") or 0
    E = mcap * 1e8 if mcap else None
    wacc = None
    if E and debt is not None and (kd is not None or debt == 0):
        D = debt
        wacc = E / (D + E) * ke + (D / (D + E) * kd * (1 - t) if D else 0)

    op = flows.get("acc_op")
    nopat = op * (1 - t) if op is not None else None
    eq, eq_owner = stocks.get("acc_eq"), stocks.get("acc_eq_owner")
    ic = eq + debt - (cash or 0) if eq is not None and debt is not None else None
    pic = None
    if prev_stocks and prev_stocks.get("acc_eq") is not None and pdebt is not None:
        pic = prev_stocks["acc_eq"] + pdebt - (prev_stocks.get("acc_cash") or 0)
    avg_ic = (ic + pic) / 2 if ic is not None and pic is not None else ic
    roic = nopat / avg_ic if nopat is not None and avg_ic and avg_ic > 0 else None

    ocf, capex = flows.get("acc_ocf"), (flows.get("acc_capex_ppe") or 0) + (flows.get("acc_capex_int") or 0)
    fcff = ocf - capex + (flows.get("acc_int_paid") or 0) * (1 - t) if ocf is not None else None

    shares = stocks.get("_shares")
    if stocks.get("_shares_note") and stocks.get("_shares_unverified"):
        notes.append(f"주식수: {stocks['_shares_note']}")
    vals, zero = {}, []

    def _eqv(ev):
        """기업가치 → 주당 지배주주 가치. 음수면 0(주주 몫 없음)"""
        v = (ev - (debt - (cash or 0)) - nci) / shares
        return v
    # S-RIM
    roe_w = wavg3(roe_hist)
    if eq_owner and roe_w is not None and shares:
        vals["srim"] = (eq_owner + eq_owner * (roe_w - ke) * w / (1 + ke - w)) / shares
    # DCF(공통 기준): FCFF = 영업현금흐름 + 영업 이자지급 × (1−t) − 설비투자(유형+무형) (운전자본 변동이 모두 반영된 현금흐름표 기준)
    #   정상화 = 최근 5년 단순 평균(3년 이상일 때), 성장률 g → 기업가치 = 정상화 FCFF × (1+g) ÷ (WACC − g)
    #   주당 = (기업가치 − 순차입금 − 비지배지분) ÷ 주식수, 음수면 0(주주 몫 없음)
    fh = [x for x in flows.get("_fcff_hist", [fcff]) if x is not None]
    fcff_w = sum(fh) / len(fh) if len(fh) >= 3 else None
    if fcff_w is not None and wacc and wacc > g + 0.005 and debt is not None and shares:
        dv = _eqv(fcff_w * (1 + g) / (wacc - g))
        vals["dcf"] = max(dv, 0.0)
        if dv <= 0:
            zero.append("dcf")
            notes.append("DCF: 순차입금·비지배지분이 기업가치보다 큼 → 0(주주 몫 없음)")
    elif fcff_w is None and fh:
        notes.append(f"FCFF가 {len(fh)}년뿐 → DCF 계산 안 함(3년 이상 필요)")
    elif debt is None:
        notes.append("차입금 미식별 → DCF·ROIC·WACC 계산 안 함")
    # NOPAT ÷ WACC(무성장): 세후 영업이익 5년 평균을 그대로 영구히 번다고 볼 때의 가치(현금흐름 대신 이익 기준)
    nh = [x for x in flows.get("_nopat_hist", [nopat]) if x is not None]
    nopat_w = sum(nh) / len(nh) if len(nh) >= 3 else None
    if nopat_w is not None and wacc and debt is not None and shares:
        dv2 = _eqv(nopat_w / wacc)
        vals["dcf_e"] = max(dv2, 0.0)
        if dv2 <= 0:
            zero.append("dcf_e")
            notes.append("NOPAT÷WACC: 순차입금·비지배지분이 기업가치보다 큼 → 0(주주 몫 없음)")
    # 상대가치
    if fair.get("per") and eps and eps > 0:
        vals["per"] = fair["per"] * eps
    if fair.get("pbr") and bps:
        vals["pbr"] = fair["pbr"] * bps
    if fair.get("psr") and revenue and shares:
        vals["psr"] = fair["psr"] * revenue * 1e8 / shares
    core = [v for k, v in vals.items() if k != "dcf_e" and k not in zero]   # 종합: S-RIM·DCF·PER·PBR·PSR(주주 몫 없음 0은 제외)
    med = _median(core) if len(core) >= 3 else None
    if len(core) < 3:
        notes.append(f"계산된 방법이 {len(core)}개뿐 → 종합 중앙값 내지 않음(3개 이상일 때만)")
    mult = {
        "per": price / eps if price and eps and eps > 0 else None,
        "pbr": price / bps if price and bps else None,
        "psr": mcap / revenue if mcap and revenue else None,
    }
    return {
        "year": y, "kind": kind, "price": price, "mcap": mcap, "shares": shares,
        "values": vals, "median": med, "gap": (med / price - 1) if med and price else None,
        "rf": rf, "ke": ke, "kd": kd, "tax": t, "tax_eff": flows.get("_tax_eff"), "wacc": wacc, "roic": roic,
        "spread": roic - wacc if roic is not None and wacc is not None else None,
        "nopat": nopat, "ic": ic, "fcff": fcff, "fcff_w": fcff_w, "debt": debt, "cash": cash,
        "net_debt": debt - (cash or 0) if debt is not None else None, "roe_w": roe_w,
        "mult": mult, "fair": dict(fair), "notes": notes,
        "nopat_w": nopat_w, "eq": eq, "eq_owner": eq_owner, "nci": nci, "zero": zero,
        "shares_note": stocks.get("_shares_note"), "shares_unverified": stocks.get("_shares_unverified"),
        "lease": stocks.get("_lease"), "fcff_n_years": len(fh),
        "fcff_hist": {str(y - i): v for i, v in enumerate(flows.get("_fcff_hist") or []) if v is not None} if kind == "실적" else {},
        "missing": _missing(notes, debt, wacc, roic, fcff_w, nopat_w, mcap, vals),
    }


FLOW_KEYS = ["acc_op", "acc_pretax", "acc_tax", "acc_ocf", "acc_capex_ppe", "acc_capex_int", "acc_int_paid",
             "acc_int_paid_fin", "acc_ni_owner", "acc_revenue", "acc_eps_basic"]
STOCK_KEYS = ["acc_debt", "acc_cash", "acc_eq", "acc_eq_owner", "acc_assets"]


def run(conn, code, P):
    conn.execute(SCHEMA)
    A = load_assumptions()
    bk = Book(conn, code)
    rows, checks = [], []
    hist_mult = {"per": [], "pbr": [], "psr": []}
    min_obs = A["상대가치_최소관측연수"]

    from . import standards
    try:
        from . import value5
        face, xnotes = value5.face_by_year(code), value5.notes_by_year(code)
    except Exception:       # noqa: BLE001  주석·원본이 없어도 계산은 계속
        face, xnotes = {}, {}

    def tax_of(y, per="FY", ttm=False):
        """법정 한계세율(지방세 포함)과 사유. 과세표준은 세전이익으로 근사"""
        pt = bk.ttm(y, "acc_pretax", per) if ttm else bk.v(y, "acc_pretax", per)
        return standards.marginal_tax(y, pt, [bk.v(y - i, "acc_pretax") for i in range(1, 6)])

    def fcff_of(y, per="FY", ttm=False):
        f = {k: (bk.ttm(y, k, per) if ttm else bk.v(y, k, per)) for k in FLOW_KEYS}
        t, _ = tax_of(y, per, ttm)
        if f["acc_ocf"] is None:
            return None
        return f["acc_ocf"] - (f["acc_capex_ppe"] or 0) - (f["acc_capex_int"] or 0) + (f["acc_int_paid"] or 0) * (1 - t)

    def nopat_of(y, per="FY", ttm=False):
        op = bk.ttm(y, "acc_op", per) if ttm else bk.v(y, "acc_op", per)
        if op is None:
            return None
        return op * (1 - tax_of(y, per, ttm)[0])

    def extras(y, per="FY", ttm=False, share_year=None):
        """공통 재료: 세율·주식수·순차입금(리스·단기금융 포함)·비지배지분"""
        t, tn = tax_of(y, per, ttm)
        sy = share_year if share_year is not None else y
        sh, sn, su = standards.shares(lambda yy, k, pp="FY": bk.v(yy, k, pp), sy)
        nd = standards.net_debt(lambda yy, k, pp="FY": bk.v(yy, k, pp), y, per, xnotes.get(y if per == "FY" else y - 1))
        nci = (face.get(y) or face.get(y - 1) or {}).get("nci")
        fl = {"_tax": t, "_tax_note": tn}
        pt, tx = (bk.ttm(y, "acc_pretax", per), bk.ttm(y, "acc_tax", per)) if ttm else (bk.v(y, "acc_pretax", per), bk.v(y, "acc_tax", per))
        fl["_tax_eff"] = tx / pt if pt and pt > 0 and tx is not None else None
        st = {"_shares": sh, "_shares_note": sn, "_shares_unverified": su, "_debt_total": nd["debt"], "_cash_total": nd["cash"],
              "_lease": nd["lease"], "_lease_added": nd["lease_added"], "_stfin_added": nd["stfin_added"], "_nci": nci or 0}
        return fl, st

    def roe(y):
        r = bk.v(y, "roe")
        return r / 100 if r is not None else None

    for y in P["past"]:
        flows = {k: bk.v(y, k) for k in FLOW_KEYS}
        flows["_fcff_hist"] = [fcff_of(y - i) for i in range(5)]
        flows["_nopat_hist"] = [nopat_of(y - i) for i in range(5)]
        fx, sx = extras(y)
        flows.update(fx)
        stocks = {k: bk.v(y, k) for k in STOCK_KEYS}
        stocks.update(sx)
        prev = {k: bk.v(y - 1, k) for k in STOCK_KEYS}
        price, mcap, eps, bps, rev = (bk.v(y, "price_close"), bk.v(y, "market_cap"), bk.v(y, "eps"),
                                      bk.v(y, "bps"), bk.v(y, "revenue"))
        # 그해 실제 배수를 누적에 넣은 뒤 중앙값을 적정 배수로
        if price and eps and eps > 0:
            hist_mult["per"].append(price / eps)
        if price and bps:
            hist_mult["pbr"].append(price / bps)
        if mcap and rev:
            hist_mult["psr"].append(mcap / rev)
        fair = {m: (statistics.median(v) if len(v) >= min_obs else None) for m, v in hist_mult.items()}
        r = _one_year(A, bk, y, flows, stocks, prev, price, mcap, [roe(y), roe(y - 1), roe(y - 2)], eps, bps, rev,
                      fair, "실적")
        if not all(fair.values()):
            r["notes"].append(f"적정 배수: 관측 {min_obs}년 미만인 배수는 계산 안 함")
        r["caution"] = not all(bk.ok(y, k) for k in ("bps", "market_cap"))
        if r["caution"]:
            r["notes"].append("BPS·시가총액이 미검증(역산 주식수) → 주당가치도 주의")
        rows.append(r)

    # 현재 연도: 최근 재무상태(분기·반기말) + 최근 12개월 흐름 + 기준일 주가
    cy = P["current"]
    per = bk.latest_interim(cy)
    price, mcap = bk.v(cy, "price_close", "ASOF"), bk.v(cy, "market_cap", "ASOF")
    if per:
        flows = {k: bk.ttm(cy, k, per) for k in FLOW_KEYS}
        missing = [k for k in ("acc_op", "acc_ocf", "acc_ni_owner") if flows.get(k) is None]
        note = None
        if missing:
            flows = {k: bk.v(cy - 1, k) for k in FLOW_KEYS}
            note = f"최근 12개월 계산 불가({', '.join(missing)}) → {cy - 1}년 연간 흐름 사용"
            flows["_fcff_hist"] = [fcff_of(cy - 1 - i) for i in range(5)]
            flows["_nopat_hist"] = [nopat_of(cy - 1 - i) for i in range(5)]
            fx = extras(cy - 1)[0]
        else:
            flows["_fcff_hist"] = [fcff_of(cy, per, ttm=True)] + [fcff_of(cy - i) for i in range(1, 5)]
            flows["_nopat_hist"] = [nopat_of(cy, per, ttm=True)] + [nopat_of(cy - i) for i in range(1, 5)]
            fx = extras(cy, per, ttm=True, share_year=cy - 1)[0]
        flows.update(fx)
        stocks = {k: bk.v(cy, k, per) for k in STOCK_KEYS}
        stocks.update(extras(cy, per, share_year=cy - 1)[1])      # 재무상태는 최근 분기·반기말, 주식수는 작년 말
        prev = {k: bk.v(cy - 1, k) for k in STOCK_KEYS}
        eq_now, eq_prev = stocks.get("acc_eq_owner"), bk.v(cy - 1, "acc_eq_owner")
        ni = flows.get("acc_ni_owner")
        roe_now = ni / ((eq_now + eq_prev) / 2) if ni is not None and eq_now and eq_prev else None
        eps = flows.get("acc_eps_basic")
        bps = bk.v(cy, "bps", per)
        rev = flows["acc_revenue"] / 1e8 if flows.get("acc_revenue") else None
        fair = {m: (statistics.median(v) if len(v) >= min_obs else None) for m, v in hist_mult.items()}
        r = _one_year(A, bk, cy, flows, stocks, prev, price, mcap, [roe_now, roe(cy - 1), roe(cy - 2)], eps, bps,
                      rev, fair, f"현재({per}말 재무·최근12개월·기준일 주가)")
        if note:
            r["notes"].insert(0, note)
        r["notes"].append(f"재무상태 {per}말, 흐름은 최근 12개월, 적정 배수는 {cy - 1}년까지 중앙값")
        r["caution"] = True
        rows.append(r)

    # 미래: 컨센서스가 있을 때만. 보정은 공통 규칙(×0.83, 3개월 사이 5% 넘게 하향이면 ×0.73, 매출은 보정 안 함)
    cons = {(r["target_year"], r["metric"]): r["value"] for r in db.latest_consensus(conn, code)}
    F = standards.factors(conn, code, P)
    base = next((r for r in reversed(rows) if r.get("shares")), None)
    last_sh = base["shares"] if base else None
    nd_now = base["net_debt"] if base else None
    nci_now = base.get("nci", 0) if base else 0
    # 자본 굴리기: 전년 지배자본 + 보정 순이익 − 배당(마지막 실적 배당 유지)
    div_last = (face.get(cy - 1) or {}).get("div") or 0
    eq_r, rolled = bk.v(cy - 1, "acc_eq_owner"), {}
    for y in [cy] + list(P["future"]):
        ni = cons.get((y, "net_income"))
        if ni is None or eq_r is None:
            break
        ni_adj = ni * 1e8 * F[(y, "net_income")][0]
        e1 = eq_r + ni_adj - div_last
        rolled[y] = (eq_r, e1, ni_adj)
        eq_r = e1
    pretax_hist = [bk.v(cy - i, "acc_pretax") for i in range(1, 6)]
    has_cy = any(r["year"] == cy for r in rows)          # 올해 실적(분기·반기)이 없으면 올해도 컨센서스로
    for fy in ([] if has_cy else [cy]) + list(P["future"]):
        eps, bps, rev = cons.get((fy, "eps")), cons.get((fy, "bps")), cons.get((fy, "revenue"))
        fe, why_e = F[(fy, "eps")]
        fo, why_o = F[(fy, "op_income")]
        eps = eps * fe if eps is not None else None
        if not any((eps, bps, rev)):
            continue
        fair = {m: (statistics.median(v) if len(v) >= min_obs else None) for m, v in hist_mult.items()}
        rf, _ = _rf(A, fy)
        ke = standards.ke(A, rf)
        t, tn = standards.marginal_tax(fy, None, pretax_hist)
        vals, notes, roe_f, eq_f = {}, [], None, None
        if fy in rolled and last_sh:
            e0, e1, ni_adj = rolled[fy]
            roe_f, eq_f = ni_adj / ((e0 + e1) / 2), e1
            w = A["S-RIM_초과이익지속계수_w"]
            vals["srim"] = (e1 + e1 * (roe_f - ke) * w / (1 + ke - w)) / last_sh
        if fair.get("per") and eps and eps > 0:
            vals["per"] = fair["per"] * eps
        if fair.get("pbr") and bps:
            vals["pbr"] = fair["pbr"] * bps
        if fair.get("psr") and rev and last_sh:
            vals["psr"] = fair["psr"] * rev * 1e8 / last_sh
        op = cons.get((fy, "op_income"))
        rows.append({"year": fy, "kind": "전망(컨센서스)", "price": None, "values": vals,
                     "median": _median(vals.values()) if len(vals) >= 3 else None,
                     "gap": None, "rf": rf, "ke": ke, "tax": t, "fair": fair, "mult": {}, "caution": True,
                     "shares": last_sh, "net_debt": nd_now, "nci": nci_now, "roe_f": roe_f, "eq_owner": eq_f,
                     "op_adj": op * 1e8 * fo if op is not None else None, "zero": [],
                     "notes": [f"컨센서스 보정: 영업이익 {why_o}, EPS·순이익·ROE {why_e}, 매출 보정 안 함",
                               f"S-RIM 자본 = 전년 자본 + 보정 순이익 − 배당({_eok(div_last)} 유지), 주식수·순차입금은 최근 값",
                               f"세율 {t:.1%}" + (f"({tn})" if tn else "")]})

    estimate_roic_wacc(A, rows, cons, P, F)
    # 전망 연도 DCF(공통 기준): 보정 영업이익 × (1−t) = FCFF(설비투자 = 감가상각, 운전자본 증가 0) → ÷ (WACC − g)
    g = A["영구성장률_g"]
    for r in rows:
        if r.get("kind") != "전망(컨센서스)" or not r.get("op_adj") or not r.get("wacc") or r.get("shares") is None \
                or r.get("net_debt") is None or r["wacc"] <= g + 0.005:
            continue
        dv = (r["op_adj"] * (1 - r["tax"]) * (1 + g) / (r["wacc"] - g) - r["net_debt"] - (r.get("nci") or 0)) / r["shares"]
        r["values"]["dcf"] = max(dv, 0.0)
        if dv <= 0:
            r["zero"].append("dcf")
        r["fcff_w"] = r["op_adj"] * (1 - r["tax"])
        core = [v for k, v in r["values"].items() if k != "dcf_e" and k not in r["zero"]]
        r["median"] = _median(core) if len(core) >= 3 else None

    # 검증 항목 11~12
    for r in rows:
        for n in r["notes"]:
            if any(x in n for x in ("비정상", "미식별", "계산 안 함", "부풀", "0 이하")):
                checks.append({"check_no": 11, "check": "가치평가 입력", "year": r["year"], "metric": "valuation",
                               "level": "WARN", "message": n})
        if r.get("roic") is not None and not (-0.5 < r["roic"] < 0.8):
            checks.append({"check_no": 12, "check": "ROIC 범위", "year": r["year"], "metric": "roic",
                           "level": "WARN", "message": f"ROIC {r['roic']:.1%}가 일반 범위를 벗어남"})
    run_at = db.now()
    conn.execute("DELETE FROM validation WHERE stock_code=? AND check_no IN (11, 12)", (code,))
    for c in checks:
        conn.execute("INSERT INTO validation (stock_code,run_at,check_no,check_name,fiscal_year,metric,level,message) "
                     "VALUES (?,?,?,?,?,?,?,?)", (code, run_at, c["check_no"], c["check"], c["year"], c["metric"],
                                                  c["level"], c["message"]))
    conn.execute("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?)",
                 (code, P["base_year"], run_at, json.dumps(A, ensure_ascii=False), json.dumps(rows, ensure_ascii=False)))
    conn.commit()
    return {"rows": rows, "assumptions": A, "checks": checks}


def latest(conn, code, base_year):
    conn.execute(SCHEMA)
    r = conn.execute("SELECT * FROM valuations WHERE stock_code=? AND base_year=?", (code, base_year)).fetchone()
    if r is None:
        return None
    return {"computed_at": r["computed_at"], "assumptions": json.loads(r["assumptions"]), "rows": json.loads(r["rows"])}
