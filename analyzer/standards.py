"""앱 전체가 함께 쓰는 가치평가 재료(같은 이름이면 같은 식). 2026-11-13 사용자 결정: '조사된 정확한 방법으로 통일'.

근거(공식 자료·교과서)
- 순차입금 = 이자 붙는 부채 전부 + 리스부채 − 현금·단기금융 (다모다란: 부채에 리스 포함)
- 지배주주 몫 = 기업가치 − 순차입금 − 비지배지분(장부가) (다모다란: 비지배지분 차감)
- 주식수 = 발행주식(보통+우선) − 자사주 (유통주식수). DART 주식총수가 없으면 지배순이익 ÷ EPS 역산(미검증 표시)
- 세율 = 법정 한계세율(지방소득세 10% 포함), 과세표준 구간은 최근 5년 흑자 세전이익 중앙값으로 근사 (다모다란: 영구 가치·부채 절세에는 한계세율)
- 자기자본비용 Ke = 국고채 10년 + β × ERP (CAPM). β는 회사별 자료가 없어 1.0, ERP 5%(다모다란 2026-01 한국 4.87% 근처)
- 영구성장률 g = 0 (보수적)
- 컨센서스 이익 보정 = × 0.83, 그 해 컨센서스가 3개월 사이 5% 넘게 깎였으면 × 0.73 (사용자 규칙; 매출은 보정 안 함)
"""
import statistics

# 법인세 과세표준 구간(원)과 국세 세율. 지방소득세는 국세의 10%를 더한다.
_BRACKETS = [
    (2018, None, [(2e8, 0.10), (200e8, 0.20), (float("inf"), 0.22)]),                                  # ~2017
    (2023, 2018, [(2e8, 0.10), (200e8, 0.20), (3000e8, 0.22), (float("inf"), 0.25)]),                  # 2018~2022
    (2026, 2023, [(2e8, 0.09), (200e8, 0.19), (3000e8, 0.21), (float("inf"), 0.24)]),                  # 2023~2025
    (None, 2026, [(2e8, 0.10), (200e8, 0.20), (3000e8, 0.22), (float("inf"), 0.25)]),                  # 2026~ (전 구간 1%p 환원)
]
LOCAL = 1.10
CUT_NORMAL, CUT_DEEP, DEEP_DROP = 0.83, 0.73, -5.0


def _table(year):
    for until, since, br in _BRACKETS:
        if (since is None or year >= since) and (until is None or year < until):
            return br
    return _BRACKETS[-1][2]


def statutory_marginal(year, taxable):
    """그 해 과세표준(원)이 걸리는 구간의 한계세율(지방세 포함)"""
    for top, rate in _table(year):
        if taxable <= top:
            return rate * LOCAL
    return _table(year)[-1][1] * LOCAL


def marginal_tax(year, pretax, pretax_hist=()):
    """법정 한계세율(지방세 포함). 과세표준은 '정상 이익 수준' = 그 해와 직전 4년 흑자 세전이익의 중앙값으로 근사
    (영구 가치에 쓰는 세율이므로 한 해의 일시적 이익 증감으로 구간이 바뀌지 않게 함). 흑자 기록이 없으면 2억~200억 구간."""
    pos = [p for p in [pretax] + list(pretax_hist)[:4] if p and p > 0]
    if pos:
        return statutory_marginal(year, statistics.median(pos)), None
    return statutory_marginal(year, 100e8), "흑자 기록 없음 → 2억~200억 구간 세율"


def ke(A, rf):
    return rf + A.get("베타", 1.0) * A["시장위험프리미엄_ERP"]


def cons_factor(trend_item, metric):
    """trend_item: report.consensus_trend()의 그 연도·지표 요약(없으면 None). 반환: (계수, 이유)"""
    if metric == "revenue":
        return 1.0, "매출은 보정 안 함"
    ch = ((trend_item or {}).get("ch") or {}).get(3)
    tag = ((trend_item or {}).get("tag") or {}).get(3)
    if (ch is not None and ch < DEEP_DROP) or tag in ("적자전환", "적자확대"):
        return CUT_DEEP, f"3개월 사이 컨센서스 {'' if ch is None else f'{ch:+.1f}% '}하향 → ×{CUT_DEEP}"
    return CUT_NORMAL, f"×{CUT_NORMAL}"


def factors(conn, code, P):
    """{(연도, 지표): (계수, 이유)}  지표: op_income(영업이익·이익률), eps(EPS·순이익·ROE), revenue"""
    from .report import consensus_trend
    tr = {(it["year"], it["metric"]): it for it in consensus_trend(conn, code, P).get("summary", [])}
    out = {}
    for y in [P["current"]] + list(P["future"]):
        for m in ("op_income", "eps", "revenue"):
            out[(y, m)] = cons_factor(tr.get((y, m)), m)
        out[(y, "net_income")] = out[(y, "roe")] = out[(y, "eps")]
    return out


def shares(get, y, per="FY"):
    """get(y, metric, per) → 값. 반환: (주식수, 출처 설명, 미검증?)"""
    sc = get(y, "shares_common", per) if per != "FY" else get(y, "shares_common")
    if sc:
        sp = get(y, "shares_pref") or 0
        tc, tp = get(y, "treasury_common") or 0, get(y, "treasury_pref") or 0
        return sc + sp - tc - tp, "발행주식 − 자사주(DART)", False
    d = get(y, "shares_derived")
    if d:
        return d, "지배순이익 ÷ EPS 역산(DART 주식총수 없음)", True
    return None, "주식수 없음", True


def net_debt(get, y, per="FY", notes_y=None):
    """반환: dict(debt, cash, nd, lease, lease_added, stfin_added). 리스는 본문에 없으면 XBRL 주석 값을 더한다."""
    debt, cash = get(y, "acc_debt", per), get(y, "acc_cash", per)
    if debt is None:
        return {"debt": None, "cash": cash, "nd": None, "lease": None, "lease_added": 0, "stfin_added": 0}
    no = notes_y or {}
    lease_face = get(y, "acc_lease_face", per) or 0
    lease_x = no.get("lease") or get(y, "acc_lease_xbrl", per)
    lease_add = (lease_x or 0) if not lease_face else 0
    stfin_add = (no.get("stfin") or 0) if (cash or 0) == (get(y, "acc_cash_only", per) or 0) else 0
    d, c = debt + lease_add, (cash or 0) + stfin_add
    return {"debt": d, "cash": c, "nd": d - c, "lease": lease_face or lease_x, "lease_added": lease_add,
            "stfin_added": stfin_add}
