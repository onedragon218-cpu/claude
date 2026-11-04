"""가치평가: S-RIM, DCF(FCFF), ROIC, WACC, 상대가치(PER·PBR·PSR), 주가, 적정가 중앙값.

모든 값은 '계산값(가정 포함)'이다. 입력 원값의 검증 상태를 그대로 물려받고, 계산식과 가정을 값마다 남긴다.
- 과거 연도: 그해 공시 실적과 그해 금리로 계산(실선)
- 올해·전망 연도: 컨센서스(신뢰 출처)로 계산(점선)
- 상대가치 기준 배수 = 그 회사 과거 10년 연말 배수의 중앙값(기간 전체를 쓰므로 과거 연도에는 사후적 기준)
"""
import json, os, statistics
from . import config, paths
from .validate import shares_info
from .metrics import pt, worst, _trust_rank

EOK = 1e8
VAL_METRICS = [  # (키, 이름, 단위, 묶음)
    ("price", "주가", "원", "ps"), ("fair", "적정가 중앙값", "원", "ps"), ("srim", "S-RIM", "원", "ps"),
    ("dcf", "DCF", "원", "ps"), ("per_v", "PER 가치", "원", "ps"), ("pbr_v", "PBR 가치", "원", "ps"), ("psr_v", "PSR 가치", "원", "ps"),
    ("roic", "ROIC", "%", "rate"), ("wacc", "WACC", "%", "rate"),
]


def load_assumptions(w=None):
    with open(os.path.join(os.path.dirname(__file__), "assumptions.json"), encoding="utf-8") as f:
        a = json.load(f)
    if w:
        p = os.path.join(w.manual, "assumptions.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                a.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    return a


def _rf(a, y):
    t = {int(k): v for k, v in a["국고채10년_연평균"].items() if not k.startswith("_")}
    past = [k for k in t if k <= y]
    return (t[max(past)] if past else t[min(t)]) / 100, (max(past) if past else min(t))


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def build(store, code, periods, metrics, w=None):
    a = load_assumptions(w)
    erp, g = a["위험프리미엄_ERP"], a["영구성장률_g"]
    b = periods["base"]
    yrs = periods["helper"] + periods["actual"]
    A = {y: {r["item"]: dict(r) for r in store.current(code, fiscal_year=y, kind="ACTUAL", period="FY")} for y in yrs}
    v = lambda y, k: A.get(y, {}).get(k, {}).get("value") if A.get(y, {}).get(k) else None
    st = lambda y, *ks: worst(*[A.get(y, {}).get(k, {}).get("status", "MISSING") if A.get(y, {}).get(k) else "MISSING" for k in ks])
    out = {k: {"actual": {}, "partial": {}, "consensus": {}, "unverified": {}} for k, *_ in VAL_METRICS}
    table = {}

    # ---------- 주가 ----------
    M = {}
    for r in store.current(code, kind="MARKET", item="price_close"):
        M.setdefault(r["fiscal_year"], []).append(r)
    rank = lambda r: (0 if r["source"].startswith("한국거래소") else 1 if (_trust_rank(r["source"]) is not None) else 2)
    price = {}
    for y, rows in M.items():
        r = sorted(rows, key=rank)[0]
        price[y] = r
        if y in periods["actual"] or y == b:
            out["price"]["actual"][y] = pt(r["value"], r["status"], r["source"], r["source_ref"],
                                           "기준일 종가" if y == b else "연말 종가", as_of=r["as_of"])

    def tax(y):
        pre, ni = v(y, "pretax"), v(y, "ni")
        if pre and pre > 0 and ni is not None:
            return _clip((pre - ni) / pre, *a["세율_범위"]), "실효세율"
        return a["기본_세율"], "기본세율"

    ic = lambda y: (v(y, "eq") + v(y, "debt") - v(y, "cash")) if None not in (v(y, "eq"), v(y, "debt"), v(y, "cash")) else None

    # ---------- 과거 연도 ----------
    fcff_hist = {}
    for y in periods["helper"]:          # 보조연도 현금흐름(첫해 DCF 3년 평균용)
        t0, _ = tax(y)
        if None not in (v(y, "ocf"), v(y, "capex_ppe")):
            fcff_hist[y] = v(y, "ocf") + (v(y, "interest_paid") or 0) * (1 - t0) - v(y, "capex_ppe") - (v(y, "capex_int") or 0)
    for y in periods["actual"]:
        row = {}
        rf, rf_y = _rf(a, y); ke = rf + erp
        t, t_how = tax(y)
        row.update(rf=rf, rf_year=rf_y, ke=ke, tax=t, tax_how=t_how)
        # ROIC
        ic0, ic1 = ic(y - 1), ic(y)
        if v(y, "op") is not None and ic0 and ic1 and (ic0 + ic1) > 0:
            nopat = v(y, "op") * (1 - t)
            roic = nopat / ((ic0 + ic1) / 2)
            row.update(nopat=nopat, ic=ic1, roic=roic)
            out["roic"]["actual"][y] = pt(roic * 100, st(y, "op", "eq", "debt", "cash"), "계산(DART)", None,
                                          f"세후영업이익 {nopat/EOK:,.0f}억(세율 {t*100:.1f}% {t_how}) ÷ 평균 투하자본 {(ic0+ic1)/2/EOK:,.0f}억")
        # WACC
        d0, d1 = v(y - 1, "debt"), v(y, "debt")
        ip = v(y, "interest_paid")
        if ip is not None and d0 is not None and d1 and (d0 + d1) > 0:
            kd = _clip(ip / ((d0 + d1) / 2), *a["타인자본비용_범위"]); kd_how = "이자지급÷평균차입부채"
        else:
            kd = rf + a["타인자본비용_기본가산"]; kd_how = f"국고채+{a['타인자본비용_기본가산']*100:.1f}%p(이자 자료 없음)"
        mcap = metrics["market_cap"]["actual"].get(y)
        E = mcap["v"] * EOK if mcap else v(y, "eq")
        if E and d1 is not None and E > 0:
            ke_w = ke
            if a.get("DCF_하마다_베타"):   # β 1을 무부채 베타로 보고 부채비율만큼 올림
                ke_w = rf + (1 + (1 - t) * d1 / E) * erp
                row["ke_levered"] = ke_w
            wacc = E / (E + d1) * ke_w + d1 / (E + d1) * kd * (1 - t)
            row.update(kd=kd, kd_how=kd_how, E=E, D=d1, wacc=wacc, E_how="시가총액" if mcap else "장부 자본")
            out["wacc"]["actual"][y] = pt(wacc * 100, worst(st(y, "debt"), mcap["status"] if mcap else "UNVERIFIED"), "계산", None,
                                          f"E {E/EOK:,.0f}억×Ke {ke_w*100:.2f}% + D {d1/EOK:,.0f}억×Kd {kd*100:.2f}%×(1−{t*100:.1f}%) ÷ {(E+d1)/EOK:,.0f}억")
        # FCFF
        if None not in (v(y, "ocf"), v(y, "capex_ppe")):
            fcff_hist[y] = v(y, "ocf") + (ip or 0) * (1 - t) - v(y, "capex_ppe") - (v(y, "capex_int") or 0)
            row["fcff"] = fcff_hist[y]
        table[y] = row

    # ---------- 공통 계산 함수 ----------
    def srim(bps, roe, ke):
        vals = {wv: bps + bps * (roe - ke) * wv / (1 + ke - wv) for wv in a["S-RIM_지속계수_w"]}
        return vals[a["S-RIM_대표_w"]], min(vals.values()), max(vals.values())

    def dcf(y_last, wacc, gs, shares, debt, cash):
        hist = [fcff_hist[k] for k in (y_last, y_last - 1, y_last - 2) if k in fcff_hist]
        if len(hist) < 2:
            return None, "현금흐름 자료 부족"
        base = sum(hist) / len(hist)
        if base <= 0:
            return None, f"최근 {len(hist)}년 평균 FCFF {base/EOK:,.0f}억 ≤ 0 → 계산 불가"
        if wacc <= g + 0.005:
            return None, "WACC가 영구성장률보다 충분히 크지 않음"
        f1, f2 = base * (1 + gs), base * (1 + gs) ** 2
        tv = f2 * (1 + g) / (wacc - g)
        ev = f1 / (1 + wacc) + (f2 + tv) / (1 + wacc) ** 2
        eq = ev - (debt - cash)
        return eq / shares, (f"평균 FCFF {base/EOK:,.0f}억, 2년 성장 {gs*100:.1f}%, 영구 {g*100:.1f}%, WACC {wacc*100:.2f}% → "
                             f"기업가치 {ev/EOK:,.0f}억 − 순차입 {(debt-cash)/EOK:,.0f}억 = {eq/EOK:,.0f}억 ÷ {shares:,.0f}주")

    roe = {y: p["v"] / 100 for y, p in metrics["roe"]["actual"].items()}

    def roe_w(y):
        ks = [(y, 3), (y - 1, 2), (y - 2, 1)]
        got = [(roe[k], wt) for k, wt in ks if k in roe]
        return sum(r * wt for r, wt in got) / sum(wt for _, wt in got) if got else None

    # ---------- 과거 연도 주당가치 ----------
    for y in periods["actual"]:
        row = table[y]
        n_bps, _, _ = shares_info(store, code, y)
        bps = metrics["bps"]["actual"].get(y)
        rw = roe_w(y)
        if bps and rw is not None:
            s, lo, hi = srim(bps["v"], rw, row["ke"])
            out["srim"]["actual"][y] = {**pt(s, bps["status"], "계산", None,
                                             f"BPS {bps['v']:,.0f} + BPS×(ROE {rw*100:.2f}% − Ke {row['ke']*100:.2f}%)×w÷(1+Ke−w), w={a['S-RIM_대표_w']}"),
                                        "lo": lo, "hi": hi}
        if "wacc" in row and n_bps and v(y, "debt") is not None:
            val, how = dcf(y, row["wacc"], 0.0, n_bps, v(y, "debt"), v(y, "cash") or 0)
            row["dcf_note"] = how
            if val is not None:
                out["dcf"]["actual"][y] = pt(val, st(y, "ocf", "debt"), "계산", None, how)

    # ---------- 상대가치 기준 배수(과거 10년 중앙값) ----------
    mult = {"per": [], "pbr": [], "psr": []}
    for y in periods["actual"]:
        p = price.get(y)
        if not p:
            continue
        n_bps, _, _ = shares_info(store, code, y)
        eps, bps, rev = metrics["eps"]["actual"].get(y), metrics["bps"]["actual"].get(y), v(y, "revenue")
        if eps and eps["v"] > 0 and p["value"] / eps["v"] <= a["PER_상한"]:
            mult["per"].append(p["value"] / eps["v"])
        if bps and bps["v"] > 0:
            mult["pbr"].append(p["value"] / bps["v"])
        if rev and n_bps:
            mult["psr"].append(p["value"] / (rev / n_bps))
        table[y].update({k: (p["value"] / x) for k, x in (("per", eps["v"] if eps and eps["v"] > 0 else None),
                                                           ("pbr", bps["v"] if bps else None),
                                                           ("psr", rev / n_bps if rev and n_bps else None)) if x})
    med = {k: (statistics.median(vs) if len(vs) >= 3 else None) for k, vs in mult.items()}

    def rel(slot, y, eps, bps, sps, status, src):
        if med["per"] and eps is not None and eps > 0:
            out["per_v"][slot][y] = pt(eps * med["per"], status, src, None, f"EPS {eps:,.0f} × 기준 PER {med['per']:.2f}배")
        if med["pbr"] and bps:
            out["pbr_v"][slot][y] = pt(bps * med["pbr"], status, src, None, f"BPS {bps:,.0f} × 기준 PBR {med['pbr']:.2f}배")
        if med["psr"] and sps:
            out["psr_v"][slot][y] = pt(sps * med["psr"], status, src, None, f"주당매출 {sps:,.0f} × 기준 PSR {med['psr']:.3f}배")

    for y in periods["actual"]:
        n_bps, _, _ = shares_info(store, code, y)
        eps, bps, rev = metrics["eps"]["actual"].get(y), metrics["bps"]["actual"].get(y), v(y, "revenue")
        rel("actual", y, eps["v"] if eps else None, bps["v"] if bps else None, rev / n_bps if rev and n_bps else None,
            worst(eps["status"] if eps else None, bps["status"] if bps else None), "계산(과거 배수)")

    # ---------- 올해·전망(컨센서스 기반) ----------
    last = periods["actual"][-1]
    n_now = None
    for r in store.current(code, fiscal_year=b, kind="MARKET", item="shares"):
        n_now = r["value"]
    n_now = n_now or shares_info(store, code, last)[0]
    for y in [b] + periods["future"]:
        c = {k: metrics[k]["consensus"].get(y) for k in ("eps", "bps", "roe", "revenue")}
        cst = worst(*[p["status"] for p in c.values() if p]) if any(c.values()) else None
        if not any(c.values()):
            continue
        sps = c["revenue"]["v"] * EOK / n_now if c["revenue"] and n_now else None
        rel("consensus", y, c["eps"]["v"] if c["eps"] else None, c["bps"]["v"] if c["bps"] else None, sps, cst, "계산(컨센서스)")
        rf, _ = _rf(a, y); ke = rf + erp
        bps_in = c["bps"]["v"] if c["bps"] and c["bps"]["status"] == "VERIFIED" else (metrics["bps"]["partial"].get(b, {}).get("v") if y == b else None)
        roe_in = c["roe"]["v"] / 100 if c["roe"] else roe_w(last)
        if bps_in and roe_in is not None:
            s, lo, hi = srim(bps_in, roe_in, ke)
            out["srim"]["consensus"][y] = {**pt(s, cst, "계산(컨센서스)", None,
                                                f"BPS {bps_in:,.0f} + BPS×(ROE {roe_in*100:.2f}% − Ke {ke*100:.2f}%)×w÷(1+Ke−w)"), "lo": lo, "hi": hi}
    # 올해 DCF: 기준일 시가총액으로 WACC, 컨센서스 매출 성장률로 2년 성장
    if last in table and "wacc" in table[last] and n_now:
        row = table[last]
        mc = metrics["market_cap"]["actual"].get(b)
        E = mc["v"] * EOK if mc else row["E"]
        rf, _ = _rf(a, b); ke = rf + erp
        D = v(last, "debt")
        ke_b = rf + (1 + (1 - row["tax"]) * D / E) * erp if a.get("DCF_하마다_베타") else ke
        wacc_b = E / (E + D) * ke_b + D / (E + D) * row["kd"] * (1 - row["tax"])
        r0, r2 = metrics["revenue"]["actual"].get(last), metrics["revenue"]["consensus"].get(b + 1)
        gs = ((r2["v"] / r0["v"]) ** 0.5 - 1) if (r0 and r2 and r0["v"] > 0) else 0.0
        gs = _clip(gs, -0.1, 0.15)
        val, how = dcf(last, wacc_b, gs, n_now, D, v(last, "cash") or 0)
        table[b] = {"ke": ke, "wacc": wacc_b, "E": E, "D": D, "dcf_note": how, "gs": gs}
        if val is not None:
            out["dcf"]["consensus"][b] = pt(val, worst(mc["status"] if mc else None, "UNVERIFIED" if not r2 else r2["status"]),
                                            "계산(기준일)", None, how)
    # ---------- DCF 범위 밖 표시(지우지 않음) ----------
    lim = a.get("DCF_주가대비_상한", 3.0)
    for slot in ("actual", "consensus"):
        for y, p in out["dcf"][slot].items():
            ref = price.get(y)
            why = None
            if p["v"] <= 0:
                why = "주주가치 0 이하: 순차입금이 계산된 기업가치보다 큼"
            elif ref and p["v"] > lim * ref["value"]:
                why = f"주가의 {p['v']/ref['value']:.1f}배(표시 상한 {lim:.0f}배 초과): WACC와 영구성장률 차이가 작고 차입이 많아 결과가 과도하게 민감"
            if why:
                p["flag"] = why
                p["cap"] = lim * ref["value"] if ref else None
                table.setdefault(y, {})["dcf_flag"] = {"v": p["v"], "why": why}

    # ---------- 적정가 중앙값 ----------
    for slot in ("actual", "consensus"):
        ys = set()
        for k in ("srim", "dcf", "per_v", "pbr_v", "psr_v"):
            ys |= set(out[k][slot])
        for y in ys:
            got = [(k, out[k][slot][y]) for k in ("srim", "dcf", "per_v", "pbr_v", "psr_v")
                   if y in out[k][slot] and not out[k][slot][y].get("flag")]   # 범위 밖 DCF는 중앙값에서 뺌
            if len(got) >= 2:
                m = statistics.median([p["v"] for _, p in got])
                out["fair"][slot][y] = pt(m, worst(*[p["status"] for _, p in got]), "계산", None,
                                          f"{len(got)}개 방법 중앙값: " + ", ".join(f"{dict((k2, n) for k2, n, *_ in VAL_METRICS)[k]} {p['v']:,.0f}" for k, p in got))
    assumptions = {"ERP": erp, "g": g, "w": a["S-RIM_지속계수_w"], "w_main": a["S-RIM_대표_w"], "tax_default": a["기본_세율"],
                   "dcf_cap": lim, "hamada": bool(a.get("DCF_하마다_베타")), "multiples": med, "multiples_n": {k: len(vs) for k, vs in mult.items()},
                   "rf_source": a["국고채10년_연평균"].get("_출처", "")}
    return out, table, assumptions
