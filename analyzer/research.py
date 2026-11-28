"""매수 판단 조사 프롬프트(2026-11-27 사용자 요청) — J·P·Q·U탭 후보 회사를 AI에게 조사시키는 글을 만든다

- 화면 J·P·Q·U의 회사 줄마다 [조사 프롬프트] 버튼 → 이 모듈이 글을 만들고 사용자가 복사해 AI(웹 검색 되는 것)에 붙여 넣는다.
- 1단계: 복사만(AI 답을 앱에 다시 넣는 기능 없음). 앱은 아무것도 바꾸지 않음(DB 읽기만).
- 구성(사용자 승인): 1 목적 · 2 앱이 이미 아는 사실(기준일) · 3 뽑힌 이유(탭별, 화면이 보낸 값) · 4 그 근거가 무너질 조건(탭별)
  · 5 공통 점검 ①~⑬ · 6 업종 추가 질문(업종코드로) · 7 지킬 것 · 8 답 형식. 한장조사(Claude) 결과가 있으면 자동으로 요약해 넣음.
- 한장조사 폴더·Claude 쪽 프로젝트는 건드리지 않는다(앱 DB에 이미 읽어 둔 hj_* 표만 읽음).
리서치 참고자료이며 투자자문이 아님
"""
import datetime as dt

from . import db

VERSION = "조사양식 v2"
TABS = {"J": "J. 성장 조건 분류", "P": "P. 우상향 저평가(ROE 적정가)", "Q": "Q. 적정 PER 주가", "U": "U. 우상향 저평가(10년 최저 PBR 적정가)"}

# 4. 탭별 '이 근거가 무너질 조건' — 먼저 확인할 것
BREAK = {
    "P": ["적정가 = ROE × BPS ÷ 10 × 보정 — 미래 ROE·BPS 컨센서스가 믿을 만한가(추정 증권사 수, 마지막 갱신일, 최근 3~6개월 하향 여부)",
          "올해~내후년 ROE 상승이 일회성(자산 매각·환입·환율 이익)이나 업황 정점에 기대고 있지 않은가",
          "우상향(작년 < 올해 < 내년 < 내후년)이 회사 가이던스·수주·업황 자료로 뒷받침되는가, 증권사 추정만의 낙관인가"],
    "U": ["적정가 = 10년 중 가장 낮았던 연말 PBR × 미래 BPS — 그 최저 PBR을 기록한 해의 상황이 지금과 비슷한가. "
          "업종이 구조적으로 나빠지기 전(또는 일시적 호황기)의 PBR이라면 기준으로 의미가 없음",
          "BPS를 깎을 일이 있는가: 자산 손상차손, 대규모 적자, 배당·자사주 외 자본 감소, 유상증자 희석",
          "우상향(작년 < 올해 < 내년 < 내후년) 컨센서스가 믿을 만한가(추정 증권사 수·갱신일·하향 여부)"],
    "Q": ["적정 PER = (1 − g/ROE) ÷ (Ke − g) — 쓴 ROE가 지속 가능한 수준인가(업황 정점 ROE면 과대평가)",
          "컨센서스 순이익에 일회성이 섞였는가, 지배주주 순이익 기준이 맞는가",
          "요구수익률(Ke)과 성장률(g) 가정이 이 회사 위험(부채·업황 변동·지배구조)에 비해 낮지 않은가"],
    "J": ["성장(매출·영업이익률·EPS 증가)이 수요 증가인가, 가격 상승·원가 하락 같은 일시 요인인가",
          "성장을 만드는 사업부·제품이 무엇이고 그 수요가 몇 년 이어질 근거(수주잔고·계약·증설)가 있는가",
          "성장에 필요한 투자·운전자본 때문에 현금흐름이 나빠지거나 증자가 필요해지지 않는가"],
}

# 5. 공통 점검 ①~⑬ (사용자 승인 목록)
CHECKS = [
    ("① 컨센서스의 질", "추정 증권사 수, 마지막 갱신일, 증권사 간 차이, 회사 가이던스와의 차이"),
    ("② 이익의 질", "일회성 손익, 영업현금흐름 ÷ 순이익, 재고·매출채권 증가, 설비투자 대비 감가상각"),
    ("③ 업황과 사이클 위치", "제품 가격·원가 차이(스프레드), 가동률, 경쟁사 증설·감산, 수요 산업 — 지금이 사이클의 어디인가"),
    ("④ 경쟁력", "점유율, 가격 결정력, 고객 집중도, 대체 위협 — 높은 ROE가 이어질 이유"),
    ("⑤ 자본 배분", "배당 정책, 자사주 매입·소각 여부, 대규모 투자 계획, 인수합병"),
    ("⑥ 지분 희석 위험", "유상증자, 전환사채·신주인수권부사채 잔액과 전환가 조정(리픽싱) 조건, 보호예수 해제 물량"),
    ("⑦ 지배구조", "최대주주 지분과 변동, 승계, 계열사 내부거래·지원, 감사의견, 감사인 교체"),
    ("⑧ 숨은 재무 위험", "차입금 만기 구조, 소송·지급보증 같은 우발채무, 환율 노출"),
    ("⑨ 정책·규제", "업종 구조조정 정책, 환경규제(탄소배출권 등), 관세·무역 — 현재 진행 단계"),
    ("⑩ 시장 측면", "하루 거래대금(유동성), 외국인·기관 보유 추이, 공매도 잔고, 최근 1년 주가 흐름, 기업가치 제고(밸류업) 공시 여부"),
    ("⑪ 저평가 이유 가설", "시장이 왜 이 가격에 두는가 — 가설 3개와 각각의 근거·반박 (가장 중요)"),
    ("⑫ 계기와 일정", "재평가 계기(실적·정책·업황 반등)와 시기, 다음 실적 발표일, 배당 기준일·주주총회"),
    ("⑬ 판단을 바꿀 조건", "무엇이 확인되면 매수 근거가 무너지는가, 앞으로 지켜볼 지표"),
]

# 6. 업종 추가 질문 — 업종코드(한국표준산업분류) 앞 2자리로 고름. 기본틀(①~⑬)은 그대로 두고 덧붙이는 질문
INDUSTRY = [
    ((19, 20), "석유정제·화학", ["주력 제품의 원료 대비 제품 가격 차(예: 나프타 대비 에틸렌·폴리머)가 최근 1~3년 어디쯤인가",
                             "중국·중동 증설이 이 회사 제품 수급에 주는 영향과 시기",
                             "국내 석유화학 구조조정(설비 감축·통합) 논의가 있다면 현재 단계와 이 회사에 미치는 영향",
                             "고부가(스페셜티) 제품 비중과 그 추세"]),
    ((21, 21), "의약품·바이오", ["매출을 받치는 제품·기술수출 계약과 그 만기", "임상·허가 일정과 실패 시 영향",
                              "연구개발비 회계 처리(비용 처리인가 자산화인가)", "자금이 몇 년 버티는가(현금 소진 속도)"]),
    ((26, 26), "반도체·전자부품", ["주요 고객과 매출 비중, 고객사 투자·재고 사이클 위치", "제품 세대 교체에서 이 회사 위치(점유율·인증)",
                                "가격 하락 압력과 원가 구조"]),
    ((27, 27), "의료·정밀기기", ["주력 제품 인허가(국가별)와 보험 수가", "수출 지역과 환율 영향", "경쟁 제품 대비 차별점"]),
    ((28, 28), "전기장비·2차전지", ["수주잔고와 납기, 주요 고객 집중도", "원재료(구리·리튬 등) 가격 전가 구조", "증설 투자 규모와 가동률"]),
    ((29, 29), "기계·장비", ["수주잔고·신규 수주 추이", "전방 산업의 투자 사이클 위치", "애프터서비스·부품 매출 비중"]),
    ((30, 30), "자동차·부품", ["주요 완성차 고객과 비중, 차종별 노출(전기차·하이브리드)", "단가 인하 압력과 원가 전가", "해외 공장·관세 영향"]),
    ((31, 31), "조선·운송장비", ["수주잔고(척수·금액·인도 시기)와 선가 추이", "저가 수주 물량이 실적에서 빠지는 시기", "인력·후판 가격 부담"]),
    ((24, 25), "철강·금속", ["제품 가격과 원료(철광석·고철·비철) 가격 차", "중국 수출 물량과 무역 규제", "전방(건설·자동차·조선) 수요"]),
    ((10, 12), "음식료", ["원재료(곡물·설탕·유지 등) 가격과 판가 인상 시점", "해외 매출 비중과 성장", "유통 채널 변화"]),
    ((17, 17), "펄프·종이", ["펄프 가격과 판가 인상·전가 시점(최근 가격 담합 제재·소송 같은 판가 관련 쟁점 포함)",
                         "인쇄용지 수요 감소 속도와 산업용지·포장재·특수지로의 전환", "수입지 가격과 환율", "설비 가동률·노후 설비 정리 계획"]),
    ((13, 18), "섬유·의복·생활소재", ["원재료(펄프·면화 등) 가격과 판가 전가", "주요 고객(브랜드) 주문 동향", "설비 노후·구조조정 계획"]),
    ((41, 42), "건설", ["미분양·미착공 사업장과 부동산 PF 보증 규모", "수주잔고 구성(주택·플랜트·해외)", "원가율 추이와 추가 원가 위험"]),
    ((45, 47), "유통·도소매", ["점포·채널별 매출 추이와 온라인 전환", "재고 회전과 판촉 비용", "임차료·인건비 부담"]),
    ((49, 52), "운송·물류", ["운임 지수 추이와 계약 구조(장기·스팟)", "연료비 전가", "선박·항공기 등 자산 투자와 부채"]),
    ((58, 63), "소프트웨어·IT·인터넷", ["반복 매출(구독·유지보수) 비중과 이탈률", "주요 고객·플랫폼 의존도", "인건비 증가와 수익성"]),
    ((59, 60), "미디어·콘텐츠", ["작품·아티스트 일정과 흥행 의존도", "판권·플랫폼 계약 구조", "제작비 회계 처리"]),
    ((61, 61), "통신", ["가입자·가입자당 매출 추이", "설비투자 주기", "요금 규제"]),
    ((35, 39), "전기·가스·환경", ["요금 규제와 원가 전가 구조", "설비투자와 차입", "정책 변화(에너지 전환)"]),
    ((64, 66), "금융", ["(은행) 예대마진·연체율·충당금 / (보험) 신계약 마진·해지율 / (증권) 거래대금·부동산 PF 노출",
                    "자본비율과 배당 여력", "금리 변화 민감도"]),
    ((68, 68), "부동산", ["보유 자산 감정가와 장부가 차이", "임대율·임대료 추이", "차입 만기와 금리"]),
    ((70, 75), "전문·과학·사업 서비스", ["주요 고객·계약 기간", "인력 확보와 단가", "정부 사업 의존도"]),
]
GENERIC_INDUSTRY = ["이 업종의 핵심 지표(가격·물량·가동률 중 무엇이 이익을 좌우하는가)와 그 최근 추세", "업종 안 경쟁사 2~3곳과 비교한 수익성·성장"]


def _v(conn, code, y, k, per="FY"):
    r = db.current_fact(conn, code, y, k, per)
    return None if r is None else r["value"]


def _eok(v):
    return None if v is None else v / 1e8


def _n(v, d=0, unit=""):
    if v is None:
        return "–"
    s = f"{v:,.{d}f}"
    return s + unit


def industry(induty_code):
    s = str(induty_code or "").strip()
    if len(s) < 2 or not s[:2].isdigit():
        return None, GENERIC_INDUSTRY
    two = int(s[:2])
    for (a, b), name, qs in INDUSTRY:
        if a <= two <= b:
            return name, qs
    return None, GENERIC_INDUSTRY


def _fr(conn, code, y, k, per="FY"):
    return db.current_fact(conn, code, y, k, per)


def _financials(conn, code, cy):
    """최근 5년 실적 표. 자료 충돌·주식 수 보정·지배순이익 대체 계산은 칸에 표시하고 아래에 이유(2026-11-28)"""
    ys = [y for y in range(cy - 5, cy) if _v(conn, code, y, "acc_revenue") is not None]
    if not ys:
        return ["(DART 실적 없음)"], None
    head = ("| 연도 | 매출(억) | 영업이익(억) | 지배순이익(억) | 당기순이익(억, 비지배 포함) | 영업현금흐름(억) | 설비투자(억) | 감가상각(억) | "
            "영업이익률 | ROE(지배 기준) | ROA(당기순이익 기준) | 부채비율 | EPS(원) | BPS(원) |")
    L, notes = [head, "|" + "---|" * 14], []
    for y in ys:
        g = lambda k: _v(conn, code, y, k)
        dep = (g("acc_dep") or 0) + (g("acc_amort") or 0) if g("acc_dep") is not None else None
        nio_r, eps_r, bps_r, roe_r = _fr(conn, code, y, "acc_ni_owner"), _fr(conn, code, y, "eps"), _fr(conn, code, y, "bps"), _fr(conn, code, y, "roe")
        nio = "–" if nio_r is None or nio_r["value"] is None else _n(_eok(nio_r["value"])) + ("‡" if nio_r["source"] == "derived" else "")
        conflict = any(r is not None and r["value"] is None and (r["status_note"] or "").startswith("자료 충돌") for r in (eps_r, roe_r))
        adj = lambda r: r is not None and r["value"] is not None and "주식 수 보정:" in (r["status_note"] or "")
        eps = "**자료 충돌**" if conflict else _n(g("eps")) + ("†" if adj(eps_r) else "")
        roe = "**자료 충돌**" if conflict else _n(g("roe"), 1, "%")
        bps = _n(g("bps")) + ("†" if adj(bps_r) else "")
        L.append(f"| {y} | {_n(_eok(g('acc_revenue')))} | {_n(_eok(g('acc_op')))} | {nio} | {_n(_eok(g('acc_ni')))} | "
                 f"{_n(_eok(g('acc_ocf')))} | {_n(_eok(g('acc_capex_ppe')))} | {_n(_eok(dep))} | {_n(g('op_margin'), 1, '%')} | {roe} | "
                 f"{_n(g('roa'), 1, '%')} | {_n(g('debt_ratio'), 0, '%')} | {eps} | {bps} |")
        if conflict:
            r = eps_r if eps_r is not None and (eps_r["status_note"] or "").startswith("자료 충돌") else roe_r
            notes.append(f"- {y}년 **자료 충돌**: 지배순이익 {_n(_eok(g('acc_ni_owner')))}억원과 보고서 EPS {_n(g('acc_eps_basic'))}원의 부호가 다름 — "
                         "앱은 이 해의 EPS·ROE를 판정에서 뺐음. DART 원문(연결 손익계산서의 지배기업 소유주지분 순이익·기본주당이익)으로 어느 쪽이 맞는지 확인해 주세요.")
        if nio_r is not None and nio_r["source"] == "derived" and nio_r["value"] is not None:
            notes.append(f"- ‡ {y}년 지배순이익: {nio_r['status_note']}")
        for k, nm in (("acc_revenue", "매출"), ("acc_op", "영업이익"), ("acc_ni_owner", "지배순이익")):
            fr = _fr(conn, code, y, k)
            if fr is not None and "처음 보고한 숫자" in (fr["status_note"] or ""):
                first = fr["status_note"].split("처음 보고한 숫자", 1)[1].split("·")[0].strip()
                try:
                    num, rest = first.split("(", 1)
                    first = f"{float(num.replace(',', '')) / 1e8:,.0f}억({rest}"
                except ValueError:
                    pass
                notes.append(f"- {y}년 {nm}: 표는 나중에 고쳐 실은 숫자, 처음 보고한 숫자는 {first}")
        for nm, r in (("EPS", eps_r), ("BPS", bps_r)):
            if adj(r):
                notes.append(f"- † {y}년 {nm} {r['status_note'].split('주식 수 보정:', 1)[1].strip()[:160]} (지금 주식 수 기준)")
    seen = []
    L += [""] + [x for x in notes if not (x in seen or seen.append(x))] if notes else []
    part = None
    for per, nm in (("Q3", "3분기 누적"), ("H1", "반기 누적"), ("Q1", "1분기")):
        rv, op = _v(conn, code, cy, "acc_revenue", per), _v(conn, code, cy, "acc_op", per)
        if rv is not None:
            pr, pp = _v(conn, code, cy - 1, "acc_revenue", per), _v(conn, code, cy - 1, "acc_op", per)
            gr = f" (전년 같은 기간 대비 매출 {((rv / pr - 1) * 100):+.1f}%)" if pr else ""
            part = f"{cy}년 {nm}: 매출 {_n(_eok(rv))}억 · 영업이익 {_n(_eok(op))}억{gr}" + (f" · 전년 같은 기간 영업이익 {_n(_eok(pp))}억" if pp is not None else "")
            break
    return L, part


def _cons_by_year(conn, code):
    by = {}
    for r in [dict(x) for x in db.latest_consensus(conn, code)]:
        by.setdefault(r["target_year"], {}).setdefault(r["metric"], r)
    return by


def _consensus(conn, code, P):
    cy = P["current"]
    by = _cons_by_year(conn, code)
    ys = [y for y in (cy, cy + 1, cy + 2) if y in by]
    if not ys:
        return ["(증권사 추정치 없음 — 컨센서스가 없는 회사)"]
    try:
        from . import standards
        F = standards.factors(conn, code, P)
    except Exception:          # noqa: BLE001
        F = {}
    L = ["| 해 | 매출(억) | 영업이익(억) [보정] | 순이익(억) [보정] | EPS(원) [보정] | BPS(원) | ROE | 추정 증권사 수 | 기준일 · 출처 |", "|" + "---|" * 9]
    for y in ys:
        m = by[y]
        g = lambda k: (m.get(k) or {}).get("value")

        def wf(k):                         # 이익 숫자 옆에 보정값(×0.83, 3개월 급하향이면 ×0.73 — 컨센 실제 달성률 가운데값). 매출·BPS는 보정 안 함
            v = g(k)
            f = (F.get((y, k)) or (None,))[0]
            return _n(v) if v is None or not f else f"{_n(v)} [{_n(v * f)}]"
        anyr = next(iter(m.values()))
        ns = [x.get("n_estimates") for x in m.values() if x.get("n_estimates") is not None]
        n_est = max(ns) if ns else None
        ntxt = f"{n_est}곳" if n_est and n_est > 1 else (f"{n_est}곳 · 원천 1/미상" if n_est == 1 else "미상 · 원천 1/미상")
        L.append(f"| {y}E | {_n(g('revenue'))} | {wf('op_income')} | {wf('net_income')} | {wf('eps')} | {_n(g('bps'))} | "
                 f"{_n(g('roe'), 1, '%')} | {ntxt} | {anyr['as_of']} · {anyr['provider']} |")
    fs = sorted({(F.get((y, 'eps')) or (None, None))[1] for y in ys} - {None})
    L.append(f"- [보정] = 원래 값 × 앱 보정계수(컨센서스 실제 달성률 가운데값 0.83, 3개월 사이 5% 넘게 하향이면 0.73)" + (f" · {'; '.join(fs)[:200]}" if fs else ""))
    # 변화: 영업이익·EPS 컨센서스가 3·6·12개월 전보다 얼마나 바뀌었나(consensus_history)
    today = dt.date.today()
    ch = []
    for y in (cy, cy + 1):
        for k, nm in (("op_income", "영업이익"), ("eps", "EPS")):
            hist = [dict(r) for r in conn.execute("SELECT as_of, value FROM consensus_history WHERE stock_code=? AND target_year=? AND metric=? "
                                                  "AND value IS NOT NULL ORDER BY as_of", (code, y, k))]
            if len(hist) < 2:
                continue
            now = hist[-1]
            parts = []
            for mth in (3, 6, 12):
                lim = (today - dt.timedelta(days=30 * mth)).isoformat()
                old = next((h for h in reversed(hist) if h["as_of"] <= lim), None)
                if old and old["value"]:
                    parts.append(f"{mth}개월 전 {_n(old['value'])}({(now['value'] / old['value'] - 1) * 100:+.0f}%)" if old["value"] > 0
                                 else f"{mth}개월 전 {_n(old['value'])}")
            if parts:
                ch.append(f"- {y}E {nm}: 지금 {_n(now['value'])}({now['as_of']}) ← " + ", ".join(parts))
    if ch:
        L += ["", "컨센서스 변화(영업이익 억원 · EPS 원):"] + ch
    return L


CF_GAP = 30.0      # 보완 B ②: EPS 3개월 수정률 − 영업이익 3개월 수정률이 이 %p 이상이면 경고


def consensus_flags(conn, code, P, nd=None):
    """컨센서스 일회성 경고(2026-11-28 사용자 결정). 반환: [문장]
    ① 같은 해 컨센 순이익 > 영업이익인데 순차입금 > 0(빚이 있어 이자를 내는 회사가 영업이익보다 많이 번다고 봄) → '영업 밖 이익 전제'
    ② 3개월 EPS 컨센 변화율 − 영업이익 컨센 변화율 ≥ 30%p → 'EPS 상향이 영업에서 오지 않음'(영업이익 기준값이 0 이하면 계산 안 함)"""
    cy = P["current"]
    out = []
    if nd is None:
        try:
            from . import goodco
            nd = goodco.Data(conn, code, P).net_debt()[0]
        except Exception:      # noqa: BLE001
            nd = None
    by = _cons_by_year(conn, code)
    for y in (cy, cy + 1, cy + 2):
        m = by.get(y) or {}
        ni, op = (m.get("net_income") or {}).get("value"), (m.get("op_income") or {}).get("value")
        if ni is not None and op is not None and ni > op and nd is not None and nd > 0:
            out.append(f"영업 밖 이익 전제: {y}E 컨센 순이익 {_n(ni)}억 > 영업이익 {_n(op)}억인데 순차입금 {_n(nd)}억(빚이 있음) — "
                       "자산 매각·평가이익 같은 일회성을 전제했을 수 있음")
    try:
        from .report import consensus_trend
        tr = {(it["year"], it["metric"]): it for it in consensus_trend(conn, code, P).get("summary", [])}
    except Exception:          # noqa: BLE001
        tr = {}
    for y in (cy, cy + 1):
        e, o = tr.get((y, "eps")), tr.get((y, "op_income"))
        if not e or not o:
            continue
        ce, co = e["ch"].get(3), o["ch"].get(3)
        if ce is None or co is None or e["tag"].get(3) or o["tag"].get(3):
            continue
        if ce - co >= CF_GAP:
            out.append(f"EPS 상향이 영업에서 오지 않음: {y}E EPS 컨센 3개월 {ce:+.0f}% vs 영업이익 {co:+.0f}% (차이 {ce - co:.0f}%p ≥ {CF_GAP:.0f}%p)")
    return out


def _survival(conn, code, P):
    """2절 맨 앞 '생존·빚' 칸(2026-11-28 사용자 결정)"""
    from . import goodco
    cy = P["current"]
    D = goodco.Data(conn, code, P)
    last = D.years[-1] if D.years else cy - 1
    L = []
    nd, when = D.net_debt()
    L.append(f"- {goodco.nd_ebitda_text(D, last)}")
    icr, icr_t = goodco.interest_cover(D, last)
    L.append(f"- {icr_t}" + (" — 1배 미만: 영업이익으로 이자를 다 못 냄" if icr is not None and icr < 1 else ""))
    fcf = []
    for y in range(cy - 5, cy):
        o, c = _v(conn, code, y, "acc_ocf"), _v(conn, code, y, "acc_capex_ppe")
        if o is not None and c is not None:
            fcf.append((y, (o - c) / 1e8))
    if fcf:
        L.append(f"- 영업현금흐름 − 설비투자(유형자산 취득) {fcf[0][0]}~{fcf[-1][0]} 합 {_n(sum(v for _, v in fcf))}억 "
                 f"({', '.join(f'{y} {_n(v)}' for y, v in fcf)})" + (f" · {5 - len(fcf)}개 연도 자료 없음" if len(fcf) < 5 else ""))
    else:
        L.append("- 영업현금흐름 − 설비투자: 자료 없음")
    since = (dt.date.today() - dt.timedelta(days=3 * 365)).strftime("%Y%m%d")
    rx = "유상증자|전환사채|신주인수권|교환사채|주식관련사채"
    hits = [f"{str(r['d'])[:4]}-{str(r['d'])[4:6]}-{str(r['d'])[6:8]} {r['t']}" for r in conn.execute(
        "SELECT rcept_dt d, report_nm t FROM disclosures WHERE stock_code=? AND rcept_dt>=? UNION "
        "SELECT rcept_dt d, title t FROM events WHERE stock_code=? AND rcept_dt>=? ORDER BY d DESC", (code, since, code, since))
        if __import__("re").search(rx, r["t"] or "")]
    L.append("- 최근 3년 유상증자·전환사채 등 공시: " + ("; ".join(hits[:6]) if hits else "앱이 받은 공시 목록에는 없음(목록 밖일 수 있으니 확인)"))
    return L, nd


def _remaining(conn, code, P):
    """남은 분기에 필요한 이익: (올해 컨센 영업이익 − 누적 영업이익)과 필요한 남은 분기 이익률"""
    cy = P["current"]
    m = _cons_by_year(conn, code).get(cy) or {}
    cop, crev = (m.get("op_income") or {}).get("value"), (m.get("revenue") or {}).get("value")
    for per, nq, nm in (("Q3", 1, "3분기"), ("H1", 2, "반기"), ("Q1", 3, "1분기")):
        rv, op = _v(conn, code, cy, "acc_revenue", per), _v(conn, code, cy, "acc_op", per)
        if rv is None or op is None:
            continue
        if cop is None or crev is None:
            return f"- 남은 {nq}개 분기에 필요한 이익: 올해 컨센서스가 없어 계산 못 함"
        rv, op = rv / 1e8, op / 1e8
        rem_op, rem_rev = cop - op, crev - rv
        cm = op / rv * 100 if rv else None
        need = rem_op / rem_rev * 100 if rem_rev > 0 else None
        return (f"- 남은 {nq}개 분기에 필요한 이익: 올해 컨센 영업이익 {_n(cop)}억 − {nm} 누적 {_n(op)}억 = **{_n(rem_op)}억**, "
                f"필요한 이익률 {_n(need, 1, '%')} (남은 매출 {_n(rem_rev)}억 기준) vs {nm} 누적 이익률 {_n(cm, 1, '%')}")
    return None


def _segments(conn, code):
    from . import segments
    asof, mix = segments.latest_mix(conn, code)
    if not mix:
        return "(사업부 구성비 자료 없음)"
    return f"{asof} 기준 · " + ", ".join(f"{m['name']} {m['pct']:.1f}%" for m in mix if m.get("pct") is not None) + f" (출처 {mix[0].get('source') or '–'})"


def _disclosures(conn, code, since):
    out = []
    for r in conn.execute("SELECT rcept_dt d, report_nm t, rcept_no n FROM disclosures WHERE stock_code=? AND rcept_dt>=? "
                          "UNION SELECT rcept_dt d, title t, rcept_no n FROM events WHERE stock_code=? AND rcept_dt>=? ORDER BY d DESC LIMIT 25",
                          (code, since.replace("-", ""), code, since.replace("-", ""))):
        d = str(r["d"] or "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
        out.append(f"- {d} {r['t']} (접수번호 {r['n']})")
    return out or ["(최근 6개월 공시 기록 없음 — DART 키가 없거나 아직 받지 않음)"]


def _news(conn, code, since):
    out = [f"- {str(r['published'])[:10]} {r['title']}{(' · ' + r['press']) if r['press'] else ''}"
           for r in conn.execute("SELECT published, title, press FROM news WHERE stock_code=? AND published>=? ORDER BY published DESC LIMIT 20",
                                 (code, since))]
    return out or ["(최근 3개월 뉴스 기록 없음)"]


def _validation(conn, code):
    r = conn.execute("SELECT MAX(run_at) t FROM validation WHERE stock_code=?", (code,)).fetchone()
    if not r or not r["t"]:
        return []
    out = []
    for x in conn.execute("SELECT level, check_name, message FROM validation WHERE stock_code=? AND run_at=? AND level IN ('WARN','FAIL')", (code, r["t"])):
        line = f"- [{x['level']}] {x['check_name']}: {x['message']}"[:220]
        if line not in out:
            out.append(line)
    return out[:8]


def _good6(conn, code, P):
    try:
        from . import goodco
        g = goodco.good6(conn, code, P)
    except Exception:          # noqa: BLE001
        return None
    names = g.get("names") or {}
    L = [f"- {str(names.get(k, k)).split('(')[0].strip()}: {v.get('mark', '')}" for k, v in (g.get("rows") or {}).items()]
    return [f"앱 '좋은 기업 7조건' 점수 {g.get('score', '–')}/{g.get('n_conditions', '–')}"] + L


def _peer(conn, code, P):
    try:
        from . import peerrank, pcache
        pr = pcache.get("업종 안 순위(전체)", P["base_year"], lambda: peerrank.compute(conn, P), pcache.signature(conn))
        x = (pr or {}).get("companies", {}).get(code)
    except Exception:          # noqa: BLE001
        return None
    if not x or not x.get("group"):
        return None
    lab = {m: (l, u) for m, l, u, _ in pr["metrics"]}
    parts = [f"{lab[m][0]} {r['rank']}위/{r['n']}" for m, r in (x.get("metrics") or {}).items() if r.get("rank")]
    return f"{x['group']} · 앱 DB 안 {x['n']}곳 중({x['year']}년 실적, 시장 전체 아님): " + ", ".join(parts)


def _hanjang(conn, code):
    """Claude 한장조사 결과(앱이 읽어 둔 것)를 짧게. 없으면 None"""
    try:
        from . import hanjang
        hanjang.ensure(conn)
        d = hanjang.load(conn, code, "claude")
    except Exception:          # noqa: BLE001
        return None
    if not d:
        return None
    c = d["company"]
    age = None
    try:
        age = (dt.date.today() - dt.date.fromisoformat(str(c.get("asof"))[:10])).days
    except (TypeError, ValueError):
        pass
    L = [f"조사일 {c.get('asof') or '–'}" + (f"({age}일 전)" if age is not None else "")]
    for n in d["nodes"][:6]:
        bits = [f"{n.get('kind') or '사업부'}"]
        if n.get("share_pct") is not None:
            bits.append(f"매출 {n['share_pct']:.0f}%")
        if n.get("rep"):
            bits.append(f"판정 {n['rep']}")
        if n.get("land"):
            bits.append(n["land"])
        line = f"- {n.get('name')}({', '.join(bits)})"
        if n.get("triggers"):
            line += f" · 판정을 바꿀 것: {str(n['triggers'])[:120]}"
        L.append(line)
    if d.get("verdict"):
        L.append(f"- 총평: {str(d['verdict'])[:400]}")
    return L


def build(conn, code, P, tab="P", why="", today=None):
    """조사 프롬프트 글(마크다운). why = 화면이 보낸 '이 탭에서 뽑힌 이유' 한두 줄(화면 설정에 따라 달라서 화면이 만든다)"""
    today = today or dt.date.today()
    tab = tab if tab in TABS else "P"
    c = conn.execute("SELECT name, induty_code FROM companies WHERE stock_code=?", (code,)).fetchone()
    if not c:
        raise ValueError(f"DB에 없는 회사: {code}")
    name, ind = c["name"] or code, c["induty_code"]
    cy = P["current"]
    from . import peerrank
    lv = peerrank._levels(ind)
    ind_label = lv[1][1] if len(lv) > 1 else (lv[0][1] if lv else "업종코드 없음")
    ind_name, ind_qs = industry(ind)
    price = _v(conn, code, cy, "price_close", "ASOF")
    pr_row = db.current_fact(conn, code, cy, "price_close", "ASOF")
    mcap = _v(conn, code, cy, "market_cap", "ASOF")
    fin, part = _financials(conn, code, cy)
    s6 = (today - dt.timedelta(days=183)).isoformat()
    s3 = (today - dt.timedelta(days=92)).isoformat()
    why = " ".join(str(why or "").split())[:1500]
    surv, nd = _survival(conn, code, P)
    flags = consensus_flags(conn, code, P, nd)
    rem = _remaining(conn, code, P)

    L = [f"# 매수 판단 조사 요청 — {name}({code})", "",
         f"요청 번호: {tab}-{code}-{today.isoformat()} · {VERSION} · 만든 곳: 재무분석 앱 화면 {TABS[tab]}", "",
         "## 1. 목적",
         f"{name}을(를) 매수할지 판단할 재료를 조사해 주세요. 이 회사는 제 분석 앱의 숫자 규칙으로 뽑힌 후보입니다. "
         "회사 소개보다 **앱이 이 회사를 고른 근거가 맞는지 검증하고, 반대 근거를 먼저 찾는 것**이 목적입니다. "
         "매수·매도 추천이 아니라 판단 재료를 원합니다.", "",
         "## 2. 앱이 이미 아는 사실 (다시 찾지 않아도 됨 · 다르면 알려 주세요)",
         "### 생존·빚 (먼저 확인)"] + surv + [
         "", f"- 업종: {ind_label} (업종코드 {ind or '–'})",
         f"- 사업부 구성: {_segments(conn, code)}", "",
         "### 최근 실적 (DART 사업보고서, 연결 기준 우선, 나중에 고쳐 실은 숫자 · EPS·BPS는 지금 주식 수 기준, 금액 억원)"] + fin + ([f"- {part}"] if part else []) + ([rem] if rem else []) + [
         "", "### 컨센서스 (증권사 평균, 원래 값 · [ ] 안은 앱 보정값)"] + _consensus(conn, code, P)
    if flags:
        L += ["", "### 컨센서스 경고"] + [f"- ⚠ {x}" for x in flags]
    pe = _peer(conn, code, P)
    if pe:
        L += ["", f"### 업종 안 위치", f"- {pe}"]
    g6 = _good6(conn, code, P)
    if g6:
        L += ["", "### 앱 판정 요약"] + [f"- {g6[0]}"] + g6[1:]
    va = _validation(conn, code)
    if va:
        L += ["", "### 앱 검증에서 걸린 항목 (DART·컨센서스 숫자 대조 경고)"] + va
    L += ["", "### 최근 6개월 공시 (제목)"] + _disclosures(conn, code, s6)
    L += ["", "### 최근 3개월 뉴스 (제목)"] + _news(conn, code, s3)
    hj = _hanjang(conn, code)
    if hj:
        L += ["", "### 이전 조사(한장조사) 요약 — 이미 조사된 내용이니 반복하지 말고, 그 뒤 바뀐 것만 확인해 주세요"] + [f"- {hj[0]}"] + hj[1:]
    L += ["", "### 주가 (숫자를 먼저 보고 가격은 마지막에)",
          f"- 주가: {_n(price)}원" + (f" ({str(pr_row['as_of'] or pr_row['collected_at'] or '')[:10]} 기준)" if pr_row else "") +
          (f" · 시가총액 {_n(mcap)}억원" if mcap else "")]
    L += ["", "## 3. 이 회사가 뽑힌 이유", f"- 화면 {TABS[tab]}: {why or '(화면에서 이유를 받지 못함)'}", "",
          "## 4. 먼저 확인할 것 — 위 근거가 무너질 조건"] + [f"- {x}" for x in BREAK[tab]] + [
          "", "## 5. 공통 점검 (항목마다 확인)"] + [f"- {a}: {b}" for a, b in CHECKS] + [
          "", f"## 6. 업종 추가 질문 ({ind_name or ind_label})"] + [f"- {q}" for q in ind_qs] + [
          "", "## 7. 지킬 것",
          "- 모든 사실에 출처 링크와 날짜를 붙여 주세요. 공시는 DART 접수번호나 링크로.",
          "- 확인하지 못한 것은 추측하지 말고 \"확인 못함\"이라고 적어 주세요.",
          "- 위 2번의 앱 숫자와 다른 값을 찾으면 둘을 나란히 적고 어느 쪽이 맞는지 근거를 주세요.",
          "- 사실과 의견(전망)을 구분해 주세요. 최신 자료(특히 정책·규제·주가)는 검색해서 기준일을 적어 주세요.",
          "", "## 8. 답 형식",
          "1) 4번·5번·6번 항목마다: **확인한 사실 / 출처·날짜 / 앱 판단에 미치는 영향(올림·내림·없음) / 확신(높음·중간·낮음)**",
          "2) 저평가 이유 가설 3개 (각각 근거와 반박)",
          "3) 매수 근거와 반대 근거 (표로 나란히)",
          "4) 판단을 바꿀 조건과 앞으로 지켜볼 지표",
          "5) 다음 확인 일정 (실적 발표·배당 기준일·주주총회 등, 날짜)",
          "6) 결론: \"검토 계속 / 보류 / 제외\" 중 하나와 그 이유 세 줄",
          "", f"(이 글은 재무분석 앱이 만든 조사 요청입니다 · 숫자 기준일은 각 줄에 적힘 · 리서치 참고자료이며 투자자문이 아님)"]
    return "\n".join(L)
