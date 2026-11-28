"""전수 점검(2026-11-28 사용자 승인) — DB에 있는 모든 회사를 한 번에 대조해 '원본을 그대로 보여 주는가 · 계산식대로 계산했는가'를 확인

원칙: 이 점검은 아무것도 고치지 않는다(DB 읽기만). 결과는 데이터 폴더 state/audit_latest.json에 저장하고 화면 B탭에서 본다.
등급
 · 오류  — 앱이 원본이나 자기 계산식과 다른 값을 들고 있음(앱이 고쳐야 함)
 · 확인  — 원본끼리·출처끼리 맞지 않음(어느 쪽이 맞는지 사람이 확인해야 함, 회사 사정일 수도 있음)
 · 참고  — 판정에 영향을 줄 수 있는 상태(결측·출처 혼재 등)
점검 항목은 CHECKS. 허용 오차는 각 항목에 적힘.
리서치 참고자료이며 투자자문이 아님
"""
import datetime as dt
import json
import os

from . import config, db

LEVELS = ("오류", "확인", "참고")
CHECKS = [
    # (번호, 이름, 등급, 설명)
    ("A1", "대표값 = DART 원본", "오류", "앱이 쓰는 DART 계정 값이 같은 보고서의 원본 관측값과 같은가(0.5원 이내)"),
    ("A2", "자산 = 부채 + 자본", "확인", "자산총계와 부채총계 + 자본총계의 차이가 자산의 0.5% 이내인가"),
    ("A3", "지배 + 비지배 = 당기순이익", "확인", "지배순이익 + 비지배지분 순이익이 당기순이익과 같은가(0.5% 또는 1억 이내)"),
    ("A4", "순이익·EPS 부호", "확인", "지배순이익과 기본 EPS의 부호가 같은가(다르면 앱은 '자료 충돌'로 판정에서 뺌)"),
    ("A5", "순이익 ÷ EPS ≈ 주식 수", "확인", "지배순이익 ÷ EPS(가중평균 주식 수)가 연말 유통주식 수와 10% 넘게 다른가(주식 수가 그해 크게 바뀐 회사는 정상일 수 있음)"),
    ("A6", "DART 실적 vs WiseReport 실적", "확인", "같은 해 매출(1% 초과)·영업이익(3% 그리고 5억 초과) 차이"),
    ("A7", "앱 ROE·ROA vs FnGuide 실적", "확인", "같은 해 ROE·ROA 차이 0.5%p 초과(정의가 조금 다를 수 있음)"),
    ("B1", "저장된 계산값 = 지금 계산식", "오류", "영업이익률·ROE·ROA·부채비율·EPS·BPS·시가총액을 저장된 원본으로 다시 계산해 저장값과 비교(상대 0.01% 이내)"),
    ("B2", "계산 빠짐", "오류", "입력값이 다 있는데 계산값이 비어 있음"),
    ("B3", "새 계산 규칙 미적용", "오류", "설치 뒤 '새 버전 준비'(원본 재해석·다시 계산)가 아직 안 된 회사"),
    ("C1", "연말 주가 출처 혼재", "참고", "연말 종가가 해마다 다른 출처(키움·네이버 등)에서 옴 — 수정주가 기준이 섞일 수 있음"),
    ("C2", "기준일 주가가 오래됨·없음", "확인", "기준일 주가가 없거나 7일 넘게 지난 값"),
    ("C3", "주식 수 급변 + 수정주가", "확인", "연말 보통주 수가 전년보다 5% 넘게 늘었는데 무상증자·분할 보정 계수가 없음 — 과거 시가총액·연말 PBR이 수정주가 × 그때 주식 수로 어긋날 수 있음"),
    ("C4", "증자 내역 수집 상태", "참고", "DART 증자(감자) 현황 수집 결과(없음·못 읽음·실패)"),
    ("D1", "차입금 미식별", "참고", "최근 결산에서 본문 차입금이 실제 차입을 못 담음 → 순차입금·DCF 계산 안 함"),
    ("E1", "컨센서스 두 출처 차이", "참고", "WiseReport와 FnGuide 추정이 매출 10%·영업이익·EPS 15% 넘게 다름"),
]
NAME = {c[0]: c for c in CHECKS}


def _path():
    return os.path.join(config.DATA_DIR, "state", "audit_latest.json")


def _rel(a, b):
    return abs(a - b) / max(abs(b), 1e-9)


def run(conn, P=None, progress=None):
    """반환: 결과 dict(저장도 함). progress(i, n, 회사)는 진행 표시용"""
    from . import goodco, pipeline, standards
    P = P or config.periods(None)
    cy = P["current"]
    years = P["past"]
    comps = [(r["stock_code"], r["name"] or r["stock_code"]) for r in conn.execute(
        "SELECT c.stock_code, c.name FROM companies c WHERE EXISTS (SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code) ORDER BY c.stock_code")]
    found = {c[0]: {} for c in CHECKS}          # 번호 → {코드: [메시지]}
    checked = {c[0]: 0 for c in CHECKS}

    def hit(cid, code, msg):
        found[cid].setdefault(code, []).append(msg)

    # A1 — 전체를 SQL 한 번으로
    for r in conn.execute(
            "SELECT f.stock_code, f.fiscal_year y, f.period p, f.metric m, f.value v FROM facts f WHERE f.is_current=1 AND f.source='DART' "
            "AND f.metric LIKE 'acc_%' AND f.value IS NOT NULL AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.stock_code=f.stock_code "
            "AND o.fiscal_year=f.fiscal_year AND o.period=f.period AND o.metric=f.metric AND o.source='DART' AND o.source_ref=f.source_ref "
            "AND ABS(o.value - f.value) <= 0.5)"):
        hit("A1", r["stock_code"], f"{r['y']} {r['p']} {config.ACCOUNTS.get(r['m'], r['m'])} {r['v']:,.0f} — 같은 보고서 원본에 이 값이 없음")
    checked["A1"] = len(comps)
    recalc_done = {r[0] for r in conn.execute("SELECT DISTINCT stock_code FROM fetch_log WHERE source=? AND key LIKE ?",
                                              (pipeline.RECALC_KEY, f"%:v{__import__('analyzer.collectors.dart', fromlist=['x']).PARSER_VERSION}.{pipeline.RECALC_VER}"))}
    cap_log = {r["stock_code"]: r for r in conn.execute(
        "SELECT stock_code, ok, n_items, note FROM fetch_log WHERE id IN (SELECT MAX(id) FROM fetch_log WHERE source='dart_capital' GROUP BY stock_code)")}
    now = dt.date.today()
    for i, (code, name) in enumerate(comps):
        if progress:
            progress(i, len(comps), name)
        fm = db.fact_map(conn, code)
        v = lambda y, k: (lambda r: None if r is None else r["value"])(fm.get((y, k)))
        conflict = pipeline.sign_conflicts(fm, years)
        try:
            fac = pipeline.share_factors(conn, code, fm)
        except Exception:      # noqa: BLE001
            fac = {}
        for y in years:
            a, l, e = v(y, "acc_assets"), v(y, "acc_liab"), v(y, "acc_eq")
            if None not in (a, l, e) and a:
                checked["A2"] += 1
                if abs(a - (l + e)) > abs(a) * 0.005:
                    hit("A2", code, f"{y} 자산 {a / 1e8:,.0f}억 vs 부채 + 자본 {(l + e) / 1e8:,.0f}억")
            ni, nio, nci = v(y, "acc_ni"), v(y, "acc_ni_owner"), v(y, "acc_ni_nci")
            if None not in (ni, nio, nci):
                checked["A3"] += 1
                if abs(nio + nci - ni) > max(abs(ni) * 0.005, 1e8):
                    hit("A3", code, f"{y} 지배 {nio / 1e8:,.0f}억 + 비지배 {nci / 1e8:,.0f}억 ≠ 당기순이익 {ni / 1e8:,.0f}억")
            eps = v(y, "acc_eps_basic")
            if nio is not None and eps is not None:
                checked["A4"] += 1
                if y in conflict:
                    hit("A4", code, f"{y} 지배순이익 {nio / 1e8:,.0f}억 · EPS {eps:,.0f}원")
                sh = pipeline.shares_for_bps(fm, y)
                if sh and eps and y not in conflict and abs(eps) >= 100:
                    checked["A5"] += 1
                    f = (fac.get(y) or (1.0,))[0]
                    imp = nio / eps
                    base = sh / f if f < 0.999 and not pipeline.eps_needs_adjust(nio, eps, sh, f) else sh
                    if _rel(imp, base) > 0.10:
                        hit("A5", code, f"{y} 순이익 ÷ EPS {imp:,.0f}주 vs 유통주식 {base:,.0f}주({_rel(imp, base):.0%} 차이)")
            # B1·B2 다시 계산
            rev, op = v(y, "acc_revenue"), v(y, "acc_op")
            eqo, peqo = v(y, "acc_eq_owner"), v(y - 1, "acc_eq_owner")
            asset, passet = a, v(y - 1, "acc_assets")
            f, _w = fac.get(y, (1.0, None))
            exp = {}
            exp["op_margin"] = op / rev * 100 if rev and op is not None else None
            exp["debt_ratio"] = l / e * 100 if e and l is not None else None
            if y in conflict:
                exp["roe"] = exp["eps"] = None
            else:
                exp["roe"] = (nio / ((peqo + eqo) / 2) * 100 if peqo is not None and eqo is not None and nio is not None
                              else nio / eqo * 100 if eqo and nio is not None else None)
                if eps is not None and f < 0.999 and pipeline.eps_needs_adjust(nio, eps, pipeline.shares_for_bps(fm, y), f):
                    exp["eps"] = eps * f
                else:
                    exp["eps"] = eps
            exp["roa"] = (ni / ((asset + passet) / 2) * 100 if ni is not None and asset and passet else ni / asset * 100 if ni is not None and asset else None)
            sh = pipeline.shares_for_bps(fm, y)
            exp["bps"] = eqo / sh * f if sh and eqo is not None else "skip"
            px, c = v(y, "price_close"), v(y, "shares_common")
            exp["market_cap"] = px * c / f / 1e8 if px is not None and c else "skip"
            for m, ev in exp.items():
                if ev == "skip":
                    continue
                row = fm.get((y, m))
                sv = None if row is None else row["value"]
                checked["B1"] += 1
                if ev is None and sv is not None:
                    hit("B1", code, f"{y} {config.METRIC_NAME.get(m, m)} 저장값 {sv:,.2f} — 지금 계산식으로는 비어야 함(입력 없음·자료 충돌)")
                elif ev is not None and sv is None:
                    hit("B2", code, f"{y} {config.METRIC_NAME.get(m, m)} — 입력값이 있는데 계산값이 비어 있음(계산하면 {ev:,.2f})")
                elif ev is not None and sv is not None and _rel(sv, ev) > 1e-4 and abs(sv - ev) > 0.01:
                    hit("B1", code, f"{y} {config.METRIC_NAME.get(m, m)} 저장값 {sv:,.2f} vs 다시 계산 {ev:,.2f}")
        checked["B2"] = checked["B1"]
        # A6 WiseReport 실적
        for r in conn.execute("SELECT fiscal_year y, metric m, value FROM observations WHERE stock_code=? AND source='WiseReport' AND period='FY' "
                              "AND metric IN ('acc_revenue','acc_op')", (code,)):
            dv = v(r["y"], r["m"])
            if dv is None or r["value"] is None or r["y"] >= cy:
                continue
            checked["A6"] += 1
            d = abs(dv - r["value"])
            bad = d > abs(dv) * 0.01 if r["m"] == "acc_revenue" else (d > abs(dv) * 0.03 and d > 5e8)
            if bad:
                hit("A6", code, f"{r['y']} {'매출' if r['m'] == 'acc_revenue' else '영업이익'} DART {dv / 1e8:,.0f}억 vs WiseReport {r['value'] / 1e8:,.0f}억")
        # A7 FnGuide ROE·ROA 실적
        for r in conn.execute("SELECT fiscal_year y, metric m, value FROM observations WHERE stock_code=? AND source='FnGuide' AND period='FY' "
                              "AND metric IN ('roa_fnguide','roe_fg_fnguide')", (code,)):
            m = "roa" if r["m"] == "roa_fnguide" else "roe"
            av = v(r["y"], m)
            if av is None or r["value"] is None:
                continue
            checked["A7"] += 1
            if abs(av - r["value"]) > 0.5:
                hit("A7", code, f"{r['y']} {m.upper()} 앱 {av:.1f}% vs FnGuide {r['value']:.1f}%")
        # B3
        checked["B3"] += 1
        if code not in recalc_done:
            hit("B3", code, "새 버전 준비(원본 재해석·다시 계산) 기록 없음")
        # C1·C2·C3
        srcs = {fm[(y, "price_close")]["source"] for y in years if fm.get((y, "price_close"))}
        checked["C1"] += 1
        if len(srcs) > 1:
            by = {}
            for y in years:
                r = fm.get((y, "price_close"))
                if r:
                    by.setdefault(r["source"], []).append(y)
            hit("C1", code, " · ".join(f"{s} {min(ys)}~{max(ys)}" for s, ys in by.items()))
        asof = db.current_fact(conn, code, cy, "price_close", "ASOF")
        if asof is None or asof["value"] is None:
            checked["C2"] += 1
            hit("C2", code, "기준일 주가 없음")
        elif asof["as_of"]:
            checked["C2"] += 1
            try:
                age = (now - dt.date.fromisoformat(str(asof["as_of"])[:10])).days
                if age > 7:
                    hit("C2", code, f"기준일 주가 {asof['value']:,.0f}원은 {asof['as_of']} 값({age}일 전)")
            except ValueError:
                pass
        for y in years:
            c0, c1 = v(y - 1, "shares_common"), v(y, "shares_common")
            if c0 and c1:
                checked["C3"] += 1
                if c1 > c0 * 1.05 and not fac.get(y - 1):
                    hit("C3", code, f"{y - 1}→{y} 보통주 {c0:,.0f} → {c1:,.0f}주(+{(c1 / c0 - 1):.0%}) · 보정 계수 없음")
        # C4
        lg = cap_log.get(code)
        checked["C4"] += 1
        if lg is None:
            hit("C4", code, "증자 내역 수집 기록 없음")
        elif not lg["ok"]:
            hit("C4", code, f"실패: {str(lg['note'] or '')[:160]}")
        elif not lg["n_items"]:
            hit("C4", code, f"0건: {str(lg['note'] or '')[:120]}")
        # D1
        if years:
            ly = max((y for y in years if v(y, "acc_assets") is not None), default=None)
            if ly:
                checked["D1"] += 1
                ip = (v(ly, "acc_int_paid") or 0) + (v(ly, "acc_int_paid_fin") or 0)
                hid, why = standards.hidden_debt(v(ly, "acc_debt"), v(ly - 1, "acc_debt"), ip, v(ly, "acc_assets"))
                if hid:
                    hit("D1", code, f"{ly} {why}")
        # E1 컨센서스 두 출처
        cons = {(r["target_year"], r["metric"]): r["value"] for r in db.latest_consensus(conn, code)}
        for y in (cy, cy + 1, cy + 2):
            for m, mf, lim in (("revenue", "revenue_fg", 0.10), ("op_income", "op_income_fg", 0.15), ("eps", "eps_fg", 0.15)):
                a_, b_ = cons.get((y, m)), cons.get((y, mf))
                if a_ is None or b_ is None:
                    continue
                checked["E1"] += 1
                if b_ and abs(a_ - b_) / abs(b_) > lim:
                    hit("E1", code, f"{y}E {m} WiseReport {a_:,.0f} vs FnGuide {b_:,.0f}")
    names = dict(comps)
    out = {"at": db.now(), "n_companies": len(comps), "version": __import__("analyzer").__version__, "checks": []}
    for cid, nm, lvl, desc in CHECKS:
        cs = found[cid]
        out["checks"].append({"id": cid, "name": nm, "level": lvl, "desc": desc, "n_checked": checked[cid], "n_companies": len(cs),
                              "n_items": sum(len(x) for x in cs.values()),
                              "companies": [{"code": c, "name": names.get(c, c), "items": items} for c, items in sorted(cs.items())]})
    out["text"] = text(out)
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except OSError:
        pass
    return out


def latest():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def text(out, per=8):
    """사용자가 복사해 보낼 요약(회사·항목 예시는 점검마다 per곳까지)"""
    L = [f"# 전수 점검 결과 — {out['at']} · 앱 {out['version']} · {out['n_companies']}곳", "",
         "| 번호 | 점검 | 등급 | 대조 건수 | 걸린 회사 | 걸린 항목 |", "|---|---|---|---|---|---|"]
    for c in out["checks"]:
        L.append(f"| {c['id']} | {c['name']} | {c['level']} | {c['n_checked']} | {c['n_companies']} | {c['n_items']} |")
    for c in out["checks"]:
        if not c["companies"]:
            continue
        L += ["", f"## {c['id']} {c['name']} ({c['level']}) — {c['desc']}"]
        for x in c["companies"][:per]:
            L.append(f"- {x['name']}({x['code']}): " + " / ".join(x["items"][:3]) + (f" 외 {len(x['items']) - 3}건" if len(x["items"]) > 3 else ""))
        if len(c["companies"]) > per:
            L.append(f"- … 외 {len(c['companies']) - per}곳")
    return "\n".join(L)
