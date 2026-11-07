"""화면 M「좋은 기업 6조건」· 화면 N「숫자 모양」 (지시서 2026-11-07, 원본: DOCA OS p236·p244·한장그림)

- 판정 기호는 ✓ · △ · ✗ · 조사 · 판정 못 함(이유) 다섯 가지만 쓴다(⑥은 '✓(컨센 기준)'도 ✓로 센다).
- 컨센서스는 DB의 원래 값(raw)을 쓰고, 이 앱의 보정계수는 쓰지 않는다. 엔진 규칙대로 ⑥·숫자 모양만 고정 ×0.83.
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

ENGINE_FACTOR = 0.83          # 엔진 규칙: 이익 컨센서스 고정 할인
REQ_DIV = 7.8                 # 10배 × (1 − 세율 22%)
MARKS = ("✓", "△", "✗", "조사")
PRINCIPLE = "좋은 기업이어도 주가가 많이 오르면 '투자하기 좋은 기업'이 아니다."
DISCLAIMER = "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"
NAMES = {
    "one": "① 매출이 과거 정점을 넘음(올해 못 넘어도 컨센상 2년 안에 넘고 방향 유지)",
    "two": "② 영업이익률이 과거 정점을 넘거나 최근 3년보다 우상향",
    "three": "③ ROE가 과거 정점을 넘거나 최근 3년보다 우상향",
    "four": "④ 부채비율이 통제 가능 범위 안이거나 최근 3년보다 줄어듦",
    "five": "⑤ 그 위에서 산업이 우호로 바뀌는 중이라 큰 폭 성장이 예상됨",
    "six": "⑥ 주가가 아직 크게 반영되지 않음",
}


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
        """가장 최근 시점의 차입금 − 현금(억원). (값, 시점 설명)"""
        per = self.latest_interim()
        if per and self.v(self.cy, "acc_debt", per) is not None:
            return (self.v(self.cy, "acc_debt", per) - (self.v(self.cy, "acc_cash", per) or 0)) / 1e8, f"{self.cy} {per}말"
        for y in reversed(self.years):
            if self.v(y, "acc_debt") is not None:
                return (self.v(y, "acc_debt") - (self.v(y, "acc_cash") or 0)) / 1e8, f"{y} 연말"
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


def rule_six(mc, nd, op27, op28, n27, last_op=None, last=None):
    """⑥ 반환: (판정, 숫자 칸)"""
    if n27 is not None and n27 <= 1:
        return "판정 못 함(원천 대기)", f"{_e(last + 2) if last else '27E'} 컨센 원천 {n27}곳 — 원천이 더 생기면 판정"
    if mc is None:
        return "판정 못 함(시총 없음)", "지금 시가총액 자료 없음"
    if op27 is None:
        return "판정 못 함(컨센 없음)", "영업이익 컨센서스 없음"
    req = (mc + (nd or 0)) / REQ_DIV
    r27, r28 = op27 * ENGINE_FACTOR, (op28 * ENGINE_FACTOR if op28 is not None else None)
    mark = "✓(컨센 기준)" if req < r27 else "✗"
    txt = (f"지금 시총이 요구하는 영업이익 {_f(req)}억 vs 27E×0.83 {_f(r27)}억 · 28E×0.83 {_f(r28)}억"
           + (f" · {last} 실적 {_f(last_op)}억" if last else ""))
    if n27 is None:
        txt += " · 원천 수 미확인"
    return mark, txt


def good6(conn, code, P):
    D = Data(conn, code, P)
    man = load_manual().get(code) or {}
    out = {"names": NAMES, "principle": PRINCIPLE, "disclaimer": DISCLAIMER, "cons_asof": D.cons_asof(),
           "factor_note": "보정: 고정 0.83(엔진 규칙) — 화면 위 '컨센서스 보정 적용'과 무관",
           "manual": man, "rows": {}}
    rev = {y: D.eok(y, "acc_revenue") for y in D.years}
    rev = {y: v for y, v in rev.items() if v is not None and v > 0}
    if len(rev) < 3:
        why = "매출 칸이 없거나 실적 연도가 3년 미만이라 계산하지 않음(금융사 등)"
        for k in NAMES:
            out["rows"][k] = {"mark": "— 판정 안 함", "text": why}
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

    # ③
    roe = {y: D.v(y, "roe") for y in ys if D.v(y, "roe") is not None}
    if all(y in roe for y in l3):
        rpy = max(roe, key=roe.get)
        rpk = roe[rpy]
        r3 = [roe[y] for y in l3]
        eq = D.eok(last, "acc_eq")
        roe_est = {}
        if eq:
            for y in est_y:
                ni = D.c(y, "net_income")
                if ni is None:
                    break
                roe_est[y] = ni / (eq + ni / 2) * 100
                eq += ni * 0.75
        mark = rule_three(r3, list(roe_est.values()), rpk)
        t = f"과거 정점 {rpk:.1f}%({rpy}) · 최근 3년 " + " → ".join(f"{v:.1f}" for v in r3) + "%"
        t += (" · 컨센 추정(해석) " + " → ".join(f"{_e(y)} {v:.1f}" for y, v in roe_est.items()) + "%") if roe_est \
            else " · 컨센 추정(해석) 없음(순이익 컨센 없음)"
        out["rows"]["three"] = {"mark": mark, "text": t}
    else:
        out["rows"]["three"] = {"mark": "판정 못 함(ROE 자료 부족)", "text": "최근 3년 ROE가 다 있지 않음"}

    # ④
    d3 = [D.v(y, "debt_ratio") for y in l3]
    if None not in d3:
        mark = "✓" if d3[-1] <= 100 or d3[-1] < d3[0] else "✗"
        t = "최근 3년 " + " → ".join(f"{v:.0f}" for v in d3) + "% "
        t += ("(100% 이하 = 통제 가능)" if d3[-1] <= 100 else
              "(100% 위지만 3년 전보다 줄어드는 중 — 규칙상 ✓, 수준은 높음)" if d3[-1] < d3[0] else "(100% 위 · 줄지 않음)")
        if man.get("four_note"):
            t += f" · 메모: {man['four_note']}"
        out["rows"]["four"] = {"mark": mark, "text": t}
    else:
        out["rows"]["four"] = {"mark": "판정 못 함(부채비율 자료 부족)", "text": "최근 3년 부채비율이 다 있지 않음"}

    # ⑤ 사람이 적는 칸
    five = man.get("five")
    if five and five[0] in MARKS:
        out["rows"]["five"] = {"mark": five[0], "text": f"{five[1]} (사람 판정 {man.get('updated', '')})".strip()}
    else:
        out["rows"]["five"] = {"mark": "조사", "text": "노드 판정 — 대화 세션이 채움(위에서 아래·아래에서 위)"}

    # ⑥
    mc, mc_st = D.market_cap_now()
    nd, nd_when = D.net_debt()
    mark, t = rule_six(mc, nd, D.c(cy + 1, "op_income"), D.c(cy + 2, "op_income"), n27, op.get(last), last)
    if mc is not None:
        t += f" · 시총 {_f(mc)}억{'(역산 주식수 · UNVERIFIED)' if mc_st == 'UNVERIFIED' else ''} · 순차입 {_f(nd)}억({nd_when})"
    eng = engine_row(code)
    if eng and _num(eng.get("보수중심(원)")) and D.v(cy, "price_close", "ASOF"):
        x = D.v(cy, "price_close", "ASOF") / _num(eng["보수중심(원)"])
        t += f" · 주가 ÷ 보수중심 {x:.2f}(1 아래면 확정 이익력으로도 쌈)"
    out["rows"]["six"] = {"mark": mark, "text": t}
    out["engine"] = bool(eng)
    out["engine_why"] = None if eng else "보수중심은 엔진 값 — 연결 안 됨"
    out["score"] = sum(1 for r in out["rows"].values() if r["mark"].startswith("✓"))
    out["last"] = last
    return out


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


def shape(conn, code, P):
    from .report import consensus_trend
    D = Data(conn, code, P)
    cy = D.cy
    past = [y for y in D.years if y >= cy - 10]
    rows = []
    for y in past:
        rev, op, ocf = D.eok(y, "acc_revenue"), D.eok(y, "acc_op"), D.eok(y, "acc_ocf")
        mc, eqo = D.v(y, "market_cap"), D.eok(y, "acc_eq_owner")
        rows.append({"year": y, "revenue": rev, "op": op, "margin": op / rev * 100 if rev and op is not None else None,
                     "ocf": ocf, "pbr": mc / eqo if mc and eqo and eqo > 0 else None,
                     "pbr_unverified": D.st(y, "market_cap") == "UNVERIFIED", "roe": D.v(y, "roe")})
    per = D.latest_interim()
    partial = None
    if per and D.v(cy, "acc_revenue", per) is not None:
        rev, op = D.eok(cy, "acc_revenue", per), D.eok(cy, "acc_op", per)
        nd, nd_when = D.net_debt()
        partial = {"label": f"{cy} " + {"H1": "상반기", "Q3": "3분기 누적", "Q1": "1분기"}[per], "revenue": rev, "op": op,
                   "margin": op / rev * 100 if rev and op is not None else None, "ocf": D.eok(cy, "acc_ocf", per),
                   "net_debt": nd, "net_debt_when": nd_when}
    rev3m = {(it["year"], it["metric"]): it for it in consensus_trend(conn, code, P).get("summary", [])}
    cons = []
    for y in (cy, cy + 1, cy + 2):
        rv, op = D.c(y, "revenue"), D.c(y, "op_income")
        if rv is None and op is None:
            cons.append({"year": y, "none": True})
            continue
        r = rev3m.get((y, "op_income")) or {}
        cons.append({"year": y, "revenue": rv, "op": op, "op_adj": op * ENGINE_FACTOR if op is not None else None,
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
            "factor": ENGINE_FACTOR, "cons_asof": D.cons_asof(), "disclaimer": DISCLAIMER}
