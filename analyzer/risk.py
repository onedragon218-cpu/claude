"""상장 유지 위험 점검(종목 찾기용). 근거는 DART에 올라온 거래소 공시와 DART 주요계정뿐이다.

제외(EXCLUDE) — 최근 12개월 거래소 공시 기준, 이후 해제 공시가 있으면 풀린 것으로 본다
  관리종목          '관리종목지정'(해제 공시가 뒤에 없을 때)
  투자주의환기종목   '투자주의환기종목지정'(해제 공시가 뒤에 없을 때)
  상장폐지·실질심사 '상장폐지', '상장적격성 실질심사', '기업심사위원회'(뒤에 '상장유지'가 없을 때)
  거래정지          가장 최근의 '매매거래정지' 관련 공시가 해제가 아닐 때
  감사의견 비적정    공시 제목에 감사의견 '거절'·'부적정'·'한정'
  회생·파산         '회생절차', '파산'
  자본잠식 50% 이상  (자본금 − 자본총계) ÷ 자본금 ≥ 50% 또는 자본총계 ≤ 0 (최근 보고서, 연결 우선·없으면 별도)
                    한국거래소 상장규정의 관리종목 기준(자본잠식률 50% 이상)을 따른 것
주의(CAUTION) — 목록에 남기되 표시
  관리종목 지정 우려 '관리종목지정우려'
  부분 자본잠식      자본잠식률 0% 초과 50% 미만
"""
import datetime as dt
import re

from .collectors import dart

EXCLUDE = ["관리종목", "투자주의환기종목", "상장폐지·실질심사", "거래정지", "감사의견 비적정", "회생·파산", "자본잠식 50% 이상"]
CAUTION = ["관리종목 지정 우려", "부분 자본잠식"]


def _n(s):
    return re.sub(r"\s", "", s or "")


def from_disclosures(items, today=None):
    """거래소 공시 목록(최근 12개월) → [(사유, 내용, 날짜, 접수번호)]"""
    rows = sorted(items, key=lambda d: (d.get("rcept_dt", ""), d.get("rcept_no", "")))
    state = {}   # 사유 → (내용, 날짜, 접수번호) 또는 None(해제)
    for d in rows:
        nm, date, no = _n(d.get("report_nm")), d.get("rcept_dt"), d.get("rcept_no")
        raw = re.sub(r"\s+", " ", d.get("report_nm", "")).strip()
        if "관리종목지정우려" in nm:
            state["관리종목 지정 우려"] = (raw, date, no)
        elif "관리종목" in nm and "해제" in nm:
            state["관리종목"] = None
            state["감사의견 비적정"] = None     # 감사의견 사유가 해소되어 해제된 경우 함께 풀림
        elif "관리종목지정" in nm:
            state["관리종목"] = (raw, date, no)
        if "투자주의환기종목" in nm:
            state["투자주의환기종목"] = None if "해제" in nm else (raw, date, no)
        if "상장유지" in nm:
            state["상장폐지·실질심사"] = None
            state["감사의견 비적정"] = None
        elif "상장폐지" in nm or "상장적격성실질심사" in nm or "기업심사위원회" in nm:
            state["상장폐지·실질심사"] = (raw, date, no)
        if "매매거래정지" in nm or "거래재개" in nm:
            state["거래정지"] = None if ("해제" in nm or "거래재개" in nm) else (raw, date, no)
        if re.search(r"(감사의견|의견).*(거절|부적정|한정)|의견거절", nm) and "해제" not in nm:
            state["감사의견 비적정"] = (raw, date, no)
        if "회생절차" in nm or "파산" in nm:
            state["회생·파산"] = (raw, date, no)
    return [(k, *v) for k, v in state.items() if v]


def from_accounts(items):
    """주요계정 목록 → (자본잠식률, 자본금, 자본총계, 기준) 또는 None"""
    for fs in ("CFS", "OFS"):
        cap = eq = None
        for it in items:
            if it.get("fs_div") != fs or it.get("sj_div") != "BS":
                continue
            nm = _n(it.get("account_nm"))
            v = dart._num(it.get("thstrm_amount"))
            if nm == "자본금" and v is not None:
                cap = v
            elif nm == "자본총계" and v is not None:
                eq = v
        if cap and eq is not None:
            return ((cap - eq) / cap, cap, eq, "연결" if fs == "CFS" else "별도")
    return None


def assess(corp, today=None):
    """DART 거래소 공시(최근 12개월) + 최근 보고서 주요계정으로 위험을 판정한다. DART 키 필요."""
    today = today or dt.date.today()
    flags = []
    lst = dart.fetch_disclosures(corp, (today - dt.timedelta(days=365)).strftime("%Y%m%d"), pblntf_ty="I", max_pages=5)
    for k, raw, date, no in from_disclosures(lst, today):
        flags.append({"type": k, "detail": raw, "date": date, "rcept_no": no})
    imp = None
    # 최근 보고서부터: 올해 3분기→반기→1분기→작년 사업보고서
    for year, reprt, label in ((today.year, "11014", "3분기"), (today.year, "11012", "반기"), (today.year, "11013", "1분기"),
                               (today.year - 1, "11011", "사업보고서")):
        d = dart.fetch_main_accounts(corp, year, reprt)
        r = from_accounts(d.get("list") or [])
        if r:
            ratio, cap, eq, basis = r
            imp = {"ratio": ratio, "capital": cap, "equity": eq, "basis": basis, "period": f"{year} {label}"}
            break
    if imp:
        if imp["equity"] <= 0 or imp["ratio"] >= 0.5:
            flags.append({"type": "자본잠식 50% 이상", "detail": f"{imp['period']}({imp['basis']}) 자본잠식률 {imp['ratio']:.0%}",
                          "date": None, "rcept_no": None})
        elif imp["ratio"] > 0:
            flags.append({"type": "부분 자본잠식", "detail": f"{imp['period']}({imp['basis']}) 자본잠식률 {imp['ratio']:.0%}",
                          "date": None, "rcept_no": None})
    level = "제외" if any(f["type"] in EXCLUDE for f in flags) else ("주의" if flags else "정상")
    return {"checked": True, "level": level, "flags": flags, "impairment": imp, "checked_at": today.isoformat()}
