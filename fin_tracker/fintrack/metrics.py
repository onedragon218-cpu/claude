"""원값 → 7개 지표. 계산식과 입력값의 출처·상태를 함께 남긴다.

계열 구분(섞지 않음)
- actual     : 과거 10년 확정 실적(사업보고서)
- partial    : 올해 반기·분기 실제값(비율·시점 지표만. 매출 같은 누적값은 연간과 비교 불가라 표시만)
- consensus  : 검증된 출처의 전망(기본 표시)
- unverified : 원문 미확인 전망(켜야 보임)
- market     : 시장가격 기반 시가총액(과거 연말, 올해는 기준일)
"""
from . import config

EOK = 1e8
RANK = {"VERIFIED": 0, "CORRECTED": 1, "UNVERIFIED": 2, "STALE": 2, "CONFLICT": 3, "MISSING": 4}


def worst(*sts):
    sts = [s for s in sts if s]
    return max(sts, key=lambda s: RANK.get(s, 5)) if sts else "MISSING"


def pt(v, status, source, ref, formula, as_of="", note=""):
    return {"v": v, "status": status, "source": source, "ref": ref, "formula": formula, "as_of": as_of, "note": note}


def build(store, code, periods):
    face = store.company(code)["face_value"]
    b = periods["base"]
    A = {}
    for r in store.current(code, kind="ACTUAL", period="FY"):
        A.setdefault(r["fiscal_year"], {})[r["item"]] = r
    out = {k: {"actual": {}, "partial": {}, "consensus": {}, "unverified": {}} for k, _, _ in config.METRICS}

    # ---------- 과거 확정 실적 ----------
    price = {r["fiscal_year"]: r for r in store.current(code, item="price_close", kind="MARKET")}
    for y in periods["actual"]:
        a = A.get(y, {})
        g = lambda k: a[k]["value"] if k in a else None
        st = lambda *ks: worst(*[a[k]["status"] if k in a else "MISSING" for k in ks])
        ref = next((r["source_ref"] for r in a.values()), None)
        src = "DART 사업보고서"
        if g("revenue") is not None:
            out["revenue"]["actual"][y] = pt(g("revenue") / EOK, st("revenue"), src, ref, "연결 매출액")
        if g("op") is not None and g("revenue"):
            out["op_margin"]["actual"][y] = pt(g("op") / g("revenue") * 100, st("op", "revenue"), src, ref,
                                               f"영업이익 {g('op')/EOK:,.0f}억 ÷ 매출액 {g('revenue')/EOK:,.0f}억 × 100")
        if g("eps") is not None:
            out["eps"]["actual"][y] = pt(g("eps"), st("eps"), src, ref, "기본주당순이익(공시값)")
        shares = g("issued_capital") / face if g("issued_capital") else None
        if g("eq_owner") is not None and shares:
            out["bps"]["actual"][y] = pt(g("eq_owner") / shares, st("eq_owner", "issued_capital"), src, ref,
                                         f"지배자본 {g('eq_owner')/EOK:,.0f}억 ÷ (자본금÷액면가 {face:,}원 = {shares:,.0f}주)",
                                         note="자사주 미차감")
        prev = A.get(y - 1, {}).get("eq_owner")
        if g("ni_owner") is not None and g("eq_owner") and prev:
            avg = (g("eq_owner") + prev["value"]) / 2
            out["roe"]["actual"][y] = pt(g("ni_owner") / avg * 100, worst(st("ni_owner", "eq_owner"), prev["status"]), src, ref,
                                         f"지배순이익 {g('ni_owner')/EOK:,.1f}억 ÷ 평균 지배자본 {avg/EOK:,.0f}억 × 100")
        if g("liab") is not None and g("eq"):
            out["debt_ratio"]["actual"][y] = pt(g("liab") / g("eq") * 100, st("liab", "eq"), src, ref,
                                                f"부채총계 {g('liab')/EOK:,.0f}억 ÷ 자본총계 {g('eq')/EOK:,.0f}억 × 100")
        if y in price and shares:
            p = price[y]
            out["market_cap"]["actual"][y] = pt(p["value"] * shares / EOK, worst(p["status"], st("issued_capital")),
                                                p["source"], p["source_ref"],
                                                f"연말 종가 {p['value']:,.0f}원 × {shares:,.0f}주", as_of=f"{y} 연말")

    # ---------- 올해: 부분 실적 ----------
    P = {}
    for r in store.current(code, fiscal_year=b, kind="ACTUAL_PARTIAL"):
        P.setdefault(r["period"], {})[r["item"]] = r
    M = {r["item"]: r for r in store.current(code, fiscal_year=b, kind="MARKET")}
    for per, a in P.items():
        g = lambda k: a[k]["value"] if k in a else None
        ref, src = next(iter(a.values()))["source_ref"], next(iter(a.values()))["source"]
        label = {"H1": "상반기", "Q1": "1분기", "Q3": "3분기 누적"}.get(per, per)
        st = worst(*[r["status"] for r in a.values()])
        if g("op") is not None and g("revenue"):
            out["op_margin"]["partial"][b] = pt(g("op") / g("revenue") * 100, st, src, ref,
                                                f"{label} 영업이익 {g('op')/EOK:,.0f}억 ÷ 매출액 {g('revenue')/EOK:,.0f}억", as_of=label)
        if g("liab") is not None and g("eq"):
            out["debt_ratio"]["partial"][b] = pt(g("liab") / g("eq") * 100, st, src, ref,
                                                 f"{label} 말 부채 {g('liab')/EOK:,.0f}억 ÷ 자본 {g('eq')/EOK:,.0f}억", as_of=label)
        if g("eq_owner") is not None and "shares" in M:
            sh = M["shares"]["value"]
            out["bps"]["partial"][b] = pt(g("eq_owner") / sh, st, src, ref,
                                          f"{label} 말 지배자본 {g('eq_owner')/EOK:,.0f}억 ÷ {sh:,.0f}주", as_of=label)
        if g("revenue") is not None:
            out["revenue"]["partial"][b] = pt(g("revenue") / EOK, st, src, ref, f"{label} 누적 매출(연간과 직접 비교 불가)",
                                              as_of=label, note="누적값")
    if "market_cap" in M:
        m = M["market_cap"]
        f = f"기준일 종가 {M['price_close']['value']:,.0f}원 × {M['shares']['value']:,.0f}주" if "price_close" in M and "shares" in M else "화면 표시값"
        out["market_cap"]["actual"][b] = pt(m["value"] / EOK, m["status"], m["source"], m["source_ref"], f, as_of=m["as_of"],
                                            note="기준일 시가총액(연말 아님)")

    # ---------- 전망 ----------
    C = {}
    for r in store.current(code, kind="CONSENSUS"):
        if r["fiscal_year"] >= b:
            C.setdefault(r["fiscal_year"], {}).setdefault(r["item"], []).append(r)
    # 기본 화면(consensus): 신뢰 출처(WiseReport/FnGuide). 상태가 CONFLICT여도 그대로 표시해 경고한다.
    # 미검증(unverified): 원문을 확인하지 못한 출처. '미검증 포함'을 켰을 때만 보인다.
    trusted = lambda r: r["source"].startswith("WiseReport")
    for y, items in C.items():
        def pick(item, want_trusted):
            rows = sorted((r for r in items.get(item, []) if trusted(r) == want_trusted),
                          key=lambda r: r["collected_at"], reverse=True)
            return rows[0] if rows else None
        for want, slot in ((True, "consensus"), (False, "unverified")):
            for item, conv, form in (("revenue", EOK, "매출액 전망"), ("eps", 1, "EPS 전망"),
                                     ("bps", 1, "BPS 전망"), ("roe", 1, "ROE 전망")):
                r = pick(item, want)
                if r:
                    out[item][slot][y] = pt(r["value"] / conv, r["status"], r["source"], r["source_ref"], form,
                                            as_of=r["as_of"], note=r["status_note"] or "")
            op, rev = pick("op", want), pick("revenue", want)
            if op and rev and op["source"] == rev["source"]:
                out["op_margin"][slot][y] = pt(op["value"] / rev["value"] * 100, worst(op["status"], rev["status"]),
                                               op["source"], op["source_ref"],
                                               f"영업이익 전망 {op['value']/EOK:,.0f}억 ÷ 매출액 전망 {rev['value']/EOK:,.0f}억",
                                               as_of=op["as_of"], note=op["status_note"] or "")
    return out
