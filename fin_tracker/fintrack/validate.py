"""검증 모듈. 네트워크 없이 DB 안의 값끼리만 대조한다.

상태 규칙
- 실적 원값: 접수번호 + 자본 항등식 + EPS·순이익·주식수 관계가 맞으면 VERIFIED.
  다른 출처(FnGuide·WiseReport 실적 열)와 어긋나면 CONFLICT.
- 이미 VERIFIED인 값은 다시 승인하지 않는다. 단 검사에서 실패하면 내린다(오류 발견).
- FnGuide 전망: 같은 화면의 실적 열이 DART와 맞아야(=제대로 읽었다는 증거) VERIFIED.
- 주가: 한국거래소 값은 '종가×상장주식수=시가총액'이 맞으면 VERIFIED, 그 밖의 출처는 UNVERIFIED.
- 원문 미확인 자료는 어떤 경우에도 VERIFIED로 올리지 않는다.
"""
import json
from . import config


def shares_info(store, code, y):
    """연말 주식 수. 반환: (BPS용 주식수, EPS 대조용 발행주식수, 설명) 또는 (None, None, 사유)"""
    rows = {r["item"]: r["value"] for r in store.current(code, fiscal_year=y, kind="ACTUAL")
            if r["item"] in ("shares_common", "shares_treasury", "shares_pref", "issued_capital")}
    if rows.get("shares_common"):
        pref = rows.get("shares_pref") or 0
        tre = rows.get("shares_treasury") or 0
        return rows["shares_common"] + pref - tre, rows["shares_common"] + pref, \
            f"DART 주식총수: 보통주 {rows['shares_common']:,.0f}" + (f" + 우선주 {pref:,.0f}" if pref else "") + (f" − 자기주식 {tre:,.0f}" if tre else "")
    c = store.company(code)
    face = c["face_value"] if c else None
    if face and rows.get("issued_capital"):
        n = rows["issued_capital"] / face
        return n, n, f"자본금÷액면가 {face:,}원 = {n:,.0f}주(자사주 미차감)"
    return None, None, "주식 수 미확보"


class Validator:
    def __init__(self, store, code, periods, run_id, reverify=False):
        self.s, self.code, self.p, self.run_id, self.reverify = store, code, periods, run_id, reverify
        self.summary = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def log(self, y, item, name, result, detail):
        self.s.log_check(self.run_id, self.code, y, item, name, result, detail)
        self.summary[result] += 1

    def _status(self, row, ok, note, fail_status="CONFLICT"):
        if row is None:
            return
        if ok:
            if row["status"] in ("UNVERIFIED", "CORRECTED", "STALE") or (self.reverify and row["status"] != "VERIFIED"):
                self.s.set_status(row["id"], "VERIFIED", note)
        elif row["status"] != fail_status:
            self.s.set_status(row["id"], fail_status, note)

    def fy(self, y):
        return {r["item"]: r for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}

    def run(self):
        for f in (self.actuals, self.yoy, self.corrections_ran, self.partial, self.market, self.consensus, self.no_mixing):
            f()
        self.s.commit()
        return self.summary

    # ---------- 실적 ----------
    def actuals(self):
        refs = {}
        for r in self.s.current(self.code, kind="REFERENCE"):
            refs.setdefault((r["fiscal_year"], r["item"]), []).append(r)
        for y in self.p["actual"]:
            rows = self.fy(y)
            v = {k: r["value"] for k, r in rows.items()}
            missing = [a for a in config.REQUIRED_ACCOUNTS if a not in v]
            if not rows:
                self.log(y, "*", "완전성", "FAIL", "이 연도 사업보고서 값이 없음 → 관련 지표 MISSING")
                continue
            if missing:
                self.log(y, ",".join(missing), "완전성", "FAIL", "계정 미확보 → 관련 지표 MISSING")
            checks = []
            fin = [r for k, r in rows.items() if not k.startswith("shares_")]
            traceable = all(r["source_ref"] for r in fin)
            checks.append(("출처 추적", traceable, "모든 값에 DART 접수번호 있음" if traceable else "접수번호 없는 값 있음"))
            if "eq_owner" in v and "eq" in v:
                ok = 0 < v["eq_owner"] <= v["eq"] * 1.0001
                checks.append(("자본 항등식", ok, f"지배자본 {v['eq_owner']/1e8:,.0f}억 ≤ 자본총계 {v['eq']/1e8:,.0f}억"))
            _, end, how = shares_info(self.s, self.code, y)
            _, start, _ = shares_info(self.s, self.code, y - 1)
            if {"ni_owner", "eps"} <= set(v) and abs(v["eps"]) >= 1 and end:
                start = start or end
                implied = v["ni_owner"] / v["eps"]
                lo, hi = min(start, end) * 0.95, max(start, end) * 1.03
                ok = lo <= implied <= hi
                checks.append(("EPS·순이익·주식수", ok, f"순이익÷EPS={implied:,.0f}주, 기초~기말 {start:,.0f}~{end:,.0f}주 ({how})"))
            elif end is None:
                self.log(y, "eps", "EPS·순이익·주식수", "WARN", "주식 수가 없어 대조 못 함")
            all_ok = all(c[1] for c in checks) and not missing
            for name, ok, detail in checks:
                self.log(y, "실적", name, "PASS" if ok else "FAIL", detail)
            conflict = self._cross(y, v, refs)
            note = "DART 원값·항등식 검사 통과"
            for item, row in rows.items():
                if item.startswith("shares_"):
                    self._status(row, bool(row["source_ref"]), "DART 주식총수 현황", "UNVERIFIED")
                elif conflict and item in conflict:
                    self._status(row, False, f"다른 출처와 불일치: {conflict[item]}")
                else:
                    self._status(row, all_ok, note if all_ok else "검사 실패", fail_status="UNVERIFIED")
        for y in self.p["helper"]:
            for row in self.fy(y).values():
                self._status(row, bool(row["source_ref"]), "보조연도: 출처 추적 가능", "UNVERIFIED")

    def _cross(self, y, v, refs):
        """다른 출처 실적 열과 대조. 반환: {항목: 설명} (불일치만)"""
        bad = {}
        bps_n, _, _ = shares_info(self.s, self.code, y)
        mine = dict(v)
        if bps_n and "eq_owner" in v:
            mine["bps"] = v["eq_owner"] / bps_n
        for item, tol in (("revenue", config.TOL_MATCH), ("op", config.TOL_MATCH), ("eq_owner", config.TOL_MATCH),
                          ("eps", 0.0), ("bps", config.TOL_BPS)):
            for r in refs.get((y, item), []):
                if item not in mine or r["status_note"] == "잠정실적(P)":
                    continue
                a, b = mine[item], r["value"]
                if item == "eps":
                    ok = abs(a - b) <= 1
                elif item in ("revenue", "op", "eq_owner"):
                    ok = abs(a - b) <= max(abs(b) * tol, 0.6e8)      # FnGuide는 억원 반올림
                else:
                    ok = b != 0 and abs(a / b - 1) <= tol
                unit = "원" if item in ("eps", "bps") else "억"
                f = (lambda x: f"{x:,.0f}") if unit == "원" else (lambda x: f"{x/1e8:,.0f}")
                src = r["source"].split()[0]
                self.log(y, item, f"교차검증({src})", "PASS" if ok else "FAIL", f"DART {f(a)}{unit} / {src} {f(b)}{unit}")
                if not ok:
                    bad[{"bps": "eq_owner"}.get(item, item)] = f"{src} {f(b)}{unit}"
                r_ok = ok
                self._status(r, r_ok, "DART와 일치", "CONFLICT")
        return bad

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
                        self.log(y, k, "전년 대비 급변", "WARN", f"{nm} {b/1e8:,.0f}억 → {a/1e8:,.0f}억 ({r*100:+.0f}%). 공시값 그대로, 사유 확인용")
            prev = cur

    def corrections_ran(self):
        r = self.s.con.execute("SELECT MAX(fetched_at) t FROM fetch_log WHERE stock_code=? AND source='DART 정정 공시' AND result='ok'",
                               (self.code,)).fetchone()["t"]
        if r:
            self.log(None, "*", "정정 공시 확인", "PASS", f"마지막 확인 {r[:10]} (갱신할 때마다 확인, 정정분은 새 버전으로 반영)")
        else:
            self.log(None, "*", "정정 공시 확인", "WARN", "아직 확인 안 함. '갱신'을 누르면 사업보고서 정정 공시를 찾아 반영")

    def partial(self):
        b = self.p["base"]
        rows_all = self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL")
        for per in {r["period"] for r in rows_all}:
            rows = {r["item"]: r for r in rows_all if r["period"] == per}
            v = {k: r["value"] for k, r in rows.items()}
            ok = all(r["source_ref"] for r in rows.values()) and ("eq_owner" not in v or "eq" not in v or 0 < v["eq_owner"] <= v["eq"])
            self.log(b, per, "올해 부분 실적", "PASS" if ok else "FAIL", f"{per} 보고서 접수번호 {next(iter(rows.values()))['source_ref']} · 자본 항등식")
            for r in rows.values():
                self._status(r, ok, f"{per} 보고서 원값", "UNVERIFIED")

    # ---------- 주가·시가총액 ----------
    def market(self):
        b = self.p["base"]
        by = {}
        for r in self.s.current(self.code, kind="MARKET"):
            by.setdefault((r["fiscal_year"], r["source"]), {})[r["item"]] = r
        for (y, src), rows in sorted(by.items()):
            sh = rows.get("listed_shares") or rows.get("shares")
            if {"price_close", "market_cap"} <= set(rows) and sh:
                calc = rows["price_close"]["value"] * sh["value"]
                printed = rows["market_cap"]["value"]
                ok = abs(calc / printed - 1) <= config.TOL_MCAP
                self.log(y, "market_cap", f"시가총액 재계산({src.split()[0]})", "PASS" if ok else "FAIL",
                         f"{rows['price_close']['value']:,.0f}원 × {sh['value']:,.0f}주 = {calc/1e8:,.0f}억 / 표시 {printed/1e8:,.0f}억")
                for r in rows.values():
                    self._status(r, ok, "종가×주식수 = 시가총액")
            elif "price_close" in rows and y < b:
                r = rows["price_close"]
                if r["status"] == "UNVERIFIED" and not r["status_note"]:
                    self.s.set_status(r["id"], "UNVERIFIED", "단일 출처 종가. 시가총액은 종가×DART 주식수로 계산")
        if any(src != "한국거래소(pykrx)" for (y, src) in by if y < b):
            self.log(None, "market_cap", "과거 시가총액 출처", "WARN",
                     "일부 연도는 한국거래소 시가총액이 아니라 종가×주식수 계산값. 수정주가가 섞이면 실제 당시 값과 다를 수 있음")

    # ---------- 컨센서스 ----------
    def consensus(self):
        b = self.p["base"]
        by = {}
        for r in self.s.current(self.code, kind="CONSENSUS"):
            if r["fiscal_year"] >= b:
                by.setdefault((r["fiscal_year"], r["item"]), []).append(r)
        # FnGuide를 제대로 읽었는지: 같은 화면 실적 열이 DART와 맞았는가
        refs = [r for r in self.s.current(self.code, kind="REFERENCE") if r["source"].startswith("FnGuide")]
        read_ok = any(r["status"] == "VERIFIED" for r in refs)
        read_bad = any(r["status"] == "CONFLICT" for r in refs)
        if refs:
            self.log(None, "*", "FnGuide 읽기 확인", "PASS" if read_ok and not read_bad else ("WARN" if read_ok else "FAIL"),
                     "같은 화면의 실적 열이 DART와 일치" if read_ok and not read_bad else "실적 열 일부가 DART와 다름(화면 구조·단위 확인 필요)")
        partial = {r["item"]: r["value"] for r in self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL")}
        mkt = {}
        for r in self.s.current(self.code, fiscal_year=b, kind="MARKET"):
            mkt.setdefault(r["item"], r["value"])
        for (y, item), rows in sorted(by.items()):
            trusted = [r for r in rows if r["source"].startswith(config.TRUSTED)]
            other = [r for r in rows if not r["source"].startswith(config.TRUSTED)]
            for r in other:
                self.log(y, item, "출처 신뢰도", "WARN", f"{r['source']}: 원문 미확인 → UNVERIFIED 유지")
            for w in trusted:
                ex = json.loads(w["extra"] or "{}")
                ok, notes = True, []
                n = ex.get("n_estimates")
                notes.append(f"추정 기관 {n:.0f}곳" if n else "추정 기관 수 미상")
                if w["source"].startswith("FnGuide"):
                    ok &= read_ok and not read_bad
                    if not read_ok:
                        notes.append("화면 읽기 미확인")
                if item == "bps" and "eq_owner" in partial and mkt.get("shares"):
                    h = partial["eq_owner"] / mkt["shares"]
                    gap = w["value"] / h - 1
                    good = abs(gap) <= 0.10
                    if y == b:
                        self.log(y, item, f"현실성({w['source'].split()[0]}, 최근 실제 BPS 대비)", "PASS" if good else "FAIL",
                                 f"전망 {w['value']:,.0f}원 vs 최근 분기말 실제 {h:,.0f}원 ({gap*100:+.1f}%)"
                                 + ("" if good else f". 남은 기간에 주당 약 {h - w['value']:,.0f}원 자본 감소가 있어야 맞는 값"))
                        if not good:
                            notes.append(f"최근 실제 BPS 대비 {gap*100:+.1f}%")
                            ok = False
                if n and n < 3 and item == "eps":
                    self.log(y, item, f"표본 수({w['source'].split()[0]})", "WARN", f"추정 기관 {n:.0f}곳: 평균의 대표성 낮음")
                self._status(w, ok, " · ".join(notes))
            # 두 신뢰 출처끼리 비교, 미검증 자료와 비교
            if len(trusted) >= 2:
                a, c = trusted[0], trusted[1]
                gap = a["value"] / c["value"] - 1 if c["value"] else 0
                self.log(y, item, "신뢰 출처끼리 비교", "PASS" if abs(gap) <= 0.02 else "WARN",
                         f"{a['source'].split()[0]} {a['value']:,.0f} / {c['source'].split()[0]} {c['value']:,.0f} ({gap*100:+.1f}%)")
            for o in other:
                if trusted and trusted[0]["value"]:
                    gap = o["value"] / trusted[0]["value"] - 1
                    if abs(gap) > 0.2:
                        self.log(y, item, "출처 간 비교", "WARN", f"{trusted[0]['source'].split()[0]} {trusted[0]['value']:,.0f} vs {o['source'][:20]} {o['value']:,.0f} ({gap*100:+.0f}%)")
                        if o["status"] != "CONFLICT":
                            self.s.set_status(o["id"], "CONFLICT", f"신뢰 출처 대비 {gap*100:+.0f}%, 원문 미확인")
        for y in [b] + self.p["future"]:
            gaps = [n for k, n, _ in config.METRICS if k != "market_cap"
                    and not any(r["status"] in ("VERIFIED", "CONFLICT") for r in by.get((y, {"op_margin": "op"}.get(k, k)), [])
                                if r["source"].startswith(config.TRUSTED))]
            if gaps:
                self.log(y, "*", "검증된 전망", "WARN", f"{y}: {', '.join(gaps)} 전망 없음(빈칸 유지)")

    def no_mixing(self):
        bad = self.s.con.execute("SELECT COUNT(*) c FROM observations WHERE stock_code=? AND is_current=1 AND kind='ACTUAL' AND period='FY' AND fiscal_year>=?",
                                 (self.code, self.p["base"])).fetchone()["c"]
        self.log(None, "*", "실적·전망 혼합 금지", "PASS" if bad == 0 else "FAIL",
                 "올해 이후 연도에 확정실적 없음 · 실적선과 전망선은 별도 계열" if bad == 0 else f"{bad}건 위반")
