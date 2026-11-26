"""업종 안 상대 순위(2026-11-26) — 같은 업종(DART 업종코드, 한국표준산업분류) 회사들 사이에서 이 회사가 어디쯤인가

- 비교 대상: 시장 전체가 아니라 **이 앱 DB에 있는 회사** 중 같은 업종. 같은 업종이 5곳 미만이면 한 단계 넓은 업종으로
  (3자리 → 2자리(중분류) → 대분류 문자). 그래도 5곳 미만이면 '비교 불가'.
- 지표(가장 최근 실적 연도): ROE·ROA·영업이익률·매출 성장률(높을수록 위) / 부채비율·PER·PBR(낮을수록 위).
  PER·PBR = 기준일 주가 ÷ 최근 결산 EPS·BPS(적자 EPS는 PER 빼고 순위).
- 계산은 모든 회사를 한 번에(화면 자료 저장본으로 재사용).
"""
from . import db

MIN_PEERS = 5
METRICS = [("roe", "ROE", "%", True), ("roa", "ROA", "%", True), ("op_margin", "영업이익률", "%", True),
           ("rev_growth", "매출 성장률", "%", True), ("debt_ratio", "부채비율", "%", False), ("per", "PER", "배", False), ("pbr", "PBR", "배", False)]
SECTION_NAME = {"A": "농림어업", "B": "광업", "C": "제조업", "D": "전기·가스", "E": "수도·폐기물", "F": "건설", "G": "도소매",
                "H": "운수·창고", "I": "숙박·음식", "J": "정보통신", "K": "금융·보험", "L": "부동산", "M": "전문·과학·기술",
                "N": "사업지원", "O": "공공행정", "P": "교육", "Q": "보건·복지", "R": "예술·여가", "S": "협회·수리·개인서비스"}
KSIC2 = {1: "농업", 2: "임업", 3: "어업", 5: "석탄·원유·천연가스 광업", 6: "금속 광업", 7: "비금속광물 광업", 8: "광업 지원",
         10: "식료품", 11: "음료", 12: "담배", 13: "섬유제품", 14: "의복·모피", 15: "가죽·가방·신발", 16: "목재·나무제품",
         17: "펄프·종이", 18: "인쇄·기록매체", 19: "석유정제품", 20: "화학물질·화학제품", 21: "의약품", 22: "고무·플라스틱",
         23: "비금속 광물제품", 24: "1차 금속", 25: "금속가공제품", 26: "전자부품·컴퓨터·통신장비", 27: "의료·정밀·광학기기",
         28: "전기장비", 29: "기타 기계·장비", 30: "자동차·트레일러", 31: "기타 운송장비", 32: "가구", 33: "기타 제품",
         34: "산업용 기계 수리", 35: "전기·가스·증기", 36: "수도", 37: "하수·폐수", 38: "폐기물·원료 재생", 39: "환경 정화",
         41: "종합 건설", 42: "전문 공사", 45: "자동차 판매", 46: "도매·상품 중개", 47: "소매", 49: "육상 운송", 50: "수상 운송",
         51: "항공 운송", 52: "창고·운송 서비스", 55: "숙박", 56: "음식점·주점", 58: "출판(소프트웨어 포함)", 59: "영상·오디오 제작·배급",
         60: "방송", 61: "우편·통신", 62: "컴퓨터 프로그래밍·시스템 통합", 63: "정보서비스", 64: "금융", 65: "보험·연금",
         66: "금융·보험 서비스", 68: "부동산", 70: "연구개발", 71: "전문 서비스", 72: "건축기술·엔지니어링", 73: "기타 전문·과학·기술",
         74: "사업시설 관리", 75: "사업 지원 서비스", 76: "임대", 85: "교육", 86: "보건", 87: "사회복지", 90: "창작·예술·여가",
         91: "스포츠·오락", 94: "협회·단체", 95: "수리", 96: "기타 개인 서비스"}


def _section(two):
    from .benchmark import SECTIONS
    for (a, b), letter in SECTIONS:
        if a <= two <= b:
            return letter
    return None


def _levels(code):
    """업종코드 → 좁은 것부터 [(묶음 열쇠, 이름)]"""
    s = str(code or "").strip()
    if len(s) < 2 or not s[:2].isdigit():
        return []
    two = int(s[:2])
    out = []
    if len(s) >= 3 and s[:3].isdigit():
        out.append((f"3:{s[:3]}", f"{KSIC2.get(two, '업종 ' + s[:2])} 세분류({s[:3]})"))
    out.append((f"2:{s[:2]}", f"{KSIC2.get(two, '업종 ' + s[:2])}({s[:2]})"))
    sec = _section(two)
    if sec:
        out.append((f"1:{sec}", f"{SECTION_NAME.get(sec, sec)} 전체({sec})"))
    return out


def _values(conn, code, P):
    cy = P["current"]
    v = lambda y, k, per="FY": (lambda r: None if r is None else r["value"])(db.current_fact(conn, code, y, k, per))
    y = next((yy for yy in range(cy - 1, cy - 4, -1) if v(yy, "roe") is not None or v(yy, "op_margin") is not None), None)
    if y is None:
        return None, None
    price = v(cy, "price_close", "ASOF")
    eps, bps = v(y, "eps"), v(y, "bps")
    r0, r1 = v(y, "acc_revenue"), v(y - 1, "acc_revenue")
    return y, {"roe": v(y, "roe"), "roa": v(y, "roa"), "op_margin": v(y, "op_margin"),
               "rev_growth": (r0 / r1 - 1) * 100 if r0 is not None and r1 and r1 > 0 else None,
               "debt_ratio": v(y, "debt_ratio"),
               "per": price / eps if price and eps and eps > 0 else None,
               "pbr": price / bps if price and bps and bps > 0 else None}


def compute(conn, P):
    """반환: {코드: {'group','n','year','metrics': {지표: {v, rank, n, median, top_pct, better}}}}"""
    rows = {}
    for r in conn.execute("SELECT c.stock_code, c.induty_code FROM companies c WHERE EXISTS "
                          "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code)"):
        y, vals = _values(conn, r["stock_code"], P)
        if vals:
            rows[r["stock_code"]] = {"lv": _levels(r["induty_code"]), "y": y, "vals": vals}
    members = {}
    for code, x in rows.items():
        for key, _ in x["lv"]:
            members.setdefault(key, []).append(code)
    out = {}
    for code, x in rows.items():
        pick = next(((k, nm) for k, nm in x["lv"] if len(members.get(k, [])) >= MIN_PEERS), None)
        if not pick:
            out[code] = {"group": None, "n": len(members.get(x["lv"][-1][0], [])) if x["lv"] else 0, "year": x["y"],
                         "why": "업종코드 없음" if not x["lv"] else f"같은 업종이 DB에 {MIN_PEERS}곳 미만(가장 넓은 묶음도)"}
            continue
        key, name = pick
        peers = members[key]
        ms = {}
        for m, label, unit, high_good in METRICS:
            mine = x["vals"][m]
            vs = sorted((rows[p]["vals"][m] for p in peers if rows[p]["vals"][m] is not None), reverse=high_good)
            if mine is None or len(vs) < MIN_PEERS:
                ms[m] = {"v": mine, "rank": None, "n": len(vs)}
                continue
            rank = 1 + sum(1 for z in vs if (z > mine if high_good else z < mine))
            mid = vs[len(vs) // 2] if len(vs) % 2 else (vs[len(vs) // 2 - 1] + vs[len(vs) // 2]) / 2
            ms[m] = {"v": mine, "rank": rank, "n": len(vs), "median": mid, "top_pct": round(rank / len(vs) * 100),
                     "better": "높을수록 위" if high_good else "낮을수록 위"}
        out[code] = {"group": name, "n": len(peers), "year": x["y"], "metrics": ms}
    return {"companies": out, "metrics": [[m, l, u, h] for m, l, u, h in METRICS], "min_peers": MIN_PEERS,
            "note": f"이 앱 DB에 있는 같은 업종 회사끼리 비교(시장 전체 아님). 같은 업종이 {MIN_PEERS}곳 미만이면 한 단계 넓은 업종."}
