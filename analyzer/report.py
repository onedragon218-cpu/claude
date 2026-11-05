"""DB → 화면용 자료(JSON) → HTML 한 장(화면 A~F)."""
import json
import os

from . import config, db

FLOW = {"revenue", "eps", "roe"}           # 현재 연도 부분 실적을 연간 값처럼 그리면 안 되는 지표
STOCK = {"bps", "debt_ratio", "market_cap"}  # 시점 값(현재 연도에 최신 실제치를 쓸 수 있음)


def _corr(metric, year, raw):
    """컨센서스 원래 값과 보정 값. 매출·EPS는 각 계수, 영업이익률은 영업이익 계수 ÷ 매출 계수, ROE는 EPS 계수,
    BPS는 이미 쌓인 자본이라 보정하지 않음."""
    from . import calibration
    if raw is None:
        return {"value": None, "raw": None, "factor": 1.0, "why": None}
    if metric in ("revenue", "eps"):
        f, why = calibration.factor(metric, year)
    elif metric == "op_margin":
        fo, w1 = calibration.factor("op_income", year)
        fr, _ = calibration.factor("revenue", year)
        f, why = (fo / fr if fr else 1.0), f"영업이익 계수 ÷ 매출 계수 ({w1})"
    elif metric == "roe":
        f, why = calibration.factor("eps", year)
    else:
        f, why = 1.0, "보정 대상 아님"
    return {"value": raw * f, "raw": raw, "factor": f, "why": why}


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
                "est": None if est is None else dict(_corr(m, y, est["value"]), provider=est["provider"],
                                                     as_of=est["as_of"], n=est["n_estimates"],
                                                     source_ref=est["source_ref"]),
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
        "growth": growth_table(conn, P),
        "calibration": calibration_info(P),
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

    def cash_of(y, per="FY", prev=None):
        """현금 잔액과 증감 원인(억원). 증감 = 영업 + 투자 + 재무 + 환율, 나머지는 '기타·차이'로 따로 둔다."""
        e = lambda v: None if v is None else v / 1e8
        c_only, c_tot = bk.v(y, "acc_cash_only", per), bk.v(y, "acc_cash", per)
        cfo, cfi, cff, fx = (bk.v(y, k, per) for k in ("acc_ocf", "acc_cfi", "acc_cff", "acc_fx"))
        p_only = prev if prev is not None else bk.v(y - 1, "acc_cash_only")
        chg = c_only - p_only if c_only is not None and p_only is not None else None
        parts = None if None in (cfo, cfi, cff) else cfo + cfi + cff + (fx or 0)
        return {"cash_only": e(c_only), "cash_total": e(c_tot), "cash_chg": e(chg), "cfo": e(cfo), "cfi": e(cfi),
                "cff": e(cff), "fx": e(fx), "cash_other": e(chg - parts) if chg is not None and parts is not None else None}

    for y in P["past"]:
        ip = (bk.v(y, "acc_int_paid") or 0) + (bk.v(y, "acc_int_paid_fin") or 0)
        icr, why = icr_of(bk.v(y, "acc_op"), ip)
        r = vr.get(y) or {}
        nd = None if r.get("debt") is None else (r["debt"] - (r.get("cash") or 0)) / 1e8
        rows.append({"year": y, "kind": "실적", "debt_ratio": bk.v(y, "debt_ratio"), "icr": icr, "icr_why": why,
                     "op": bk.v(y, "acc_op"), "interest": ip or None, "net_debt": nd,
                     "net_debt_why": None if nd is not None else ("차입금이 재무상태표에 따로 표시되지 않음" if r else "자료 없음"),
                     "ind_debt_ratio": bm["debt_ratio"].get(y), "ind_icr": bm["icr"].get(y),
                     "all_debt_ratio": bm["all_debt_ratio"].get(y), "all_icr": bm["all_icr"].get(y), **cash_of(y)})
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
                     "ind_debt_ratio": None, "ind_icr": None, "all_debt_ratio": None, "all_icr": None,
                     "flow_basis": f"{per} 누적(전년말 대비)", **cash_of(cy, per)})
    grade_leverage(conn, code, rows, bk, vr)
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
    lev = [r for r in rows if r.get("lev_grade") not in (None, "미확인") and not r.get("lev_halfyear")]
    lev_alert = lev[-1] if lev and lev[-1]["lev_grade"] in ("주의", "위험") else None
    from . import valuation as _v
    return {"rows": rows, "warnings": warn, "lev_alert": lev_alert,
            "lev_rules": _v.load_assumptions().get("순차입금_EBITDA_구간"),
            "nd_ratio_rules": _v.load_assumptions().get("순차입금비율_구간"), "industry": {"item": bm["item"], "name": bm["name"], "source": bm["source"],
            "missing_reason": bm["missing_reason"], "induty_code": induty}}


def da_of(bk, y, per="FY"):
    """감가상각비+무형자산상각비(+사용권자산 상각). 합계 계정이 있으면 그것. 없으면 None(미확인)."""
    tot = bk.v(y, "acc_da_total", per)
    if tot is not None:
        return tot, "감가상각비·상각비 합계"
    dep, amo, rou = bk.v(y, "acc_dep", per), bk.v(y, "acc_amort", per), bk.v(y, "acc_rou_dep", per)
    if dep is None:
        return None, None
    return dep + (amo or 0) + (rou or 0), "감가상각비" + (" + 무형자산상각비" if amo is not None else "") + \
        (" + 사용권자산상각비" if rou else "")


def grade_leverage(conn, code, rows, bk, vr):
    """순차입금 ÷ EBITDA 판정(실무 경험칙). 기준값은 assumptions.json '순차입금_EBITDA_구간'."""
    from . import db as _db, valuation as _v
    A = _v.load_assumptions()
    R = A.get("순차입금_EBITDA_구간", {})
    NR = A.get("순차입금비율_구간", {})
    safe, warn_at, danger_at = R.get("안전_미만", 1.0), R.get("주의_이상", 3.0), R.get("위험_이상", 4.0)
    icr_cut = R.get("이자보상배율_미만이면_주의를_위험으로", 1.0)
    streak_need = R.get("악화추세_연속연수", 2)
    # WiseReport EBITDA(교차 확인값): {연도: 원}
    wr = {o["fiscal_year"]: o["value"] for o in conn.execute(
        "SELECT * FROM observations WHERE stock_code=? AND metric='ebitda_wr' ORDER BY id", (code,))}
    prev_ratio, streak = None, 0
    for r in rows:
        y = r["year"]
        half = r["kind"] != "실적"
        per = None
        if half:
            per = bk.latest_interim(y)
        notes = []
        # EBITDA
        if not half:
            op = bk.v(y, "acc_op")
            da, da_src = da_of(bk, y)
        else:
            op_t = bk.ttm(y, "acc_op", per)
            da_now, da_src = da_of(bk, y, per)
            da_prev_fy, _ = da_of(bk, y - 1)
            da_prev_p, _ = da_of(bk, y - 1, per)
            if op_t is not None and None not in (da_now, da_prev_fy, da_prev_p):
                op, da = op_t, da_prev_fy + da_now - da_prev_p
                r["ebitda_basis"] = "최근 12개월(TTM)"
            else:
                op, da = bk.v(y, "acc_op", per), da_now
                r["lev_halfyear"] = True
                r["ebitda_basis"] = f"{per} 누적(연간 아님) — 반기 기준, 비교 주의(배수가 약 2배로 보임)"
        ebitda = op + da if op is not None and da is not None else None
        src = "DART(영업이익 + " + (da_src or "") + ")" if ebitda is not None else None
        if ebitda is None and not half and y in wr:
            ebitda, src = wr[y], "WiseReport(영업이익+감가상각비, 교차 확인값)"
            notes.append("DART에서 감가상각비를 찾지 못해 WiseReport EBITDA 사용")
        if ebitda is not None and y in wr and src.startswith("DART"):
            d = (ebitda - wr[y]) / abs(wr[y]) if wr[y] else None
            r["ebitda_wr"] = wr[y] / 1e8
            if d is not None and abs(d) > 0.05:
                notes.append(f"WiseReport EBITDA {wr[y] / 1e8:,.0f}억과 {d:+.0%} 차이(상각 범위 정의 차이 가능)")
        amo = bk.v(y, "acc_amort", per if half else "FY")
        if amo and op is not None and amo > abs(op) and op is not None:
            notes.append("무형자산상각비가 영업이익보다 커서 EBITDA가 커 보이고 배수가 낮게(안전하게) 나올 수 있음")
        r["ebitda"] = None if ebitda is None else ebitda / 1e8
        r["ebitda_src"] = src
        nd = r.get("net_debt")
        # 리스부채: 재무상태표 본문에 리스부채 줄이 없고 XBRL 주석에 있으면 순차입금에 더함
        lease_face = bk.v(y, "acc_lease_face", per if half else "FY")
        lease_x = bk.v(y, "acc_lease_xbrl", per if half else "FY")
        if nd is not None and not lease_face and lease_x:
            nd = nd + lease_x / 1e8
            r["net_debt"] = nd
            notes.append(f"재무상태표 본문에 없는 리스부채 {lease_x / 1e8:,.0f}억(XBRL 주석)을 순차입금에 포함")
        eq = bk.v(y, "acc_eq", per if half else "FY")
        r["nd_ratio"] = nd / (eq / 1e8) * 100 if nd is not None and eq else None
        nr = r["nd_ratio"]
        r["nd_ratio_zone"] = None if nr is None else ("안전" if nr <= NR.get("안전_이하", 50) else
                                                      "보통" if nr <= NR.get("주의_초과", 100) else "주의")
        # 판정
        ratio, grade = None, None
        if nd is None:
            grade = "미확인"; notes.append(r.get("net_debt_why") or "순차입금 계산 불가")
        elif ebitda is None:
            grade = "미확인"; notes.append("감가상각비를 원본에서 찾지 못함(단정하지 않음)")
        elif ebitda <= 0:
            grade = "위험"; notes.append("EBITDA 적자, 계산 불가")
        else:
            ratio = nd / r["ebitda"]
            grade = "안전" if ratio < safe else "보통" if ratio < warn_at else "주의" if ratio < danger_at else "위험"
            if ratio < 0:
                notes.append("순현금(현금이 빚보다 많음)")
        if grade == "주의" and r.get("icr") is not None and r["icr"] < icr_cut:
            grade = "위험"; notes.append(f"이자보상배율 {r['icr']:.2f}배(1배 미만)와 겹쳐 주의 → 위험")
        r["nd_ebitda"], r["lev_grade"] = ratio, grade
        # 악화 추세(실적 연도의 배수가 연속 상승)
        if ratio is not None and not r.get("lev_halfyear"):
            streak = streak + 1 if prev_ratio is not None and ratio > prev_ratio else 0
            prev_ratio = ratio
            r["lev_trend"] = streak >= streak_need and ratio > 0   # 순차입금이 있을 때만(순현금 축소는 별도 의미)
            if r["lev_trend"]:
                notes.append(f"배수 {streak}년 연속 상승(악화 추세)")
        else:
            r["lev_trend"] = False
        r["lev_notes"] = notes


def calibration_info(P):
    """화면 표시용 보정계수(연도·지표별)와 근거"""
    from . import calibration
    cal = calibration.load()
    out = {"available": bool(cal), "fiscal_year": cal.get("fiscal_year") if cal else None,
           "companies": cal.get("companies") if cal else None, "factors": {}}
    for y in [P["current"]] + P["future"]:
        for m in ("revenue", "op_income", "eps"):
            f, why = calibration.factor(m, y)
            out["factors"][f"{y}:{m}"] = {"factor": f, "why": why}
    if cal and cal.get("factors"):   # 측정값은 참고로만 보여 준다(적용은 assumptions.json 방식을 따름)
        name = {"revenue": "매출", "op_income": "영업이익", "eps": "EPS"}
        parts = []
        for m in ("revenue", "op_income", "eps"):
            b = cal["factors"].get(m) or {}
            seg = [f"{k}개월 전 ×{b[k]['median']:.2f}(n={b[k]['n']})" for k in ("11", "6", "1") if k in b]
            if seg:
                parts.append(f"{name[m]} " + ", ".join(seg))
        if parts:
            out["measured"] = (f"{cal['fiscal_year']}년 컨센서스 대비 실제 중앙값(기업 {cal.get('companies')}곳, "
                               f"WiseReport 컨센서스 추이): " + " / ".join(parts))
    return out


def growth_table(conn, P):
    """DB에 자료가 있는 기업의 작년 실적·올해~내후년 컨센서스(원래 값과 보정 값)"""
    from . import calibration, db as _db
    cy = P["current"]
    years = [cy - 1, cy, cy + 1, cy + 2]
    cal = calibration.load()
    out = []
    for c in conn.execute("SELECT c.stock_code, c.name FROM companies c WHERE EXISTS "
                          "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code) ORDER BY c.name"):
        code = c["stock_code"]
        fv = lambda y, k: (lambda r: None if r is None else r["value"])(_db.current_fact(conn, code, y, k))
        cons = {(r["target_year"], r["metric"]): dict(r) for r in _db.latest_consensus(conn, code)}
        row = {"code": code, "name": c["name"], "years": {}}
        for y in years:
            if y < cy:   # 작년: 실적
                rev = fv(y, "acc_revenue")
                op = fv(y, "acc_op")
                eps = fv(y, "acc_eps_basic")
                rev = rev / 1e8 if rev is not None else None
                op = op / 1e8 if op is not None else None
                row["years"][y] = {"kind": "실적", "raw": {"revenue": rev, "op_income": op, "eps": eps},
                                   "adj": {"revenue": rev, "op_income": op, "eps": eps}}
                continue
            raw = {m: (cons.get((y, m)) or {}).get("value") for m in ("revenue", "op_income", "eps")}
            if all(v is None for v in raw.values()):
                continue
            adj = {}
            for m, v in raw.items():
                f, _ = calibration.factor(m, y, cal=cal)
                adj[m] = None if v is None else v * f
            asof = max(((cons.get((y, m)) or {}).get("as_of") or "") for m in raw)
            row["years"][y] = {"kind": "컨센서스", "raw": raw, "adj": adj, "as_of": asof}
        for y, d in row["years"].items():
            for k in ("raw", "adj"):
                r = d[k]
                r["op_margin"] = r["op_income"] / r["revenue"] * 100 if r.get("revenue") and r.get("op_income") is not None else None
        out.append(row)
    return {"base_year": cy, "years": years, "rows": out}
