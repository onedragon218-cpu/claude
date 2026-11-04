"""DB → 화면용 자료(JSON) → HTML 한 장(화면 A~F)."""
import json
import os

from . import config, db

FLOW = {"revenue", "eps", "roe"}           # 현재 연도 부분 실적을 연간 값처럼 그리면 안 되는 지표
STOCK = {"bps", "debt_ratio", "market_cap"}  # 시점 값(현재 연도에 최신 실제치를 쓸 수 있음)


def payload(conn, code, base_year=None, last_run=None):
    P = config.periods(base_year)
    from . import valuation
    vres = valuation.run(conn, code, P)      # 가정 파일을 바꿨을 때도 바로 반영되도록 매번 다시 계산(수집 없음)
    comp = conn.execute("SELECT * FROM companies WHERE stock_code=?", (code,)).fetchone()
    cur = db.facts(conn, code)
    allf = db.facts(conn, code, current_only=False)
    years = sorted({r["fiscal_year"] for r in cur} | set(P["all"]))
    cons = [dict(r) for r in conn.execute("SELECT * FROM consensus WHERE stock_code=? ORDER BY as_of, target_year, metric",
                                          (code,))]
    latest = {}
    for r in db.latest_consensus(conn, code):
        latest[(r["target_year"], r["metric"])] = dict(r)
    # 영업이익률 추정치가 없고 매출·영업이익 추정치가 있으면 계산
    for (ty, m), r in list(latest.items()):
        if m == "op_income" and (ty, "revenue") in latest and (ty, "op_margin") not in latest and latest[(ty, "revenue")]["value"]:
            latest[(ty, "op_margin")] = dict(r, metric="op_margin", unit="%",
                                             value=r["value"] / latest[(ty, "revenue")]["value"] * 100,
                                             source_ref=(r["source_ref"] or "") + " (영업이익/매출 추정치로 계산)")
    series = {}
    for m in config.METRIC_KEYS:
        pts = {}
        for y in years:
            fy = next((r for r in cur if r["fiscal_year"] == y and r["metric"] == m and r["period"] == "FY"), None)
            part = next((r for p in ("Q3", "H1", "Q1", "ASOF") for r in cur
                         if r["fiscal_year"] == y and r["metric"] == m and r["period"] == p), None)
            est = latest.get((y, m))
            pts[y] = {
                "fy": _pt(fy), "partial": _pt(part),
                "est": None if est is None else {"value": est["value"], "provider": est["provider"],
                                                 "as_of": est["as_of"], "n": est["n_estimates"],
                                                 "source_ref": est["source_ref"]},
            }
        series[m] = pts
    status = {}
    for r in cur:
        if r["metric"].startswith(("acc_", "shares_", "treasury_", "price_")) or r["metric"] in config.METRIC_KEYS:
            status[r["status"]] = status.get(r["status"], 0) + 1
    # 필요한데 비어 있는 칸(MISSING)
    missing = []
    for y in P["past"]:
        for m in config.METRIC_KEYS:
            p = series[m].get(y, {}).get("fy")
            if p is None or p["value"] is None:
                missing.append({"year": y, "metric": m, "reason": (p or {}).get("note") or "자료 없음"})
    for y in P["future"] + [P["current"]]:
        for m in config.METRIC_KEYS:
            if m == "market_cap" and y > P["current"]:
                continue
            if series[m].get(y, {}).get("est") is None:
                missing.append({"year": y, "metric": m, "reason": "컨센서스 미확보"})
    val = [dict(r) for r in conn.execute("SELECT * FROM validation WHERE stock_code=? ORDER BY check_no, fiscal_year", (code,))]
    fetches = [dict(r) for r in conn.execute("SELECT * FROM fetch_log WHERE stock_code=? ORDER BY id DESC LIMIT 60", (code,))]
    last_collect = conn.execute("SELECT MAX(collected_at) FROM facts WHERE stock_code=? AND source!='derived'", (code,)).fetchone()[0]
    last_verify = conn.execute("SELECT MAX(verified_at) FROM facts WHERE stock_code=?", (code,)).fetchone()[0]
    companies = [dict(r) for r in conn.execute(
        "SELECT c.stock_code, c.name, COUNT(f.id) n, MAX(f.collected_at) last FROM companies c LEFT JOIN facts f "
        "ON f.stock_code=c.stock_code AND f.is_current=1 GROUP BY c.stock_code ORDER BY c.name")]
    return {
        "company": {"code": code, "name": comp["name"] if comp else code, "corp_code": comp["corp_code"] if comp else None},
        "periods": P, "years": years, "generated_at": db.now(),
        "metrics": [{"key": k, "name": n, "unit": u, "flow": k in FLOW, "stock": k in STOCK} for k, n, u in config.METRICS],
        "accounts": config.ACCOUNTS,
        "series": series,
        "facts": [_row(r) for r in cur],
        "history": [dict(r) for r in db.history(conn, code)],
        "consensus": cons,
        "validation": val,
        "status_counts": status,
        "missing": missing,
        "fetch_log": fetches,
        "last_collect": last_collect, "last_verify": last_verify,
        "companies": companies,
        "last_run": last_run,
        "screen": __import__("analyzer.screener", fromlist=["results"]).results(conn),
        "universe": [{"code": c, "name": n} for c, n in __import__("analyzer.universe", fromlist=["load"]).load()],
        "stability": stability(conn, code, P, vres["rows"], comp),
        "app_version": __import__("analyzer").__version__,
        "valuation": {"rows": vres["rows"], "assumptions": vres["assumptions"], "base_year": P["base_year"]},
        "total_facts": len(cur), "total_versions": len(allf),
    }


def _pt(r):
    if r is None:
        return None
    return {"value": r["value"], "status": r["status"], "note": r["status_note"], "period": r["period"],
            "source": r["source"], "source_ref": r["source_ref"], "as_of": r["as_of"], "data_type": r["data_type"],
            "collected_at": r["collected_at"]}


def _row(r):
    return {k: r[k] for k in ("fiscal_year", "period", "metric", "value", "unit", "basis", "data_type", "source",
                              "source_ref", "source_url", "collected_at", "as_of", "status", "status_note", "version")}


def render(data, out_path=None):
    tpl = open(os.path.join(os.path.dirname(__file__), "templates", "dashboard.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
    html = html.replace("__TITLE__", f"{data['company']['name']} 재무 분석")
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        open(out_path, "w", encoding="utf-8").write(html)
    return html


def stability(conn, code, P, vrows, comp):
    """재무 안정성: 부채비율·이자보상배율·순차입금과 한국은행 업종 평균, 경고."""
    from . import benchmark, valuation
    bk = valuation.Book(conn, code)
    induty = comp["induty_code"] if comp is not None and "induty_code" in comp.keys() else None
    bm = benchmark.for_company(induty)
    vr = {r["year"]: r for r in vrows}
    rows = []

    def icr_of(op, ip):
        if op is None:
            return None, "영업이익 없음"
        if not ip:
            return None, "이자지급 없음(무차입 또는 자료 없음) → 배율 계산 안 함"
        return op / ip, None

    for y in P["past"]:
        ip = (bk.v(y, "acc_int_paid") or 0) + (bk.v(y, "acc_int_paid_fin") or 0)
        icr, why = icr_of(bk.v(y, "acc_op"), ip)
        r = vr.get(y) or {}
        nd = None if r.get("debt") is None else (r["debt"] - (r.get("cash") or 0)) / 1e8
        rows.append({"year": y, "kind": "실적", "debt_ratio": bk.v(y, "debt_ratio"), "icr": icr, "icr_why": why,
                     "op": bk.v(y, "acc_op"), "interest": ip or None, "net_debt": nd,
                     "net_debt_why": None if nd is not None else ("차입금이 재무상태표에 따로 표시되지 않음" if r else "자료 없음"),
                     "ind_debt_ratio": bm["debt_ratio"].get(y), "ind_icr": bm["icr"].get(y),
                     "all_debt_ratio": bm["all_debt_ratio"].get(y), "all_icr": bm["all_icr"].get(y)})
    cy = P["current"]
    per = bk.latest_interim(cy)
    if per:
        op = bk.ttm(cy, "acc_op", per)
        ip = (bk.ttm(cy, "acc_int_paid", per) or 0) + (bk.ttm(cy, "acc_int_paid_fin", per) or 0)
        icr, why = icr_of(op, ip)
        r = vr.get(cy) or {}
        nd = None if r.get("debt") is None else (r["debt"] - (r.get("cash") or 0)) / 1e8
        rows.append({"year": cy, "kind": f"현재({per}말·최근12개월)", "debt_ratio": bk.v(cy, "debt_ratio", per),
                     "icr": icr, "icr_why": why, "op": op, "interest": ip or None, "net_debt": nd,
                     "net_debt_why": None if nd is not None else "자료 없음",
                     "ind_debt_ratio": None, "ind_icr": None, "all_debt_ratio": None, "all_icr": None})
    # 경고(사실만): 최근 실적 연도 기준
    warn = []
    act = [r for r in rows if r["kind"] == "실적"]
    last = next((r for r in reversed(act) if r["debt_ratio"] is not None), None)
    if last:
        if last["ind_debt_ratio"]:
            ratio = last["debt_ratio"] / last["ind_debt_ratio"]
            if ratio >= 1.5:
                warn.append(f"{last['year']}년 부채비율 {last['debt_ratio']:.0f}% = 업종 평균 {last['ind_debt_ratio']:.0f}%의 "
                            f"{ratio:.1f}배")
    low = [r["year"] for r in act if r["icr"] is not None and r["icr"] < 1]
    if low:
        warn.append("이자보상배율 1배 미만(영업이익으로 이자를 다 못 냄): " + ", ".join(f"{y}년" for y in low))
    cur = next((r for r in rows if r["kind"] != "실적"), None)
    if cur and cur["icr"] is not None and cur["icr"] < 1:
        warn.append(f"현재(최근 12개월) 이자보상배율 {cur['icr']:.2f}배")
    return {"rows": rows, "warnings": warn, "industry": {"item": bm["item"], "name": bm["name"], "source": bm["source"],
            "missing_reason": bm["missing_reason"], "induty_code": induty}}
