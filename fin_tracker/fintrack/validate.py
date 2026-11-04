"""검증 모듈. 네트워크 없이 DB 안의 값끼리만 대조한다.

상태 규칙
- 실적 원값: 출처 접수번호 + 항등식 + EPS·순이익·주식수 관계가 맞으면 VERIFIED.
  다른 출처 참고값이 있고 어긋나면 CONFLICT.
- 이미 VERIFIED인 값은 다시 승인하지 않는다. 단 검사에서 실패하면 CONFLICT로 내린다(오류 발견).
- 주가: 출처가 하나뿐이면 UNVERIFIED로 둔다. 시가총액은 다른 출처가 인쇄한 값과 맞으면 VERIFIED.
- 미검증 컨센서스(원문 미확인)는 어떤 경우에도 VERIFIED로 올리지 않는다.
"""
import json
from . import config


class Validator:
    def __init__(self, store, code, periods, run_id, reverify=False):
        self.s, self.code, self.p, self.run_id, self.reverify = store, code, periods, run_id, reverify
        self.face = store.company(code)["face_value"]
        self.summary = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def log(self, y, item, name, result, detail):
        self.s.log_check(self.run_id, self.code, y, item, name, result, detail)
        self.summary[result] += 1

    def _status(self, row, ok, note, fail_status="CONFLICT"):
        if row is None:
            return
        if ok:
            if row["status"] != "VERIFIED" and (row["status"] in ("UNVERIFIED", "CORRECTED") or self.reverify):
                self.s.set_status(row["id"], "VERIFIED", note)
        else:
            if row["status"] != fail_status:
                self.s.set_status(row["id"], fail_status, note)

    def fy(self, y):
        return {r["item"]: r for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}

    def run(self):
        self.actuals()
        self.yoy()
        self.corrections()
        self.partial()
        self.market()
        self.consensus()
        self.no_mixing()
        self.s.commit()
        return self.summary

    # ---------- 실적 ----------
    def actuals(self):
        ref = {(r["fiscal_year"], r["item"]): r for r in self.s.current(self.code, kind="REFERENCE")}
        prev_rows = self.fy(self.p["actual"][0] - 1)
        for y in self.p["actual"]:
            rows = self.fy(y)
            v = {k: r["value"] for k, r in rows.items()}
            missing = [a for a in config.BASE_ACCOUNTS if a not in v]
            if missing:
                self.log(y, ",".join(missing), "완전성", "FAIL", "계정 미확보 → 관련 지표 MISSING")
            checks = []
            traceable = all(r["source_ref"] for r in rows.values())
            checks.append(("출처 추적", traceable, "모든 값에 DART 접수번호 있음" if traceable else "접수번호 없는 값 있음"))
            if "eq_owner" in v and "eq" in v:
                ok = 0 < v["eq_owner"] <= v["eq"] * 1.0001
                checks.append(("자본 항등식", ok, f"지배자본 {v['eq_owner']/1e8:,.0f}억 ≤ 자본총계 {v['eq']/1e8:,.0f}억"))
            if {"ni_owner", "eps", "issued_capital"} <= set(v) and abs(v["eps"]) >= 1:
                implied = v["ni_owner"] / v["eps"]
                end = v["issued_capital"] / self.face
                start = prev_rows.get("issued_capital")["value"] / self.face if "issued_capital" in prev_rows else end
                lo, hi = min(start, end) * 0.97, max(start, end) * 1.03
                ok = lo <= implied <= hi
                checks.append(("EPS·순이익·주식수", ok,
                               f"순이익÷EPS={implied:,.0f}주, 기초~기말 주식수 {start:,.0f}~{end:,.0f}주(±3%)"))
            all_ok = all(c[1] for c in checks) and not missing
            for name, ok, detail in checks:
                self.log(y, "실적", name, "PASS" if ok else "FAIL", detail)
            # 다른 출처와 대조 (있을 때만)
            conflict = None
            if (y, "eps") in ref and "eps" in v:
                ok = ref[(y, "eps")]["value"] == v["eps"]
                self.log(y, "eps", "교차검증(WiseReport)", "PASS" if ok else "FAIL",
                         f"DART {v['eps']:,.0f}원 / WiseReport {ref[(y, 'eps')]['value']:,.0f}원")
                conflict = conflict or (None if ok else "eps")
            if (y, "bps") in ref and {"eq_owner", "issued_capital"} <= set(v):
                bps = v["eq_owner"] / (v["issued_capital"] / self.face)
                r = ref[(y, "bps")]["value"]
                ok = abs(bps / r - 1) <= config.TOL_BPS
                self.log(y, "bps", "교차검증(WiseReport)", "PASS" if ok else "FAIL",
                         f"계산 {bps:,.0f}원 / WiseReport {r:,.0f}원 (차이 {100*(bps/r-1):+.2f}%, 허용 ±{config.TOL_BPS*100}%: 자사주 차감 여부 차이)")
                conflict = conflict or (None if ok else "bps")
            note = "DART 원값·항등식 검사 통과" + (" · WiseReport와 일치" if (y, "eps") in ref else "")
            for item, row in rows.items():
                if conflict and item in ("eps", "eq_owner", "issued_capital"):
                    self._status(row, False, f"{conflict} 출처 간 불일치")
                else:
                    self._status(row, all_ok, note if all_ok else "검사 실패", fail_status="UNVERIFIED")
            prev_rows = rows
        # 보조연도(ROE 평균자본용)
        for y in self.p["helper"]:
            for row in self.fy(y).values():
                self._status(row, bool(row["source_ref"]), "보조연도: 출처 추적 가능", "UNVERIFIED")

    def yoy(self):
        names = {"revenue": "매출액", "op": "영업이익", "ni_owner": "지배순이익", "eq_owner": "지배자본", "liab": "부채총계"}
        prev = self.fy(self.p["actual"][0] - 1)
        for y in self.p["actual"]:
            cur = self.fy(y)
            for k, nm in names.items():
                if k in cur and k in prev and prev[k]["value"]:
                    a, b = cur[k]["value"], prev[k]["value"]
                    r = (a - b) / abs(b)
                    if abs(r) > config.YOY_WARN or (a < 0) != (b < 0):
                        self.log(y, k, "전년 대비 급변", "WARN",
                                 f"{nm} {b/1e8:,.0f}억 → {a/1e8:,.0f}억 ({r*100:+.0f}%). 원값은 공시와 같음, 사유 확인용 표시")
            prev = cur

    def corrections(self):
        from .sources import dart
        a = self.p["actual"]
        corp = self.s.company(self.code)["corp_code"]
        found, msg = dart.corrections(corp, f"{a[0] + 1}0101", f"{self.p['base']}1231")
        if found is None:
            self.log(None, "*", "정정 공시 확인", "WARN", f"미실행({msg}). 키를 넣고 다시 실행하면 사업보고서 정정 공시를 찾아 해당 연도를 다시 받음")
        elif not found:
            self.log(None, "*", "정정 공시 확인", "PASS", f"{a[0]}~{a[-1]} 사업보고서 정정 공시 없음")
        else:
            for x in found:
                self.log(None, "*", "정정 공시 확인", "WARN", f"{x['report_nm']} {x['rcept_dt']} ({x['rcept_no']}) → 해당 연도 재수집 필요")

    def partial(self):
        b = self.p["base"]
        for per in {r["period"] for r in self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL")}:
            rows = {r["item"]: r for r in self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL", period=per)}
            v = {k: r["value"] for k, r in rows.items()}
            ok = all(r["source_ref"] for r in rows.values()) and ("eq_owner" not in v or "eq" not in v or 0 < v["eq_owner"] <= v["eq"])
            self.log(b, per, "올해 부분 실적", "PASS" if ok else "FAIL", f"{per} DART 접수번호 {next(iter(rows.values()))['source_ref']} · 자본 항등식")
            for r in rows.values():
                self._status(r, ok, f"{per} 보고서 원값", "UNVERIFIED")

    # ---------- 주가·시가총액 ----------
    def market(self):
        b = self.p["base"]
        for r in self.s.current(self.code, item="price_close", kind="MARKET"):
            if r["fiscal_year"] < b:
                if r["status"] != "UNVERIFIED" and not self.reverify:
                    continue
                self.s.set_status(r["id"], "UNVERIFIED", "단일 출처(KIS 수정주가). 한국거래소 자료와 대조 전")
        rows = {r["item"]: r for r in self.s.current(self.code, fiscal_year=b, kind="MARKET")}
        if {"price_close", "shares", "market_cap"} <= set(rows):
            calc = rows["price_close"]["value"] * rows["shares"]["value"]
            printed = rows["market_cap"]["value"]
            ok = abs(calc / printed - 1) <= config.TOL_MCAP + 0.0004  # 인쇄값이 억원 단위 반올림
            self.log(b, "market_cap", "시가총액 재계산", "PASS" if ok else "FAIL",
                     f"{rows['price_close']['value']:,.0f}원 × {rows['shares']['value']:,.0f}주 = {calc/1e8:,.1f}억 / 화면 {printed/1e8:,.0f}억")
            for r in rows.values():
                self._status(r, ok, "주가×주식수 = 화면 시가총액")
        years = [y for y in self.p["actual"]]
        self.log(None, "market_cap", "과거 시가총액 산식", "WARN",
                 f"{years[0]}~{years[-1]} 시가총액 = 연말 수정주가 × (자본금÷액면가). 수정주가와 당시 주식수를 곱하므로 "
                 "유상증자·합병이 있던 해는 실제 당시 시가총액과 다를 수 있음(한국거래소 자료로 대조 필요)")

    # ---------- 컨센서스 ----------
    def consensus(self):
        b = self.p["base"]
        cons = self.s.current(self.code, kind="CONSENSUS")
        by = {}
        for r in cons:
            by.setdefault((r["fiscal_year"], r["item"]), []).append(r)
        partial = {r["item"]: r["value"] for r in self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL")}
        mkt = {r["item"]: r["value"] for r in self.s.current(self.code, fiscal_year=b, kind="MARKET")}
        for (y, item), rows in sorted(by.items()):
            wise = [r for r in rows if r["source"].startswith("WiseReport")]
            other = [r for r in rows if not r["source"].startswith("WiseReport")]
            for r in other:  # 원문 미확인 자료는 승인하지 않음
                self.log(y, item, "출처 신뢰도", "WARN", f"{r['source']}: 원문 미확인 → UNVERIFIED 유지")
            for w in wise:
                ex = json.loads(w["extra"] or "{}")
                ok, notes = True, [f"추정 기관 {ex.get('n_estimates', '?'):.0f}곳" if ex.get("n_estimates") else "추정 기관 수 미상"]
                if item == "eps" and ex.get("per_e") and mkt.get("price_close"):
                    per = mkt["price_close"] / w["value"]
                    good = abs(per - ex["per_e"]) < 0.06
                    self.log(y, item, "내부 일관성(주가÷EPS=PER)", "PASS" if good else "FAIL",
                             f"{mkt['price_close']:,.0f}÷{w['value']:,.0f}={per:.2f} / 화면 PER {ex['per_e']}")
                    ok &= good
                if item == "bps" and {"eq_owner"} <= set(partial) and mkt.get("shares"):
                    h1 = partial["eq_owner"] / mkt["shares"]
                    gap = w["value"] / h1 - 1
                    good = abs(gap) <= 0.10
                    self.log(y, item, "현실성(반기 실제 BPS 대비)", "PASS" if good else "FAIL",
                             f"컨센서스 {w['value']:,.0f}원 vs 2026-06-30 실제 {h1:,.0f}원 ({gap*100:+.1f}%). "
                             + ("" if good else f"하반기에 주당 약 {h1 - w['value']:,.0f}원 자본 감소가 있어야 맞는 값 → 원문 확인 필요"))
                    if not good:
                        notes.append(f"반기 실제 BPS 대비 {gap*100:+.1f}%")
                    ok &= good
                if ex.get("n_estimates") and ex["n_estimates"] < 3:
                    self.log(y, item, "표본 수", "WARN", f"추정 기관 {ex['n_estimates']:.0f}곳: 평균의 대표성 낮음")
                for o in other:
                    gap = o["value"] / w["value"] - 1 if w["value"] else 0
                    if abs(gap) > 0.2:
                        self.log(y, item, "출처 간 비교", "WARN",
                                 f"WiseReport {w['value']:,.0f} vs {o['source'][:20]} {o['value']:,.0f} ({gap*100:+.0f}%)")
                        if o["status"] != "CONFLICT":
                            self.s.set_status(o["id"], "CONFLICT", f"WiseReport 대비 {gap*100:+.0f}%, 원문 미확인")
                self._status(w, ok, " · ".join(notes))
        for y in [b] + self.p["future"]:
            for key, name, _ in config.METRICS:
                if key == "market_cap":
                    continue
                need = {"op_margin": ["op", "revenue"]}.get(key, [key])
                for n in need:
                    if not any(r["status"] == "VERIFIED" for r in by.get((y, n), [])):
                        self.log(y, n, "검증된 컨센서스", "WARN", f"{y} {n}: 검증된 출처 없음(MISSING)")

    def no_mixing(self):
        b = self.p["base"]
        bad = self.s.con.execute(
            "SELECT COUNT(*) c FROM observations WHERE stock_code=? AND is_current=1 AND "
            "kind='ACTUAL' AND fiscal_year>=?", (self.code, b)).fetchone()["c"]
        # 화면 계열은 metrics.build가 kind별로 따로 만든다(실적선에는 ACTUAL만, 전망선에는 CONSENSUS만).
        self.log(None, "*", "실적·전망 혼합 금지", "PASS" if bad == 0 else "FAIL",
                 "올해 이후 연도에 확정실적 없음 · 실적선과 전망선은 별도 계열" if bad == 0 else f"{bad}건 위반")
