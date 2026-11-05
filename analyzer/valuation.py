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
        elif fcff_w is not None and fcff_w <= 0:
            out["dcf"] = "최근 3년 가중 FCFF(영업현금흐름−투자)가 0 이하 → 영구가치 공식 적용 불가"
        elif not wacc:
            out["dcf"] = "WACC 없음"
        else:
            out["dcf"] = next((n for n in notes if "DCF 지분가치" in n), "자료 부족")
    if "dcf_e" not in vals:
        if debt is None:
            out["dcf_e"] = nodebt
        elif nopat_w is not None and nopat_w <= 0:
            out["dcf_e"] = "최근 3년 가중 세후 영업이익이 0 이하"
        elif not wacc:
            out["dcf_e"] = "WACC 없음"
        else:
            out["dcf_e"] = next((n for n in notes if "이익 기준" in n), "자료 부족")
    return out


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
    t = _tax_rate(A, flows.get("acc_pretax"), flows.get("acc_tax"), notes)

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

    shares = eq_owner / bps if eq_owner and bps else None
    vals = {}
    # S-RIM
    roe_w = wavg3(roe_hist)
    if eq_owner and roe_w is not None and shares:
        vals["srim"] = (eq_owner + eq_owner * (roe_w - ke) * w / (1 + ke - w)) / shares
    # DCF(FCFF): 3:2:1 가중 FCFF → 영구성장 → 기업가치 − 순차입금 → 지배지분
    fcff_w = wavg3(fcff_hist := flows.get("_fcff_hist", [fcff]))
    if fcff_w is not None and fcff_w > 0 and wacc and wacc > g + 0.005 and debt is not None and shares and eq:
        ev = fcff_w * (1 + g) / (wacc - g)
        dv = (ev - (debt - (cash or 0))) * (eq_owner / eq) / shares
        if dv > 0:
            vals["dcf"] = dv
        else:
            notes.append("DCF 지분가치가 0 이하(순차입금 > 기업가치) → 빈칸")
        if wacc - g < 0.03:
            notes.append(f"WACC({wacc:.1%})와 성장률({g:.1%}) 차이가 3%p 미만 → DCF가 크게 부풀 수 있음(주의)")
    elif fcff_w is not None and fcff_w <= 0:
        notes.append("가중 FCFF가 0 이하 → DCF 계산 안 함")
    elif debt is None:
        notes.append("차입금 미식별 → DCF·ROIC·WACC 계산 안 함")
    # DCF(이익 기준): 현금흐름이 투자·운전자본으로 크게 출렁이는 회사(예: 콘텐츠 제작비가 영업현금흐름에 잡히는 회사)를 위해
    # 실제 영업이익으로 계산한다. 기업가치 = 가중 NOPAT ÷ WACC (새 투자가 자본비용만큼만 번다고 보는 보수적 가정, 성장 가치 0)
    nopat_w = wavg3(flows.get("_nopat_hist", [nopat]))
    if nopat_w is not None and nopat_w > 0 and wacc and debt is not None and shares and eq:
        dv2 = (nopat_w / wacc - (debt - (cash or 0))) * (eq_owner / eq) / shares
        if dv2 > 0:
            vals["dcf_e"] = dv2
        else:
            notes.append("DCF(이익 기준) 지분가치가 0 이하(순차입금 > 기업가치) → 빈칸")
    elif nopat_w is not None and nopat_w <= 0:
        notes.append("가중 NOPAT(세후 영업이익)가 0 이하 → DCF(이익 기준) 계산 안 함")
    # 상대가치
    if fair.get("per") and eps and eps > 0:
        vals["per"] = fair["per"] * eps
    if fair.get("pbr") and bps:
        vals["pbr"] = fair["pbr"] * bps
    if fair.get("psr") and revenue and shares:
        vals["psr"] = fair["psr"] * revenue * 1e8 / shares
    core = [v for k, v in vals.items() if k != "dcf_e"]   # 종합은 기존 5가지 방법(S-RIM·DCF·PER·PBR·PSR)으로 유지
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
        "rf": rf, "ke": ke, "kd": kd, "tax": t, "wacc": wacc, "roic": roic,
        "spread": roic - wacc if roic is not None and wacc is not None else None,
        "nopat": nopat, "ic": ic, "fcff": fcff, "fcff_w": fcff_w, "debt": debt, "cash": cash,
        "net_debt": debt - (cash or 0) if debt is not None else None, "roe_w": roe_w,
        "mult": mult, "fair": dict(fair), "notes": notes,
        "nopat_w": nopat_w,
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

    def fcff_of(y, per="FY", ttm=False):
        f = {k: (bk.ttm(y, k, per) if ttm else bk.v(y, k, per)) for k in FLOW_KEYS}
        t = _tax_rate(A, f["acc_pretax"], f["acc_tax"], [])
        if f["acc_ocf"] is None:
            return None
        return f["acc_ocf"] - (f["acc_capex_ppe"] or 0) - (f["acc_capex_int"] or 0) + (f["acc_int_paid"] or 0) * (1 - t)

    def nopat_of(y, per="FY", ttm=False):
        f = {k: (bk.ttm(y, k, per) if ttm else bk.v(y, k, per)) for k in ("acc_op", "acc_pretax", "acc_tax")}
        if f["acc_op"] is None:
            return None
        return f["acc_op"] * (1 - _tax_rate(A, f["acc_pretax"], f["acc_tax"], []))

    def roe(y):
        r = bk.v(y, "roe")
        return r / 100 if r is not None else None

    for y in P["past"]:
        flows = {k: bk.v(y, k) for k in FLOW_KEYS}
        flows["_fcff_hist"] = [fcff_of(y), fcff_of(y - 1), fcff_of(y - 2)]
        flows["_nopat_hist"] = [nopat_of(y), nopat_of(y - 1), nopat_of(y - 2)]
        stocks = {k: bk.v(y, k) for k in STOCK_KEYS}
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
            flows["_fcff_hist"] = [fcff_of(cy - 1), fcff_of(cy - 2), fcff_of(cy - 3)]
            flows["_nopat_hist"] = [nopat_of(cy - 1), nopat_of(cy - 2), nopat_of(cy - 3)]
        else:
            flows["_fcff_hist"] = [fcff_of(cy, per, ttm=True), fcff_of(cy - 1), fcff_of(cy - 2)]
            flows["_nopat_hist"] = [nopat_of(cy, per, ttm=True), nopat_of(cy - 1), nopat_of(cy - 2)]
        stocks = {k: bk.v(cy, k, per) for k in STOCK_KEYS}
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

    # 미래: 컨센서스가 있을 때만(주당 EPS·BPS·매출 추정치)
    cons = {(r["target_year"], r["metric"]): r["value"] for r in db.latest_consensus(conn, code)}
    last_sh = next((r["shares"] for r in reversed(rows) if r.get("shares")), None)
    for fy in P["future"]:
        eps, bps, rev, roe_e = cons.get((fy, "eps")), cons.get((fy, "bps")), cons.get((fy, "revenue")), cons.get((fy, "roe"))
        # 컨센서스 보정계수(원래 값은 DB에 그대로): EPS·ROE는 EPS 계수, 매출은 매출 계수, BPS는 보정 안 함
        from . import calibration
        fe, why_e = calibration.factor("eps", fy)
        fr, _ = calibration.factor("revenue", fy)
        eps = eps * fe if eps is not None else None
        roe_e = roe_e * fe if roe_e is not None else None
        rev = rev * fr if rev is not None else None
        if not any((eps, bps, rev)):
            continue
        fair = {m: (statistics.median(v) if len(v) >= min_obs else None) for m, v in hist_mult.items()}
        rf, _ = _rf(A, fy)
        ke = rf + A["베타"] * A["시장위험프리미엄_ERP"]
        vals = {}
        if bps and roe_e is not None:
            w = A["S-RIM_초과이익지속계수_w"]
            vals["srim"] = bps + bps * (roe_e / 100 - ke) * w / (1 + ke - w)
        if fair.get("per") and eps and eps > 0:
            vals["per"] = fair["per"] * eps
        if fair.get("pbr") and bps:
            vals["pbr"] = fair["pbr"] * bps
        if fair.get("psr") and rev and last_sh:
            vals["psr"] = fair["psr"] * rev * 1e8 / last_sh
        rows.append({"year": fy, "kind": "전망(컨센서스)", "price": None, "values": vals,
                     "median": _median(vals.values()) if len(vals) >= 3 else None,
                     "gap": None, "ke": ke, "fair": fair, "mult": {}, "notes": ["컨센서스 EPS·BPS·ROE·매출로 계산, DCF 제외",
                               f"보정계수 적용: EPS·ROE ×{fe:.2f}, 매출 ×{fr:.2f} ({why_e})"],
                     "caution": True})

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
