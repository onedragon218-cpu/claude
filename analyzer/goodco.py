"""화면 M「좋은 기업 7조건」(2026-11-25 ④ ROA 추가, 처음 6조건)· 화면 N「숫자 모양」 (지시서 2026-11-07, 원본: DOCA OS p236·p244·한장그림)

- 판정 기호는 ✓ · △ · ✗ · 조사 · 판정 못 함(이유) 다섯 가지만 쓴다(⑦ 주가는 '✓(컨센 기준)'도 ✓로 센다 — 2026-11-25 번호 변경 전 ⑥).
- 컨센서스는 DB의 원래 값(raw)을 쓰고, 이 앱의 보정계수는 쓰지 않는다. 엔진 규칙대로 ⑦(예전 ⑥)·숫자 모양만 고정 ×0.83.
- 빈 연도·빈 분기는 만들어 넣지 않는다. 판정하지 못한 칸은 이유를 적는다.
- 리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님
"""
import csv
import datetime as dt
import glob
import json
import os
import statistics

from . import config, db

ENGINE_FACTOR = 0.83          # 공통 보정 규칙의 기본값(급하향이면 0.73 — analyzer/standards.py)
REQ_DIV = 7.8                 # 예전 기본값: 10배 × (1 − 22%). 지금은 10배 × (1 − 법정 한계세율)을 씀
MARKS = ("✓", "△", "✗", "조사")
PRINCIPLE = "좋은 기업이어도 주가가 많이 오르면 '투자하기 좋은 기업'이 아니다."
DISCLAIMER = "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"
NAMES = {      # 2026-11-25: ④ ROA 추가(뒤 번호 밀기) → 7조건. 내부 이름(one…six)은 그대로라 사람이 적은 칸(five)이 이어짐
    "one": "① 매출이 과거 정점을 넘음(올해 못 넘어도 컨센상 2년 안에 넘고 방향 유지)",
    "two": "② 영업이익률이 과거 정점을 넘거나 최근 3년보다 우상향",
    "three": "③ ROE가 과거 정점을 넘거나 최근 3년보다 우상향",
    "roa": "④ ROA가 과거 정점을 넘거나 최근 3년보다 우상향",
    "rel": "참고: ROE와 ROA의 관계 — 오른 ROE가 실력인가 빚(레버리지)인가(점수에 넣지 않음)",
    "four": "⑤ 부채비율이 통제 가능 범위 안이거나 최근 3년보다 줄어듦 · 이자보상배율 1배 미만이면 ✗, 1~3배면 △",
    "five": "⑥ 그 위에서 산업이 우호로 바뀌는 중이라 큰 폭 성장이 예상됨",
    "six": "⑦ 주가가 아직 크게 반영되지 않음",
}
SCORED = ("one", "two", "three", "roa", "four", "five", "six")


def manual_file():
    return os.path.join(config.DATA_DIR, "good6_manual.json")


def load_manual():
    try:
        with open(manual_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_manual(code, five_mark, five_text, four_note):
    if five_mark not in MARKS:
        raise ValueError("판정은 ✓·△·✗·조사 중 하나")
    d = load_manual()
    d[code] = {"five": [five_mark, (five_text or "").strip()[:500]], "four_note": (four_note or "").strip()[:300],
               "updated": dt.date.today().isoformat()}
    os.makedirs(os.path.dirname(manual_file()), exist_ok=True)
    with open(manual_file(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ---- 자료 읽기 ----
class Data:
    def __init__(self, conn, code, P):
        self.conn, self.code, self.P = conn, code, P
        rows = conn.execute("SELECT fiscal_year, period, metric, value, status FROM facts WHERE stock_code=? AND is_current=1",
                            (code,)).fetchall()
        self.f = {(r["fiscal_year"], r["period"], r["metric"]): (r["value"], r["status"]) for r in rows}
        cy = P["current"]
        self.years = sorted({y for (y, p, m) in self.f if p == "FY" and m == "acc_revenue" and y < cy})
        self.cons = {(r["target_year"], r["metric"]): dict(r) for r in db.latest_consensus(conn, code)}
        self.cy = cy

    def v(self, y, k, per="FY"):
        x = self.f.get((y, per, k))
        return None if x is None else x[0]

    def st(self, y, k, per="FY"):
        x = self.f.get((y, per, k))
        return None if x is None else x[1]

    def eok(self, y, k, per="FY"):
        x = self.v(y, k, per)
        return None if x is None else x / 1e8

    def c(self, y, m):
        r = self.cons.get((y, m))
        return None if r is None else r["value"]

    def n_est(self, y):
        for m in ("op_income", "revenue", "eps"):
            r = self.cons.get((y, m))
            if r is not None and r.get("n_estimates") is not None:
                return r["n_estimates"]
        return None

    def cons_asof(self):
        return max((r["as_of"] or "" for r in self.cons.values()), default=None) or None

    def latest_interim(self):
        for per in ("Q3", "H1", "Q1"):
            if self.v(self.cy, "acc_revenue", per) is not None or self.v(self.cy, "acc_debt", per) is not None:
                return per
        return None

    def net_debt(self):
        """가장 최근 시점의 순차입금(억원, 공통 기준: 차입 전부 + 리스 − 현금·단기금융). (값, 시점 설명)"""
        from . import standards
        try:
            from .value5 import notes_by_year
            xn = notes_by_year(self.code)
        except Exception:      # noqa: BLE001
            xn = {}
        get = lambda y, k, per="FY": self.v(y, k, per)
        if self.years:                     # 최근 결산에서 본문 차입금이 실제를 못 담으면(2026-11-28) 순차입금을 만들지 않음
            ly = self.years[-1]
            ip = (self.v(ly, "acc_int_paid") or 0) + (self.v(ly, "acc_int_paid_fin") or 0)
            hid, why = standards.hidden_debt(self.v(ly, "acc_debt"), self.v(ly - 1, "acc_debt"), ip, self.v(ly, "acc_assets"))
            if hid:
                return None, f"차입금 미식별 — {why} 차입금이 다른 금융부채 줄에 묶여 있는 것으로 보임({ly})"
        per = self.latest_interim()
        if per and self.v(self.cy, "acc_debt", per) is not None:
            nd = standards.net_debt(get, self.cy, per, xn.get(self.cy - 1))
            return nd["nd"] / 1e8, f"{self.cy} {per}말"
        for y in reversed(self.years):
            if self.v(y, "acc_debt") is not None:
                return standards.net_debt(get, y, "FY", xn.get(y))["nd"] / 1e8, f"{y} 연말"
        return None, "차입금 자료 없음"

    def market_cap_now(self):
        v = self.v(self.cy, "market_cap", "ASOF")
        return v, self.st(self.cy, "market_cap", "ASOF")


def _f(v, d=0):
    if v is None:
        return "–"
    return f"{v:,.{d}f}"


def _e(y):
    return f"{str(y)[2:]}E"


# ---- 엔진 다리(선택) ----
def engine_row(code):
    """환경변수 DOCA_ENGINE가 있으면 data/판/전황판_실전_*.csv 최신 파일에서 그 종목 줄을 읽는다. 없으면 None."""
    base = os.environ.get("DOCA_ENGINE")
    if not base:
        return None
    files = sorted(glob.glob(os.path.join(base, "data", "판", "전황판_실전_*.csv")))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("code", "")).strip().zfill(6) == code:
                    return dict(r, _file=os.path.basename(files[-1]))
    except OSError:
        return None
    return None


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---- 판정 규칙(함수 단위로 시험 가능) ----
def rule_three(r3, roe_est, rpk):
    """③ ROE: 컨센 추정이 정점을 넘으면 ✓, 최근 3년 우상향(마지막 해도 유지) ✓, 마지막 해 꺾임 △, 그 외 ✗"""
    if any(v is not None and rpk is not None and v > rpk for v in roe_est):
        return "✓"
    if r3[-1] > r3[0] and r3[-1] >= r3[-2]:
        return "✓"
    if r3[-1] > r3[0]:
        return "△"
    return "✗"


def rule_six(mc, nd, op27, op28, n27, last_op=None, last=None, tax=0.22, f27=ENGINE_FACTOR, f28=ENGINE_FACTOR):
    """⑦(내부 six) 반환: (판정, 숫자 칸). 요구 영업이익 = (시총 + 순차입금) ÷ (10 × (1 − 세율)), 컨센 영업이익은 공통 보정 계수"""
    if n27 is not None and n27 <= 1:
        return "판정 못 함(원천 대기)", f"{_e(last + 2) if last else '27E'} 컨센 원천 {n27}곳 — 원천이 더 생기면 판정"
    if mc is None:
        return "판정 못 함(시총 없음)", "지금 시가총액 자료 없음"
    if op27 is None:
        return "판정 못 함(컨센 없음)", "영업이익 컨센서스 없음"
    div = 10 * (1 - tax)
    req = (mc + (nd or 0)) / div
    r27, r28 = op27 * f27, (op28 * f28 if op28 is not None else None)
    mark = "✓(컨센 기준)" if req < r27 else "✗"
    txt = (f"지금 시총이 요구하는 영업이익 {_f(req)}억(÷{div:.2f} = 10배 × (1−{tax:.1%})) vs 27E×{f27} {_f(r27)}억 · 28E×{f28} {_f(r28)}억"
           + (f" · {last} 실적 {_f(last_op)}억" if last else ""))
    if n27 is None:
        txt += " · 원천 수 미확인"
    return mark, txt


def good6(conn, code, P):
    D = Data(conn, code, P)
    man = load_manual().get(code) or {}
    out = {"names": NAMES, "principle": PRINCIPLE, "disclaimer": DISCLAIMER, "cons_asof": D.cons_asof(),
           "factor_note": "보정: 앱 공통 규칙(×0.83, 3개월 사이 5% 넘게 하향이면 ×0.73) · 세율: 법정 한계세율(지방세 포함)",
           "manual": man, "rows": {}}
    rev = {y: D.eok(y, "acc_revenue") for y in D.years}
    rev = {y: v for y, v in rev.items() if v is not None and v > 0}
    if len(rev) < 3:
        why = "매출 칸이 없거나 실적 연도가 3년 미만이라 계산하지 않음(금융사 등)"
        for k in NAMES:
            out["rows"][k] = {"mark": "— 판정 안 함", "text": why}
        out["n_conditions"] = len(SCORED)
        out["score"] = 0
        return out
    cy = D.cy
    ys = sorted(rev)
    last = ys[-1]
    l3 = [last - 2, last - 1, last]
    est_y = [cy, cy + 1, cy + 2]
    n27 = D.n_est(cy + 1)
    n_first = D.n_est(cy)

    # ①
    pky = max(rev, key=rev.get)
    pk = rev[pky]
    est = {y: D.c(y, "revenue") for y in est_y if D.c(y, "revenue") is not None}
    over = [y for y in est_y if y in est and est[y] > pk]
    if est:
        trend = (est.get(cy + 2) is not None and est.get(cy + 1) is not None and est[cy + 2] >= est[cy + 1] * 0.97)
        cons_pass = bool(over) and min(over) <= cy + 2 and trend
    else:
        cons_pass = False
    mark = "✓" if rev[last] >= pk or cons_pass else "✗"
    t = f"과거 정점 {_f(pk)}억({pky}) · {last} {_f(rev[last])}억"
    if est:
        t += " · 컨센 " + " → ".join(f"{_e(y)} {_f(est.get(y))}" for y in est_y)
        t += f" · 처음 넘는 해 {_e(min(over))}" if over else " · 컨센으로도 못 넘음"
        if rev[last] < pk and cons_pass:
            n = n27 if n27 is not None else n_first
            t += f" · 원천 {n if n is not None else '미확인'}곳"
    else:
        t += " · 컨센 없음"
    out["rows"]["one"] = {"mark": mark, "text": t}

    # ②
    op = {y: D.eok(y, "acc_op") for y in ys}
    m = {y: op[y] / rev[y] * 100 for y in ys if op.get(y) is not None}
    if len(m) >= 3 and all(y in m for y in l3):
        mpy = max(m, key=m.get)
        m_est = {y: D.c(y, "op_income") / D.c(y, "revenue") * 100 for y in est_y
                 if D.c(y, "op_income") is not None and D.c(y, "revenue")}
        up3 = m[last] > m[last - 2]
        mark = "✓" if any(v > m[mpy] for v in m_est.values()) or up3 else "✗"
        t = (f"과거 정점 {m[mpy]:.1f}%({mpy}) · 최근 3년 " + " → ".join(f"{m[y]:.1f}" for y in l3)
             + f"% ({'우상향' if up3 else '우상향 아님'})")
        t += (" · 컨센 " + " → ".join(f"{_e(y)} {m_est[y]:.1f}" if y in m_est else f"{_e(y)} –" for y in est_y) + "%") if m_est else " · 컨센 없음"
        out["rows"]["two"] = {"mark": mark, "text": t}
    else:
        out["rows"]["two"] = {"mark": "판정 못 함(영업이익 자료 부족)", "text": "최근 3년 영업이익이 다 있지 않음"}

    # ③ ROE · ④ ROA — 같은 규칙(과거 정점을 넘거나 최근 3년 우상향). 전망은 둘 다 FnGuide Financial Highlight 추정치(같은 출처)
    def ratio_rule(key, cons_key, label):
        hist = {y: D.v(y, key) for y in ys if D.v(y, key) is not None}
        gap_note = ""
        if not all(y in hist for y in l3):
            # 마지막 해가 아닌 한 해만 '자료 충돌'로 빠졌으면(2026-11-28) 남은 두 해로 판정: 마지막 ≤ 앞 해면 ✗, 아니면 △까지만(우상향 유지 확인 불가)
            miss = [y for y in l3 if y not in hist]
            if len(miss) == 1 and miss[0] != l3[2] and D.st(miss[0], key) == "CONFLICT":
                gap_note = f" · {miss[0]}년은 자료 충돌로 빠짐(남은 두 해로 판정)"
            else:
                return {"mark": f"판정 못 함({label} 자료 부족)", "text": f"최근 3년 {label}가 다 있지 않음"}, None
        py = max(hist, key=hist.get)
        if gap_note:
            earlier, lastv = [hist[y] for y in l3 if y in hist]
            r3 = [earlier, lastv, lastv] if lastv <= earlier else [earlier, float("inf"), lastv]
        else:
            r3 = [hist[y] for y in l3]
        est = {y: D.c(y, cons_key) for y in est_y if D.c(y, cons_key) is not None}
        src = "FnGuide 추정"
        if not est and key == "roe":                  # FnGuide 추정이 없으면 예전 방식(순이익 컨센으로 추산)
            eq = D.eok(last, "acc_eq")
            if eq:
                for y in est_y:
                    ni = D.c(y, "net_income")
                    if ni is None:
                        break
                    est[y] = ni / (eq + ni / 2) * 100
                    eq += ni * 0.75
            src = "컨센 추정(해석 — FnGuide 추정 없음)"
        mark = rule_three(r3, list(est.values()), hist[py])
        actual = rule_three(r3, [], hist[py])        # 실적만으로 본 판정(2026-11-28: 컨센서스 덕에 ✓인지 드러나게)
        if mark == "✓" and actual != "✓":
            mark = "✓(컨센 기준)"
        t = f"과거 정점 {hist[py]:.1f}%({py}) · 최근 3년 " + " → ".join("충돌" if hist.get(y) is None else f"{hist[y]:.1f}" for y in l3) + "%" + gap_note
        t += (f" · {src} " + " → ".join(f"{_e(y)} {v:.1f}" for y, v in est.items()) + "%") if est else f" · 추정 없음"
        if mark.startswith("✓(컨센"):
            t += f" · 실적만으로는 {actual}(추정이 과거 정점 {hist[py]:.1f}%를 넘어 ✓)"
        return {"mark": mark, "text": t, "actual": actual, "last": r3[-1] if not gap_note else hist[l3[2]]}, r3
    out["rows"]["three"], roe3 = ratio_rule("roe", "roe_fg", "ROE")
    out["rows"]["roa"], roa3 = ratio_rule("roa", "roa", "ROA")
    # 참고: ROE와 ROA의 관계(점수 아님) — 레버리지 = 평균 자산 ÷ 평균 지배자본(ROE÷ROA는 지배/전체 기준이 섞여 직접 계산)
    lev = []
    for y in l3:
        a, pa, e, pe = D.eok(y, "acc_assets"), D.eok(y - 1, "acc_assets"), D.eok(y, "acc_eq_owner"), D.eok(y - 1, "acc_eq_owner")
        lev.append((a + pa) / (e + pe) if a and pa and e and pe and (e + pe) > 0 else (a / e if a and e and e > 0 else None))
    up = lambda m: m.startswith("✓") or m.startswith("△")
    r3o, r4o = out["rows"]["three"], out["rows"]["roa"]
    # 관계는 실적 기준 판정으로만(2026-11-28: 컨센서스 덕의 ✓로 '실력 개선'이라 쓰지 않게)
    m3, m4 = r3o.get("actual", r3o["mark"]), r4o.get("actual", r4o["mark"])
    neg = r3o.get("last") is not None and r3o["last"] <= 0
    if m3.startswith("판정 못 함") or m4.startswith("판정 못 함"):
        rel = "판정 못 함"
    elif up(m3) and up(m4) and neg:
        rel = "적자 축소(ROE·ROA 함께 오르지만 ROE가 아직 0 이하)"
    elif up(m3) and up(m4):
        rel = "실력 개선(ROE·ROA 함께 오름)"
    elif up(m3):
        rel = "부채 효과 주의(ROE만 오름 — 빚으로 키운 ROE)"
    elif up(m4):
        rel = "빚 줄이며 체력 개선(ROA는 오르고 ROE는 아님)"
    else:
        rel = "악화(ROE·ROA 함께 약함)"
    lt = "레버리지(평균 자산 ÷ 평균 지배자본) " + " → ".join("–" if v is None else f"{v:.2f}" for v in lev) + "배"
    if None not in lev:
        lt += " (늘어남)" if lev[-1] > lev[0] * 1.05 else " (줄어듦)" if lev[-1] < lev[0] * 0.95 else " (비슷함)"
    if roe3 and roa3:
        lt += " · ROE " + " → ".join(f"{v:.1f}" for v in roe3) + "% · ROA " + " → ".join(f"{v:.1f}" for v in roa3) + "%"
    out["rows"]["rel"] = {"mark": rel, "text": lt}

    # ⑤(내부 이름 four) 부채비율 + 이자 감당력(2026-11-28 사용자 결정: 이자보상배율 1배 미만 ✗, 1~3배 △, 판정 옆에 숫자 표시)
    d3 = [D.v(y, "debt_ratio") for y in l3]
    icr, icr_t = interest_cover(D, last)
    nde_t = nd_ebitda_text(D, last)
    if None not in d3:
        mark = "✓" if d3[-1] <= 100 or d3[-1] < d3[0] else "✗"
        t = "최근 3년 " + " → ".join(f"{v:.0f}" for v in d3) + "% "
        t += ("(100% 이하 = 통제 가능)" if d3[-1] <= 100 else
              "(100% 위지만 3년 전보다 줄어드는 중 — 규칙상 ✓, 수준은 높음)" if d3[-1] < d3[0] else "(100% 위 · 줄지 않음)")
        if d3[0] < d3[1] < d3[2]:
            t += " · 3년 연속 상승"
        if icr is not None and icr < ICR_FAIL:
            mark = "✗"
            t += f" · 이자보상배율 {icr:.2f}배 — {ICR_FAIL:g}배 미만이라 부채비율과 관계없이 ✗"
        elif icr is not None and icr < ICR_WARN and mark == "✓":
            mark = "△"
            t += f" · 이자보상배율 {icr:.2f}배 — {ICR_FAIL:g}~{ICR_WARN:g}배라 △"
        else:
            t += f" · {icr_t}"
        t += f" · {nde_t}"
        if man.get("four_note"):
            t += f" · 메모: {man['four_note']}"
        out["rows"]["four"] = {"mark": mark, "text": t, "icr": icr}
    else:
        out["rows"]["four"] = {"mark": "판정 못 함(부채비율 자료 부족)", "text": "최근 3년 부채비율이 다 있지 않음"}

    # ⑥(내부 five) 사람이 적는 칸
    five = man.get("five")
    if five and five[0] in MARKS:
        out["rows"]["five"] = {"mark": five[0], "text": f"{five[1]} (사람 판정 {man.get('updated', '')})".strip()}
    else:
        out["rows"]["five"] = {"mark": "조사", "text": "노드 판정 — 대화 세션이 채움(위에서 아래·아래에서 위)"}

    # ⑦(내부 six) 주가
    mc, mc_st = D.market_cap_now()
    nd, nd_when = D.net_debt()
    from . import standards
    Fs = standards.factors(conn, code, P)
    tax6, _ = standards.marginal_tax(cy + 1, None, [D.v(last - i, "acc_pretax") for i in range(5)])
    mark, t = rule_six(mc, nd, D.c(cy + 1, "op_income"), D.c(cy + 2, "op_income"), n27, op.get(last), last,
                       tax=tax6, f27=Fs[(cy + 1, "op_income")][0], f28=Fs[(cy + 2, "op_income")][0])
    if mc is not None:
        t += f" · 시총 {_f(mc)}억{'(역산 주식수 · UNVERIFIED)' if mc_st == 'UNVERIFIED' else ''} · 순차입 {_f(nd)}억({nd_when})"
    eng = engine_row(code)
    if eng and _num(eng.get("보수중심(원)")) and D.v(cy, "price_close", "ASOF"):
        x = D.v(cy, "price_close", "ASOF") / _num(eng["보수중심(원)"])
        t += f" · 주가 ÷ 보수중심 {x:.2f}(1 아래면 확정 이익력으로도 쌈)"
    out["rows"]["six"] = {"mark": mark, "text": t}
    out["engine"] = bool(eng)
    out["engine_why"] = None if eng else "보수중심은 엔진 값 — 연결 안 됨"
    out["score"] = sum(1 for k in SCORED if (out["rows"].get(k) or {}).get("mark", "").startswith("✓"))
    out["n_conditions"] = len(SCORED)
    out["last"] = last
    return out


ICR_FAIL, ICR_WARN = 1.0, 3.0      # 이자보상배율 기준(사용자 확정 2026-11-28)


def interest_cover(D, y):
    """이자보상배율 = 영업이익 ÷ 이자 지급(현금흐름표 영업·재무 분류 합, 화면 H와 같은 값). (값, 설명)"""
    op = D.v(y, "acc_op")
    ip = (D.v(y, "acc_int_paid") or 0) + (D.v(y, "acc_int_paid_fin") or 0)
    if op is None:
        return None, "이자보상배율 –(영업이익 없음)"
    if not ip:
        return None, "이자보상배율 –(이자 지급 없음: 무차입이거나 자료 없음)"
    return op / ip, f"이자보상배율 {op / ip:.2f}배({y})"


def nd_ebitda_text(D, y):
    nd, when = D.net_debt()
    op, dep, am = D.eok(y, "acc_op"), D.eok(y, "acc_dep"), D.eok(y, "acc_amort")
    if nd is None:
        return f"순차입금 ÷ EBITDA –({when})"
    if op is None or dep is None:
        return f"순차입금 {nd:,.0f}억({when}) · EBITDA –(감가상각비 없음)"
    ebitda = op + dep + (am or 0)
    if ebitda <= 0:
        return f"순차입금 {nd:,.0f}억({when}) · EBITDA {ebitda:,.0f}억(0 이하라 배수 의미 없음)"
    return f"순차입금 ÷ EBITDA {nd / ebitda:.1f}배(순차입금 {nd:,.0f}억 {when} · EBITDA {ebitda:,.0f}억 {y})"


# ---- 화면 N ----
def quarters(D):
    """저장된 누적값으로 만들 수 있는 분기만(억원). 빠진 분기는 만들지 않는다."""
    out = []
    for y in range(D.cy - 3, D.cy + 1):
        cum = {per: (D.eok(y, "acc_revenue", per), D.eok(y, "acc_op", per)) for per in ("Q1", "H1", "Q3", "FY")}

        def diff(a, b):
            if a is None or b is None or None in a or None in b:
                return None
            return (a[0] - b[0], a[1] - b[1])
        q1 = cum["Q1"] if None not in cum["Q1"] else None
        for qn, val in ((1, q1), (2, diff(cum["H1"], cum["Q1"])), (3, diff(cum["Q3"], cum["H1"])), (4, diff(cum["FY"], cum["Q3"]))):
            if val is not None:
                out.append({"label": f"{str(y)[2:]}.{qn}Q", "revenue": val[0], "op": val[1]})
    return out[-12:]


def year_pbr(D, y):
    """연말 PBR = 연말 시가총액(억) ÷ 지배주주 자본(억). 화면 N 표와 화면 P 'min PBR'이 같은 값을 쓴다."""
    mc, eqo = D.v(y, "market_cap"), D.eok(y, "acc_eq_owner")
    return mc / eqo if mc and eqo and eqo > 0 else None


def min_pbr(D):
    """최근 10개 연도(화면 N 표와 같은 범위)의 가장 낮은 연말 PBR × 최근 결산 실제 BPS.
    BPS = DART 계산값(bps), 없으면 그해 연말 주가 ÷ 연말 PBR. 반환: dict 또는 None"""
    cy = D.cy
    pb = [(y, year_pbr(D, y)) for y in D.years if y >= cy - 10]
    pb = [(y, v) for y, v in pb if v is not None and v > 0]
    if not pb:
        return None
    y0, v0 = min(pb, key=lambda x: (x[1], -x[0]))
    bps = by = None
    for y in reversed(D.years):
        b = D.v(y, "bps")
        if b is None:
            p, v = D.v(y, "price_close"), year_pbr(D, y)
            b = p / v if p and v else None
        if b is not None and b > 0:
            bps, by = b, y
            break
    if bps is None:
        return {"pbr": v0, "year": y0, "n": len(pb), "bps": None, "bps_year": None, "price": None,
                "unverified": D.st(y0, "market_cap") == "UNVERIFIED"}
    return {"pbr": round(v0, 4), "year": y0, "n": len(pb), "bps": bps, "bps_year": by, "price": v0 * bps,
            "unverified": D.st(y0, "market_cap") == "UNVERIFIED" or D.st(by, "bps") == "UNVERIFIED"}


def _lev(D, y):
    """레버리지 = 평균 자산 ÷ 평균 지배자본(전년 말·그해 말). 전년이 없으면 그해 말끼리"""
    a, pa, e, pe = D.eok(y, "acc_assets"), D.eok(y - 1, "acc_assets"), D.eok(y, "acc_eq_owner"), D.eok(y - 1, "acc_eq_owner")
    if a and pa and e and pe and (e + pe) > 0:
        return (a + pa) / (e + pe)
    return a / e if a and e and e > 0 else None


def shape(conn, code, P):
    from .report import consensus_trend
    D = Data(conn, code, P)
    cy = D.cy
    past = [y for y in D.years if y >= cy - 10]
    rows = []
    for y in past:
        rev, op, ocf = D.eok(y, "acc_revenue"), D.eok(y, "acc_op"), D.eok(y, "acc_ocf")
        rows.append({"year": y, "revenue": rev, "op": op, "margin": op / rev * 100 if rev and op is not None else None,
                     "ocf": ocf, "pbr": year_pbr(D, y),
                     "pbr_unverified": D.st(y, "market_cap") == "UNVERIFIED", "roe": D.v(y, "roe"), "roa": D.v(y, "roa"),
                     "lev": _lev(D, y)})
    per = D.latest_interim()
    partial = None
    if per and D.v(cy, "acc_revenue", per) is not None:
        rev, op = D.eok(cy, "acc_revenue", per), D.eok(cy, "acc_op", per)
        nd, nd_when = D.net_debt()
        partial = {"label": f"{cy} " + {"H1": "상반기", "Q3": "3분기 누적", "Q1": "1분기"}[per], "revenue": rev, "op": op,
                   "margin": op / rev * 100 if rev and op is not None else None, "ocf": D.eok(cy, "acc_ocf", per),
                   "net_debt": nd, "net_debt_when": nd_when}
    rev3m = {(it["year"], it["metric"]): it for it in consensus_trend(conn, code, P).get("summary", [])}
    from . import standards
    Fn = standards.factors(conn, code, P)
    cons = []
    for y in (cy, cy + 1, cy + 2):
        rv, op = D.c(y, "revenue"), D.c(y, "op_income")
        if rv is None and op is None:
            cons.append({"year": y, "none": True})
            continue
        r = rev3m.get((y, "op_income")) or {}
        fy_ = Fn[(y, "op_income")][0]
        from .report import cons_gap
        gap = [g for g in cons_gap(lambda m, yy: (D.c(yy, m), (D.cons.get((yy, m)) or {}).get("provider")), y) if g["big"]]
        cons.append({"year": y, "revenue": rv, "op": op, "op_adj": op * fy_ if op is not None else None, "factor": fy_,
                     "roe": D.c(y, "roe_fg"), "roa": D.c(y, "roa"), "gap": gap,
                     "margin": op / rv * 100 if rv and op is not None else None,
                     "rev3m": (r.get("ch") or {}).get(3), "rev3m_tag": (r.get("tag") or {}).get(3)})
    ms = [r["margin"] for r in rows if r["margin"] is not None]
    last_c = next((c for c in reversed(cons) if not c.get("none") and c.get("margin") is not None), None)
    memo = None
    if ms:
        memo = f"10년 이익률 가운데 {statistics.median(ms):.1f}% · 최고 {max(ms):.1f}%"
        if last_c:
            memo += f" · {_e(last_c['year'])} {last_c['margin']:.1f}%"
        eng = engine_row(code)
        if eng:
            extra = [f"{k} {eng[k]}" for k in ("8년읽기", "8년통과(9칸)", "이익률흐름") if eng.get(k)]
            if extra:
                memo += " · 엔진: " + " · ".join(extra)
    q = quarters(D)
    return {"rows": rows, "partial": partial, "cons": cons, "memo": memo, "quarters": q,
            "quarters_note": None if len(q) >= 4 else "분기 자료 미수집 — 최근 3년 1분기·반기·3분기 보고서 수집 필요",
            "factor": "공통 규칙(×0.83, 급하향 ×0.73)", "cons_asof": D.cons_asof(), "disclaimer": DISCLAIMER}
