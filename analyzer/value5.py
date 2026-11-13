"""화면 O「가치 5방법」 — DCF · RIM · 자산가치 · 스트레스 · 재무위험 (사용자 지시 2026-11-08)

자료(읽기 전용, 기존 표·계산은 바꾸지 않음)
- facts(DB): 매출·영업이익·지배순이익·지배자본·자본·부채총계·현금(+단기금융)·차입 합계·리스(본문)·이자지급·CAPEX·감가상각
  ·주식수·자사주·주가
- 저장된 DART 재무제표 원본(seed·data/raw): 매출채권·재고·매입채무·비지배지분·유형자산·유동 차입·전환사채·배당금 지급
- XBRL 원본(data/raw/<코드>/xbrl_*.zip, 주석): 토지·건물·설비, 이자비용, 차입금 만기(2·3년차), 단기금융상품, 리스부채
- 컨센서스: WiseReport 원래 값 × 0.83(3개월 사이 5% 넘게 깎였으면 × 0.73)

빈칸은 추정으로 채우지 않고 이유를 적는다. 리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님.
"""
import glob
import io
import json
import math
import os
import re
import zipfile

from . import config, db
from .collectors import dart

TAX = 0.22            # 세율을 못 정한 해에만 쓰는 예비값(보통은 가치평가 G의 법정 한계세율을 받음)
DEFAULTS = {"ke_premium": 0.05, "wacc": None, "omega": 0.6,
            "rec": {"cash": 1.0, "recv": 0.85, "inv": 0.60, "land": 1.0, "bldg": 0.30, "other": 0.0}}
CUT_NORMAL, CUT_DEEP, DEEP_DROP = 0.83, 0.73, -5.0
STRESS_YEARS = 20          # ④ 불황 영업이익: 올해 기준 과거 20년(자료가 없으면 있는 범위)
STRESS_PCT = 0.20


# ---------- 저장된 DART 재무제표 원본에서 추가 계정 읽기 ----------
def _nm(it):
    return re.sub(r"\s", "", it.get("account_nm", ""))


def _first(lst, col, sj, ids=(), names=(), name_rx=None, exclude_ids=()):
    for it in lst:
        if it.get("sj_div") == sj and dart._norm_id(it.get("account_id")) in ids:
            v = dart._num(it.get(col))
            if v is not None:
                return v
    for it in lst:
        if it.get("sj_div") != sj or dart._norm_id(it.get("account_id")) in exclude_ids:
            continue
        if _nm(it) in names or (name_rx and name_rx.search(_nm(it))):
            v = dart._num(it.get(col))
            if v is not None:
                return v
    return None


DEBT_RX = re.compile(r"(차입금|사채|유동성장기부채)")
CUR_RX = re.compile(r"(단기|유동성|^유동)")
CB_RX = re.compile(r"(전환사채|신주인수권부사채|교환사채)")


def face_values(lst, col):
    """한 열(당기·전기·전전기)의 추가 계정. 값은 원."""
    has_bs = any(it.get("sj_div") == "BS" and dart._num(it.get(col)) is not None for it in lst)
    if not has_bs:
        return {}
    out = {
        "recv": _first(lst, col, "BS", ("ifrs-full_CurrentTradeReceivables", "ifrs-full_TradeAndOtherCurrentReceivables"),
                       ("매출채권", "매출채권및기타채권", "매출채권및기타유동채권", "매출채권및기타수취채권"),
                       exclude_ids=("ifrs-full_NoncurrentTradeReceivables", "ifrs-full_TradeAndOtherNoncurrentReceivables")),
        "inv": _first(lst, col, "BS", ("ifrs-full_Inventories",), ("재고자산",)),
        "pay": _first(lst, col, "BS", ("ifrs-full_TradeAndOtherCurrentPayablesToTradeSuppliers", "ifrs-full_TradeAndOtherCurrentPayables",
                                       "ifrs-full_CurrentTradePayables"),
                      ("매입채무", "매입채무및기타채무", "매입채무및기타유동채무", "매입채무및기타지급채무")),
        "nci": _first(lst, col, "BS", ("ifrs-full_NoncontrollingInterests",), ("비지배지분",)),
        "ppe": _first(lst, col, "BS", ("ifrs-full_PropertyPlantAndEquipment",), ("유형자산",)),
    }
    if out["inv"] is None:              # 재무상태표는 있는데 재고 줄이 없음 → 0(재고 없는 회사)
        out["inv"], out["inv_absent"] = 0.0, True
    cur, cb, hit = 0.0, 0.0, False
    for it in lst:
        if it.get("sj_div") != "BS":
            continue
        nm = _nm(it)
        v = dart._num(it.get(col))
        if v is None or "리스" in nm or dart.SUM_EXCLUDE.search(nm) or not DEBT_RX.search(nm):
            continue
        if CUR_RX.search(nm) and not (nm.startswith("비유동") and "유동성" not in nm):
            cur += v; hit = True
        if CB_RX.search(nm):
            cb += v
    out["debt_cur"] = cur if hit else 0.0
    out["cb"] = cb
    div = _first(lst, col, "CF", ("ifrs-full_DividendsPaidClassifiedAsFinancingActivities",), ("배당금의지급", "배당금지급", "배당금의지급(재무)"))
    out["div"] = abs(div) if div is not None else None
    return {k: v for k, v in out.items() if v is not None}


def raw_files(code):
    fs = glob.glob(os.path.join(config.SEED_DIR, code, "dart_*_11011_CFS*.json"))
    fs += glob.glob(os.path.join(config.DATA_DIR, "raw", code, "dart_*_11011_CFS*.json"))
    return fs


def face_by_year(code):
    """{연도: {계정: 값}} — 그 연도 보고서(당기) > 다음 해 보고서(전기) > 다다음 해(전전기) 순으로 채움"""
    cand = {}
    for f in raw_files(code):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        lst = d.get("list") or []
        if not lst or lst[0].get("reprt_code") != "11011":
            continue
        y0 = int(lst[0]["bsns_year"])
        for rank, (col, y) in enumerate((("thstrm_amount", y0), ("frmtrm_amount", y0 - 1), ("bfefrmtrm_amount", y0 - 2))):
            for k, v in face_values(lst, col).items():
                cand.setdefault(y, {}).setdefault(k, []).append((rank, -y0, v))
    return {y: {k: sorted(vs)[0][2] for k, vs in d.items()} for y, d in cand.items()}


# ---------- XBRL 주석(차원 포함) ----------
LAND = re.compile(r"Land(?!AndBuildings)", re.I)
BLDG = re.compile(r"(Building|Structure|Machinery|Equipment|Vehicle|Tool|Furniture|Fixture)", re.I)
MAT = {"NotLaterThanOneYearMember": 1, "LaterThanOneYearAndNotLaterThanTwoYearsMember": 2,
       "LaterThanTwoYearsAndNotLaterThanThreeYearsMember": 3}
DEBT_CONCEPT = re.compile(r"(Borrowing|Bond|Debenture)", re.I)
STFIN = re.compile(r"(ShortTermFinancialInstrument|ShorttermDepositsNotClassifiedAsCashEquivalents|ShortTermDeposit)", re.I)
CONS_AXIS = "ConsolidatedAndSeparateFinancialStatementsAxis"


def _xbrl_facts(xml):
    from .collectors import dart_xbrl as X
    ctx = X._parse_contexts(xml)
    out = []
    for m in re.finditer(r"<([\w-]+:)?([A-Za-z0-9_]+)\b([^>]*\bcontextRef=\"([^\"]+)\"[^>]*)>([^<]*)</", xml):
        c = ctx.get(m.group(4))
        v = X._num(m.group(5))
        if not c or v is None:
            continue
        dims = dict(c["dims"])
        cons = dims.pop(CONS_AXIS, None)
        if cons is not None and "Consolidated" not in cons:
            continue                      # 별도 값 제외
        out.append({"concept": m.group(2), "dims": dims, "start": c["start"], "end": c["end"], "instant": c["instant"], "v": v})
    return out


def notes_from_xml(xml):
    """{연도: {'land','bldg','interest','mat1','mat2','mat3','stfin','lease'}} (FY 말·연간 값만)"""
    import datetime as _dt
    res = {}

    def slot(f):
        if f["instant"]:
            d = _dt.date.fromisoformat(f["instant"])
            return d.year if d.month == 12 else None
        if f["start"] and f["end"]:
            s, e = _dt.date.fromisoformat(f["start"]), _dt.date.fromisoformat(f["end"])
            return e.year if s.month == 1 and s.day == 1 and e.month == 12 else None
        return None
    for f in _xbrl_facts(xml):
        y = slot(f)
        if y is None:
            continue
        r = res.setdefault(y, {})
        dims, c = f["dims"], f["concept"]
        dimset = set(dims)
        # 유형자산 종류별 장부금액
        if c in ("PropertyPlantAndEquipment",) and dimset and dimset <= {"ClassesOfPropertyPlantAndEquipmentAxis",
                                                                         "CarryingAmountAccumulatedDepreciationAmortisationAndImpairmentAndGrossCarryingAmountAxis"}:
            if dims.get("CarryingAmountAccumulatedDepreciationAmortisationAndImpairmentAndGrossCarryingAmountAxis", "CarryingAmountMember") != "CarryingAmountMember":
                continue
            mem = dims.get("ClassesOfPropertyPlantAndEquipmentAxis", "")
            if LAND.search(mem):
                r["land"] = r.get("land", 0) + f["v"]
            elif BLDG.search(mem):
                r["bldg"] = r.get("bldg", 0) + f["v"]
            continue
        if not dims:
            if c == "Land":
                r.setdefault("land_direct", f["v"])
            elif c in ("Buildings", "Machinery", "Structures", "OfficeEquipment", "Vehicles"):
                r["bldg_direct"] = r.get("bldg_direct", 0) + f["v"]
            elif c in ("InterestExpense", "InterestExpenseOnBorrowings", "InterestExpenseOnFinancialLiabilitiesMeasuredAtAmortisedCost"):
                r.setdefault("interest", abs(f["v"]))
            elif STFIN.search(c) and f["instant"]:
                r["stfin"] = r.get("stfin", 0) + f["v"]
            elif c == "LeaseLiabilities" and f["instant"]:
                r.setdefault("lease", f["v"])
            continue
        # 차입금 만기 분석(1·2·3년차)
        if "MaturityAxis" in dims and DEBT_CONCEPT.search(c) and dimset <= {"MaturityAxis", "ClassesOfFinancialLiabilitiesAxis",
                                                                            "TypesOfFinancialLiabilitiesAxis"}:
            n = MAT.get(dims["MaturityAxis"])
            if n:
                r[f"mat{n}"] = r.get(f"mat{n}", 0) + f["v"]
    for y, r in res.items():                 # 차원 없는 개별 개념은 종류별 값이 없을 때만 사용
        if "land" not in r and "land_direct" in r:
            r["land"] = r["land_direct"]
        if "bldg" not in r and "bldg_direct" in r:
            r["bldg"] = r["bldg_direct"]
        r.pop("land_direct", None); r.pop("bldg_direct", None)
    return res


def notes_by_year(code):
    out = {}
    for p in sorted(glob.glob(os.path.join(config.DATA_DIR, "raw", code, "xbrl_*.zip"))):
        try:
            z = zipfile.ZipFile(p)
            for n in z.namelist():
                if n.lower().endswith(".xbrl"):
                    for y, r in notes_from_xml(z.read(n).decode("utf-8", "replace")).items():
                        out.setdefault(y, {}).update({k: v for k, v in r.items() if k not in out.get(y, {})})
        except (OSError, zipfile.BadZipFile, ValueError):
            continue
    return out


def collect(conn, code, stats=None, has_key=False):
    """최근 사업보고서 3개의 XBRL 원본이 없으면 받아 둔다(주석: 토지·건물, 이자비용, 만기). 7일 안에 실패한 건 다시 안 함."""
    if not has_key:
        return
    from . import pipeline
    from .collectors import dart_xbrl
    import datetime as _dt
    for y in range(_dt.date.today().year - 3, _dt.date.today().year):
        rc = pipeline._report_rcept(conn, code, y, "11011")
        if not rc or os.path.exists(dart_xbrl.path_for(code, rc)):
            continue
        key = f"{code}:{rc}"
        if db.fetched_recently(conn, "value5_xbrl", key, 7):
            continue
        try:
            if stats:
                stats.req("OpenDART")
            dart_xbrl.fetch(code, rc, "11011")
            db.log_fetch(conn, "value5_xbrl", key, code, True, 1)
        except Exception as e:      # noqa: BLE001  주석은 보조 자료라 실패해도 분석을 멈추지 않음
            db.log_fetch(conn, "value5_xbrl", key, code, False, 0, str(e)[:200])


# ---------- 계산 입력 만들기 ----------
def _g():
    from .valuation import load_assumptions
    return load_assumptions().get("영구성장률_g", 0.0)


def _rf_table():
    from .valuation import load_assumptions
    from . import rates
    return {k: v / 100 for k, v in rates.table(load_assumptions())[0].items()}


def inputs(conn, code, P, vrows=None):
    cy = P["current"]
    f = {(r["fiscal_year"], r["period"], r["metric"]): r["value"] for r in
         conn.execute("SELECT fiscal_year, period, metric, value FROM facts WHERE stock_code=? AND is_current=1", (code,))}
    v = lambda y, k, per="FY": f.get((y, per, k))
    face, notes = face_by_year(code), notes_by_year(code)
    rf = _rf_table()
    wacc = {r["year"]: r.get("wacc") for r in (vrows or [])}
    actual = sorted({y for (y, per, k) in f if per == "FY" and k == "acc_op" and y < cy})
    years = {}
    for y in actual:
        fa, no = face.get(y, {}), notes.get(y, {})
        sc, sp = v(y, "shares_common"), v(y, "shares_pref")
        tc, tp = v(y, "treasury_common"), v(y, "treasury_pref")
        shares = (sc + (sp or 0) - (tc or 0) - (tp or 0)) if sc else None
        da = v(y, "acc_da_total")
        if da is None and v(y, "acc_dep") is not None:
            da = v(y, "acc_dep") + (v(y, "acc_amort") or 0) + (v(y, "acc_rou_dep") or 0)
        lease_face = v(y, "acc_lease_face") or 0
        lease_x = no.get("lease") or v(y, "acc_lease_xbrl")
        lease_add = (lease_x or 0) if not lease_face else 0     # 본문에 리스부채가 없으면 주석 값을 순차입금에 더함
        stfin_add = no.get("stfin") or 0 if (v(y, "acc_cash") or 0) == (v(y, "acc_cash_only") or 0) else 0
        debt, cash = v(y, "acc_debt"), v(y, "acc_cash")
        nd = None if debt is None else debt + lease_add - (cash or 0) - stfin_add
        wc = None if None in (fa.get("recv"), fa.get("inv"), fa.get("pay")) else fa["recv"] + fa["inv"] - fa["pay"]
        interest_x = no.get("interest")
        interest_cf = (v(y, "acc_int_paid") or 0) + (v(y, "acc_int_paid_fin") or 0) if (v(y, "acc_int_paid") is not None or v(y, "acc_int_paid_fin") is not None) else None
        years[y] = {
            "year": y, "kind": "실적", "op": v(y, "acc_op"), "ni": v(y, "acc_ni_owner"), "eq": v(y, "acc_eq_owner"),
            "liab": v(y, "acc_liab"), "nci": fa.get("nci", 0.0 if fa else None), "shares": shares,
            "shares_note": None if shares else "발행주식수 없음(DART 주식총수 미수집)",
            "da": da, "capex": (v(y, "acc_capex_ppe") or 0) + (v(y, "acc_capex_int") or 0) if v(y, "acc_capex_ppe") is not None else None,
            "wc": wc, "recv": fa.get("recv"), "inv": fa.get("inv"), "pay": fa.get("pay"),
            "inv_note": "재무상태표에 재고자산 줄 없음 → 0" if fa.get("inv_absent") else None,
            "cash_st": None if cash is None else cash + stfin_add, "land": no.get("land"), "bldg": no.get("bldg"),
            "ppe": fa.get("ppe"), "debt": debt, "lease": lease_face or lease_x, "lease_in_nd": bool(lease_face or lease_x),
            "cb": fa.get("cb"), "nd": nd, "div": fa.get("div"),
            "interest": interest_x if interest_x is not None else interest_cf,
            "interest_src": "XBRL 이자비용" if interest_x is not None else ("현금흐름표 이자 지급" if interest_cf is not None else None),
            "mat1": fa.get("debt_cur"), "mat2": no.get("mat2"), "mat3": no.get("mat3"),
            "rf": rf.get(y) if y in rf else rf.get(max([k for k in rf if k <= y], default=min(rf)) if rf else None),
            "wacc": wacc.get(y),
        }
    # 공통 재료는 가치평가(G)와 같은 값을 쓴다(같은 이름이면 같은 식): 세율·FCFF·주식수·순차입금·비지배지분·WACC·Ke 기준 금리
    G = {r["year"]: r for r in (vrows or [])}
    for y, r in years.items():
        g = G.get(y)
        if not g:
            continue
        r.update(tax=g.get("tax"), fcff=g.get("fcff"), shares=g.get("shares"), nd=g.get("net_debt"), nci=g.get("nci") or 0,
                 wacc=g.get("wacc"), rf=g.get("rf", r.get("rf")), lease=g.get("lease"),
                 shares_note=g.get("shares_note") if g.get("shares_unverified") else (None if g.get("shares") else "주식수 없음"))
    hist = {}
    for g in G.values():
        for k, val in (g.get("fcff_hist") or {}).items():
            hist.setdefault(int(k), val)
    for y, r in years.items():
        if r.get("fcff") is None and y in hist:
            r["fcff"] = hist[y]                 # G 표 시작 이전 연도의 FCFF도 G가 계산한 값 그대로
    # 전망 연도
    from .report import consensus_trend
    cons = {(r["target_year"], r["metric"]): r["value"] for r in db.latest_consensus(conn, code)}
    rev = {(it["year"], it["metric"]): it for it in consensus_trend(conn, code, P).get("summary", [])}
    last = actual[-1] if actual else None
    L = years.get(last, {})
    from . import standards
    Fc = standards.factors(conn, code, P)
    latest = next((G[y] for y in sorted(G, reverse=True) if G[y].get("shares") and G[y].get("kind", "").startswith(("현재", "실적"))), None) or {}
    for y in (cy, cy + 1, cy + 2):
        op_c, ni_c = cons.get((y, "op_income")), cons.get((y, "net_income"))
        if op_c is None and ni_c is None:
            continue
        r_op, r_ni = rev.get((y, "op_income"), {}), rev.get((y, "eps"), {})
        f_op, f_ni = Fc[(y, "op_income")][0], Fc[(y, "net_income")][0]
        g = G.get(y) if (G.get(y) or {}).get("kind", "").startswith("전망") else None
        years[y] = {"year": y, "kind": "전망", "op": None if op_c is None else op_c * 1e8 * f_op,
                    "ni": None if ni_c is None else ni_c * 1e8 * f_ni, "op_raw": op_c, "ni_raw": ni_c, "f_op": f_op, "f_ni": f_ni,
                    "rev3m_op": (r_op.get("ch") or {}).get(3), "rev3m_ni": (r_ni.get("ch") or {}).get(3),
                    "shares": latest.get("shares"), "nci": latest.get("nci") or 0, "nd": latest.get("net_debt"),
                    "nd_when": f"{latest.get('year')} {latest.get('kind', '')[:2]}" if latest else None,
                    "tax": (g or latest).get("tax") if (g or latest) else None,
                    "da": L.get("da"), "div": L.get("div") or 0, "interest": L.get("interest"),
                    "interest_src": L.get("interest_src"), "mat1": L.get("mat1"), "mat2": L.get("mat2"), "mat3": L.get("mat3"),
                    "rf": (g or {}).get("rf") or rf.get(y, rf.get(max(rf)) if rf else None),
                    "wacc": (g or {}).get("wacc") or latest.get("wacc"), "lease": latest.get("lease")}
    price = v(cy, "price_close", "ASOF")
    return {"years": years, "order": sorted(years), "last": last, "price": price, "price_date": None,
            "stress_from": cy - STRESS_YEARS, "stress_to": cy - 1, "g": _g()}


# ---------- 계산(화면 JS와 같은 식) ----------
def stress_op(data):
    ops = [(y, r["op"]) for y, r in data["years"].items()
           if r["kind"] == "실적" and data["stress_from"] <= y <= data["stress_to"] and r["op"] is not None]
    if not ops:
        return None, None, 0
    ops.sort(key=lambda t: t[1])
    k = max(0, math.ceil(STRESS_PCT * len(ops)) - 1)
    return ops[k][1], ops[k][0], len(ops)


def compute(data, params=None):
    p = json.loads(json.dumps(DEFAULTS))
    for k, val in (params or {}).items():
        if k == "rec":
            p["rec"].update(val)
        else:
            p[k] = val
    Y, order, price = data["years"], data["order"], data["price"]
    out = {}
    fcff = {}
    for i, y in enumerate(order):
        r = Y[y]
        if r["kind"] == "실적":
            fcff[y] = r.get("fcff")       # 가치평가(G)와 같은 FCFF: 영업현금흐름 + 영업 이자×(1−t) − 설비투자
        else:
            fcff[y] = None if r["op"] is None or r.get("tax") is None else r["op"] * (1 - r["tax"])   # 설비투자 = 감가상각, 운전자본 증가 0
    eq = {}
    bad, bad_y, bad_n = stress_op(data)
    for y in order:
        r = Y[y]
        res = {"year": y, "kind": r["kind"], "why": {}}
        wacc = p["wacc"] if p["wacc"] is not None else r.get("wacc")
        ke = None if r.get("rf") is None else r["rf"] + p["ke_premium"]
        sh, nd, nci = r.get("shares"), r.get("nd"), r.get("nci") or 0
        # 자본·ROE
        if r["kind"] == "실적":
            eq[y] = r["eq"]
            e0 = Y.get(y - 1, {}).get("eq")
            roe = r["ni"] / ((e0 + r["eq"]) / 2 if e0 else r["eq"]) if r.get("ni") is not None and r.get("eq") else None
        else:
            e0 = eq.get(y - 1)
            if e0 is None or r["ni"] is None:
                eq[y], roe = None, None
            else:
                eq[y] = e0 + r["ni"] - (r.get("div") or 0)
                roe = r["ni"] / ((e0 + eq[y]) / 2)
        res["roe"], res["eq"], res["ke"], res["wacc"] = roe, eq[y], ke, wacc
        # ① DCF
        if r["kind"] == "실적":
            win = [fcff[t] for t in range(y - 4, y + 1) if fcff.get(t) is not None]
            fn = sum(win) / len(win) if len(win) >= 3 else None
            res["fcff_n_years"] = len(win)
        else:
            fn = fcff[y]
        res["fcff"], res["fcff_n"] = fcff.get(y), fn
        if fn is None:
            res["why"]["dcf"] = "FCFF(영업현금흐름 기준)가 3년 미만" if r["kind"] == "실적" else "영업이익 컨센서스 없음"
        elif not wacc or sh is None or nd is None:
            res["why"]["dcf"] = "WACC·주식수·순차입금 중 없음"
        else:
            g = data.get("g", 0.0)
            res["dcf"] = (fn * (1 + g) / (wacc - g) - nd - nci) / sh
        # ② RIM
        if None in (eq[y], roe, ke) or sh is None:
            res["why"]["rim"] = "지배자본·ROE·Ke·주식수 중 없음"
        else:
            w = p["omega"]
            res["rim"] = (eq[y] + (roe - ke) * eq[y] * w / (1 + ke - w)) / sh
            res["rim_w1"] = (eq[y] + (roe - ke) * eq[y] / ke) / sh
        # ③ 자산가치(실적 연도만)
        if r["kind"] != "실적":
            res["why"]["asset"] = "전망 연도 자산 명세 없음"
        elif r.get("land") is None or r.get("bldg") is None:
            res["why"]["asset"] = "토지·건물·설비 구분 없음(XBRL 유형자산 주석 미수집)"
        elif None in (r.get("cash_st"), r.get("recv"), r.get("inv"), r.get("liab")) or sh is None:
            res["why"]["asset"] = "현금·매출채권·재고·부채·주식수 중 없음"
        else:
            rc = p["rec"]
            tot = r["cash_st"] * rc["cash"] + r["recv"] * rc["recv"] + r["inv"] * rc["inv"] + r["land"] * rc["land"] + r["bldg"] * rc["bldg"]
            res["asset"] = (tot - r["liab"] - nci) / sh
        # ④ 스트레스
        if bad is None or not wacc or sh is None or nd is None:
            res["why"]["stress"] = "불황 영업이익·WACC·주식수·순차입금 중 없음"
        else:
            res["stress_raw"] = (bad * (1 - (r.get("tax") or 0.22)) / wacc - nd - nci) / sh
            res["stress"] = res["stress_raw"]
        # ⑤ 재무위험
        if "stress" not in res:
            res["why"]["risk"] = "④를 계산하지 못함"
        elif None in (r.get("interest"), r.get("mat1"), fn, price):
            res["why"]["risk"] = "이자·1년 내 만기·FCFF·주가 중 없음"
        elif r.get("mat2") is None or r.get("mat3") is None:
            res["why"]["risk"] = "2·3년차 만기 자료 없음(XBRL 만기 주석 미수집)"
        else:
            short = max(0.0, 3 * r["interest"] + r["mat1"] + r["mat2"] + r["mat3"] - 3 * fn)
            res["shortfall"] = short
            res["risk"] = 0.0 if res["stress_raw"] <= 0 else res["stress_raw"] * sh / (sh + short / price)
        for k in ("dcf", "rim", "asset", "stress", "risk"):
            if k in res and res[k] is not None and res[k] < 0:
                res[k] = 0.0
                res.setdefault("no_equity", []).append(k)
        out[y] = res
    return {"rows": out, "params": p, "stress_op": bad, "stress_year": bad_y, "stress_n": bad_n}


def payload(conn, code, P, vrows):
    data = inputs(conn, code, P, vrows)
    res = compute(data)
    ser = {str(y): r for y, r in data["years"].items()}
    from .valuation import load_assumptions
    A = load_assumptions()
    dfl = json.loads(json.dumps(DEFAULTS))
    dfl["ke_premium"] = A.get("베타", 1.0) * A["시장위험프리미엄_ERP"]
    return {"inputs": ser, "order": data["order"], "price": data["price"], "last": data["last"], "defaults": dfl,
            "g": data.get("g", 0.0), "stress_from": data["stress_from"], "stress_to": data["stress_to"], "stress_pct": STRESS_PCT,
            "cut": [CUT_NORMAL, CUT_DEEP, DEEP_DROP], "result": {str(y): r for y, r in res["rows"].items()},
            "stress_op": res["stress_op"], "stress_year": res["stress_year"], "stress_n": res["stress_n"],
            "disclaimer": "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"}
