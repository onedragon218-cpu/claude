"""원값 → 7개 지표. 계산식과 입력값의 출처·상태를 값마다 남긴다.

계열 구분(섞지 않음)
- actual     : 과거 10년 확정 실적(사업보고서) + 시가총액(과거 연말, 올해는 기준일)
- partial    : 올해 최근 분기·반기 실제값(비율·시점 지표만 그래프에 표시)
- consensus  : 신뢰 출처(FnGuide → WiseReport 순) 전망. CONFLICT여도 상태를 붙여 표시
- unverified : 원문 미확인 전망('미검증 함께 보기'를 켜야 보임)
"""
from . import config
from .validate import shares_info

EOK = 1e8
RANK = {"VERIFIED": 0, "CORRECTED": 1, "UNVERIFIED": 2, "STALE": 2, "CONFLICT": 3, "MISSING": 4}


def worst(*sts):
    sts = [s for s in sts if s]
    return max(sts, key=lambda s: RANK.get(s, 5)) if sts else "MISSING"


def pt(v, status, source, ref, formula, as_of="", note=""):
    return {"v": v, "status": status, "source": source, "ref": ref, "formula": formula, "as_of": as_of, "note": note or ""}


def _trust_rank(src):
    for i, t in enumerate(config.TRUSTED):
        if src.startswith(t):
            return i
    return None


def build(store, code, periods):
    b = periods["base"]
    A = {}
    for r in store.current(code, kind="ACTUAL", period="FY"):
        A.setdefault(r["fiscal_year"], {})[r["item"]] = r
    out = {k: {"actual": {}, "partial": {}, "consensus": {}, "unverified": {}} for k, _, _ in config.METRICS}
    M = {}
    for r in store.current(code, kind="MARKET"):
        M.setdefault(r["fiscal_year"], {}).setdefault(r["item"], []).append(r)

    # ---------- 과거 확정 실적 ----------
    for y in periods["actual"]:
        a = A.get(y, {})
        g = lambda k: a[k]["value"] if k in a else None
        st = lambda *ks: worst(*[a[k]["status"] if k in a else "MISSING" for k in ks])
        ref = next((r["source_ref"] for k, r in a.items() if not k.startswith("shares_")), None)
        src = "DART 사업보고서"
        if g("revenue") is not None:
            out["revenue"]["actual"][y] = pt(g("revenue") / EOK, st("revenue"), src, ref, "매출액(공시)")
        if g("op") is not None and g("revenue"):
            out["op_margin"]["actual"][y] = pt(g("op") / g("revenue") * 100, st("op", "revenue"), src, ref,
                                               f"영업이익 {g('op')/EOK:,.0f}억 ÷ 매출액 {g('revenue')/EOK:,.0f}억 × 100")
        if g("eps") is not None:
            out["eps"]["actual"][y] = pt(g("eps"), st("eps"), src, ref, "기본주당순이익(공시값)")
        n_bps, n_end, how = shares_info(store, code, y)
        if g("eq_owner") is not None and n_bps:
            out["bps"]["actual"][y] = pt(g("eq_owner") / n_bps, st("eq_owner"), src, ref,
                                         f"지배자본 {g('eq_owner')/EOK:,.0f}억 ÷ {n_bps:,.0f}주 ({how})")
        prev = A.get(y - 1, {}).get("eq_owner")
        if g("ni_owner") is not None and g("eq_owner") and prev:
            avg = (g("eq_owner") + prev["value"]) / 2
            out["roe"]["actual"][y] = pt(g("ni_owner") / avg * 100, worst(st("ni_owner", "eq_owner"), prev["status"]), src, ref,
                                         f"지배순이익 {g('ni_owner')/EOK:,.1f}억 ÷ 평균 지배자본 {avg/EOK:,.0f}억 × 100")
        if g("liab") is not None and g("eq"):
            out["debt_ratio"]["actual"][y] = pt(g("liab") / g("eq") * 100, st("liab", "eq"), src, ref,
                                                f"부채총계 {g('liab')/EOK:,.0f}억 ÷ 자본총계 {g('eq')/EOK:,.0f}억 × 100")
        mk = M.get(y, {})
        cap = next((r for r in mk.get("market_cap", []) if r["source"].startswith("한국거래소")), None)
        if cap:
            out["market_cap"]["actual"][y] = pt(cap["value"] / EOK, cap["status"], cap["source"], cap["source_ref"],
                                                "한국거래소 연말 시가총액", as_of=cap["as_of"])
        elif mk.get("price_close") and n_end:
            p = mk["price_close"][0]
            out["market_cap"]["actual"][y] = pt(p["value"] * n_end / EOK, worst(p["status"], "UNVERIFIED"), p["source"], p["source_ref"],
                                                f"연말 종가 {p['value']:,.0f}원 × {n_end:,.0f}주 (계산값)", as_of=p["as_of"] or f"{y} 연말",
                                                note="계산값")

    # ---------- 올해: 부분 실적 ----------
    P = {}
    for r in store.current(code, fiscal_year=b, kind="ACTUAL_PARTIAL"):
        P.setdefault(r["period"], {})[r["item"]] = r
    mkb = M.get(b, {})
    shares_now = next((r["value"] for r in sorted(mkb.get("shares", []), key=lambda r: _trust_rank(r["source"]) or 9)), None)
    for per, a in P.items():
        g = lambda k: a[k]["value"] if k in a else None
        first = next(iter(a.values()))
        ref, src = first["source_ref"], first["source"]
        label = {"H1": "상반기", "Q1": "1분기", "Q3": "3분기 누적"}.get(per, per)
        st = worst(*[r["status"] for r in a.values()])
        if g("op") is not None and g("revenue"):
            out["op_margin"]["partial"][b] = pt(g("op") / g("revenue") * 100, st, src, ref,
                                                f"{label} 영업이익 {g('op')/EOK:,.0f}억 ÷ 매출액 {g('revenue')/EOK:,.0f}억", as_of=label)
        if g("liab") is not None and g("eq"):
            out["debt_ratio"]["partial"][b] = pt(g("liab") / g("eq") * 100, st, src, ref,
                                                 f"{label} 말 부채 {g('liab')/EOK:,.0f}억 ÷ 자본 {g('eq')/EOK:,.0f}억", as_of=label)
        if g("eq_owner") is not None and shares_now:
            out["bps"]["partial"][b] = pt(g("eq_owner") / shares_now, st, src, ref,
                                          f"{label} 말 지배자본 {g('eq_owner')/EOK:,.0f}억 ÷ {shares_now:,.0f}주", as_of=label)
        if g("revenue") is not None:
            out["revenue"]["partial"][b] = pt(g("revenue") / EOK, st, src, ref, f"{label} 누적 매출(연간과 직접 비교 불가)",
                                              as_of=label, note="누적값")
    caps = sorted(mkb.get("market_cap", []), key=lambda r: _trust_rank(r["source"]) if _trust_rank(r["source"]) is not None else 9)
    if caps:
        m = caps[0]
        same = {k: next((r for r in mkb.get(k, []) if r["source"] == m["source"]), None) for k in ("price_close", "shares")}
        f = (f"기준일 종가 {same['price_close']['value']:,.0f}원 × {same['shares']['value']:,.0f}주"
             if same["price_close"] and same["shares"] else "화면 표시값")
        out["market_cap"]["actual"][b] = pt(m["value"] / EOK, m["status"], m["source"], m["source_ref"], f,
                                            as_of=m["as_of"], note="기준일 시가총액(연말 아님)")

    # ---------- 전망 ----------
    C = {}
    for r in store.current(code, kind="CONSENSUS"):
        if r["fiscal_year"] >= b:
            C.setdefault(r["fiscal_year"], {}).setdefault(r["item"], []).append(r)
    for y, items in C.items():
        def pick(item, want_trusted):
            rows = [r for r in items.get(item, []) if (_trust_rank(r["source"]) is not None) == want_trusted]
            rows.sort(key=lambda r: ((_trust_rank(r["source"]) or 0), r["collected_at"]), reverse=False)
            if want_trusted and rows:   # 가장 신뢰 높은 출처 중 가장 최근 수집분
                best = min(_trust_rank(r["source"]) for r in rows)
                rows = sorted([r for r in rows if _trust_rank(r["source"]) == best], key=lambda r: r["collected_at"], reverse=True)
            return rows[0] if rows else None
        for want, slot in ((True, "consensus"), (False, "unverified")):
            for item, conv in (("revenue", EOK), ("eps", 1), ("bps", 1), ("roe", 1), ("debt_ratio", 1)):
                r = pick(item, want)
                if r:
                    out[item][slot][y] = pt(r["value"] / conv, r["status"], r["source"], r["source_ref"],
                                            f"{dict((k, n) for k, n, _ in config.METRICS)[item]} 전망", as_of=r["as_of"], note=r["status_note"])
            op, rev = pick("op", want), pick("revenue", want)
            if op and rev and op["source"] == rev["source"] and rev["value"]:
                out["op_margin"][slot][y] = pt(op["value"] / rev["value"] * 100, worst(op["status"], rev["status"]),
                                               op["source"], op["source_ref"],
                                               f"영업이익 전망 {op['value']/EOK:,.0f}억 ÷ 매출액 전망 {rev['value']/EOK:,.0f}억",
                                               as_of=op["as_of"], note=op["status_note"])
    return out
