"""데이터 검증 모듈(지시서 8장, 10개 항목).

validate_accounts(): 원천 계정의 상태(VERIFIED/CONFLICT 등)를 정한다. 지표 계산보다 먼저 돈다.
run(): 10개 항목 검사 결과를 validation 테이블과 보고서(dict)로 남긴다.
"""
import statistics

from . import config, db

TOL = 0.005  # 0.5%


def _rel(a, b):
    if a is None or b is None:
        return None
    return abs(a - b) / max(abs(a), abs(b), 1e-9)


def validate_accounts(conn, code, P):
    """DART 원천 계정: 회계 등식(자산=부채+자본)이 맞고, 다른 출처와 어긋나지 않으면 VERIFIED.
    주가: 연말 거래일의 종가이면 VERIFIED. 역산 값은 항상 UNVERIFIED."""
    rows = conn.execute("SELECT * FROM facts WHERE stock_code=? AND is_current=1", (code,)).fetchall()
    by = {}
    for r in rows:
        by[(r["fiscal_year"], r["period"], r["metric"])] = r
    for r in rows:
        if r["status"] in ("STALE", "MISSING") or r["value"] is None:
            continue
        y, per, m = r["fiscal_year"], r["period"], r["metric"]
        if r["source"] == "DART" and m.startswith("acc_"):
            a, l, e = (by.get((y, per, k)) for k in ("acc_assets", "acc_liab", "acc_eq"))
            ident = None if not (a and l and e) else _rel(a["value"], l["value"] + e["value"])
            others = [o for o in db.observations(conn, code, y, m, per)
                      if o["source"] != "DART" and _rel(o["value"], r["value"]) > TOL
                      and abs(o["value"] - r["value"]) > 6e6]   # 억원 단위 반올림(±0.05억) 차이는 무시
            if others:
                db.set_status(conn, r["id"], "CONFLICT", f"{others[0]['source']} 값 {others[0]['value']:,.0f}와 불일치")
            elif ident is not None and ident > TOL:
                db.set_status(conn, r["id"], "CONFLICT", f"자산≠부채+자본(차이 {ident:.2%})")
            elif ident is not None and ident <= TOL and any(
                    o["source"] != "DART" for o in db.observations(conn, code, y, m, per)):
                db.set_status(conn, r["id"], "VERIFIED", "회계 등식 일치 · 다른 출처(WiseReport)와 일치")
            elif ident is None:
                db.set_status(conn, r["id"], "UNVERIFIED", (r["status_note"] or "") + " · 회계 등식 확인 불가")
            else:
                keep = "CORRECTED" if r["status"] == "CORRECTED" else "VERIFIED"
                db.set_status(conn, r["id"], keep, r["status_note"])
        elif r["source"] == "DART" and m.startswith(("shares_", "treasury_")):
            db.set_status(conn, r["id"], "VERIFIED", r["status_note"])
        elif m == "price_close" and per == "FY":
            ok = r["as_of"] and r["as_of"][5:] >= "12-20"
            db.set_status(conn, r["id"], "VERIFIED" if ok else "UNVERIFIED", r["status_note"])
    conn.commit()


def run(conn, code, P):
    run_at = db.now()
    conn.execute("DELETE FROM validation WHERE stock_code=?", (code,))   # 최신 검사 결과만 유지
    fm = db.fact_map(conn, code)
    out = []

    def add(no, name, y, metric, level, msg):
        out.append({"check_no": no, "check": name, "year": y, "metric": metric, "level": level, "message": msg})
        conn.execute("INSERT INTO validation (stock_code,run_at,check_no,check_name,fiscal_year,metric,level,message) "
                     "VALUES (?,?,?,?,?,?,?,?)", (code, run_at, no, name, y, metric, level, msg))

    def v(y, k):
        r = fm.get((y, k))
        return None if r is None else r["value"]

    past = P["past"]
    # 1 영업이익률 재계산
    for y in past:
        rev, op, om = v(y, "acc_revenue"), v(y, "acc_op"), v(y, "op_margin")
        if None in (rev, op, om):
            add(1, "영업이익률 재계산", y, "op_margin", "MISSING", "입력값 없음")
        elif abs(op / rev * 100 - om) > 0.01:
            add(1, "영업이익률 재계산", y, "op_margin", "ERROR", f"저장값 {om:.2f}% vs 재계산 {op / rev * 100:.2f}%")
    # 2 EPS와 순이익
    implied = {y: v(y, "acc_ni_owner") / v(y, "acc_eps_basic") for y in past
               if v(y, "acc_ni_owner") is not None and v(y, "acc_eps_basic")}
    for y, imp in implied.items():
        sh = None
        if v(y, "shares_common"):
            sh = v(y, "shares_common") + (v(y, "shares_pref") or 0) - (v(y, "treasury_common") or 0) - (v(y, "treasury_pref") or 0)
        if sh:
            d = _rel(imp, sh)
            if d > 0.03:
                add(2, "EPS-순이익 관계", y, "eps", "WARN", f"순이익/EPS={imp:,.0f}주, 유통주식수 {sh:,.0f}주(차이 {d:.1%})")
    if implied:
        med = statistics.median(implied.values())
        for y, imp in implied.items():
            d = _rel(imp, med)
            if d > 0.03 and abs(v(y, "acc_eps_basic")) >= 300:   # EPS가 작으면 반올림 오차가 커서 제외
                add(2, "EPS-순이익 관계", y, "eps", "WARN",
                    f"순이익/EPS={imp:,.0f}주, 기간 중앙값 {med:,.0f}주와 {d:.1%} 차이(주식 수 변동·우선주·일회성 확인)")
    if not any(v(y, "shares_common") for y in past):
        add(2, "EPS-순이익 관계", None, "eps", "INFO", "공식 주식 수 미확보 → DART 주식총수 현황 수집 후 정밀 대조")
    # 3 BPS
    for y in past:
        r = fm.get((y, "bps"))
        if r is None or r["value"] is None:
            add(3, "BPS-자본·주식수", y, "bps", "MISSING", "계산 불가")
        elif "역산" in (r["status_note"] or ""):
            add(3, "BPS-자본·주식수", y, "bps", "WARN", "공식 주식 수 없이 역산 주식수 사용")
    # 4 ROE 기준
    for y in past:
        r = fm.get((y, "roe"))
        if r is None or r["value"] is None:
            add(4, "ROE 산출 기준", y, "roe", "MISSING", "계산 불가")
        elif "평균 아님" in (r["status_note"] or ""):
            add(4, "ROE 산출 기준", y, "roe", "WARN", "전년 자본 없음 → 기말 자본 사용")
    add(4, "ROE 산출 기준", None, "roe", "INFO", "지배주주 순이익 / (전년말+당년말 지배주주 자본)/2. DART 공시 ROE와 정의가 다를 수 있음")
    # 5 부채비율
    for y in past:
        l, e, dr = v(y, "acc_liab"), v(y, "acc_eq"), v(y, "debt_ratio")
        if None in (l, e, dr):
            add(5, "부채비율 재계산", y, "debt_ratio", "MISSING", "입력값 없음")
        elif abs(l / e * 100 - dr) > 0.01:
            add(5, "부채비율 재계산", y, "debt_ratio", "ERROR", "저장값과 재계산 불일치")
    # 6 시가총액
    for y in past:
        r = fm.get((y, "market_cap"))
        if r is None or r["value"] is None:
            add(6, "시가총액=주가×주식수", y, "market_cap", "MISSING", "주가 또는 주식 수 없음")
        elif "역산" in (r["status_note"] or ""):
            add(6, "시가총액=주가×주식수", y, "market_cap", "WARN", "공식 주식 수 없이 역산 주식수 사용(주의)")
    # 7 정정 공시
    for d in conn.execute("SELECT * FROM disclosures WHERE stock_code=? AND is_correction=1", (code,)):
        add(7, "정정 공시", d["report_year"], None, "WARN", f"{d['report_nm']} ({d['rcept_no']})")
    for r in conn.execute("SELECT * FROM facts WHERE stock_code=? AND is_current=1 AND status IN ('STALE','CORRECTED')", (code,)):
        add(7, "정정 공시", r["fiscal_year"], r["metric"], "WARN", f"{r['status']}: {r['status_note']}")
    if not conn.execute("SELECT 1 FROM fetch_log WHERE source='dart_disclosures' AND stock_code=? AND ok=1", (code,)).fetchone():
        add(7, "정정 공시", None, None, "INFO", "DART 공시 목록을 아직 점검하지 못함(API 키 필요) — 정정 여부 미확인")
    # 8 실적·추정 혼합
    for r in conn.execute("SELECT * FROM facts WHERE stock_code=? AND is_current=1 AND data_type='ESTIMATE' AND fiscal_year<?",
                          (code, P["current"])):
        add(8, "실적·추정 혼합", r["fiscal_year"], r["metric"], "ERROR", "과거 연도에 추정치가 저장됨")
    for r in conn.execute("SELECT * FROM facts WHERE stock_code=? AND is_current=1 AND data_type IN ('ACTUAL','PARTIAL') "
                          "AND fiscal_year>?", (code, P["current"])):
        add(8, "실적·추정 혼합", r["fiscal_year"], r["metric"], "ERROR", "미래 연도에 실적이 저장됨")
    # 9 급등락
    lim = {"revenue": 0.5, "bps": 0.5, "market_cap": 1.0, "eps": 1.0}
    for m, L in lim.items():
        for y in past[1:]:
            a, b = v(y - 1, m), v(y, m)
            if a and b is not None and a > 0 and abs(b / a - 1) > L:
                add(9, "전년 대비 급등락", y, m, "WARN", f"{config.METRIC_NAME[m]} {a:,.1f} → {b:,.1f} ({b / a - 1:+.0%})")
            elif a and b is not None and (a > 0) != (b > 0):
                add(9, "전년 대비 급등락", y, m, "WARN", f"{config.METRIC_NAME[m]} 부호 전환 {a:,.1f} → {b:,.1f}")
    for m in ("op_margin", "roe"):
        for y in past[1:]:
            a, b = v(y - 1, m), v(y, m)
            if a is not None and b is not None and abs(b - a) > 10:
                add(9, "전년 대비 급등락", y, m, "WARN", f"{config.METRIC_NAME[m]} {a:.1f}% → {b:.1f}% ({b - a:+.1f}%p)")
    # 10 출처·보고서 간 불일치
    for r in conn.execute("SELECT fiscal_year, period, metric, COUNT(DISTINCT source_ref) n, MIN(value) mn, MAX(value) mx "
                          "FROM observations WHERE stock_code=? GROUP BY fiscal_year, period, metric HAVING n>1", (code,)):
        d = _rel(r["mn"], r["mx"])
        if d and d > TOL:
            add(10, "출처 간 불일치", r["fiscal_year"], r["metric"], "WARN",
                f"보고서·출처별 값 {r['mn']:,.0f} ~ {r['mx']:,.0f} (차이 {d:.1%}, 후속 보고서 재작성 가능)")
    conn.commit()
    summary = {}
    for o in out:
        summary[o["level"]] = summary.get(o["level"], 0) + 1
    return {"run_at": run_at, "summary": summary, "items": out}
