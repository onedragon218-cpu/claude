"""DART XBRL 재무제표 원본(주석 포함)에서 감가상각비·무형자산상각비·사용권자산상각비·리스부채를 읽는다.

- API: OpenDART '재무제표 원본파일(XBRL)' fnlttXbrl.xml (rcept_no, reprt_code) → ZIP(인스턴스 문서 .xbrl 포함)
- 받은 ZIP은 데이터 폴더 raw/<종목코드>/xbrl_<접수번호>.zip 에 보관하고 다시 받지 않는다.
- 연결 재무제표 값만 쓴다(연결·별도 구분 축의 '연결' 구성원, 다른 차원이 붙은 값은 제외).
  연결 값이 하나도 없는 회사(연결 대상이 없는 회사)는 차원 없는 값 또는 '별도'를 쓴다.
- 한 보고서에 당기·전기 값이 함께 들어 있다(사업보고서: 2개 연도, 반기보고서: 올해·전년 같은 기간 누적).
- 주의: 이 개발 환경에서는 DART 접속이 막혀 실제 파일로는 시험하지 못했다. DART XBRL 구조를 본뜬 시험 파일로만 검증.
"""
import datetime as dt
import io
import os
import re
import urllib.parse
import urllib.request
import zipfile

from .. import config
from . import dart

API = "https://opendart.fss.or.kr/api/fnlttXbrl.xml"

# 개념(요소) 이름의 끝부분으로 찾는다(접두어 ifrs-full / dart / entity… 무관)
DA_TOTAL = ["AdjustmentsForDepreciationAndAmortisationExpense", "DepreciationAndAmortisationExpense"]
DEP = ["AdjustmentsForDepreciationExpense", "DepreciationExpense", "DepreciationPropertyPlantAndEquipment"]
AMORT = ["AdjustmentsForAmortisationExpense", "AmortisationExpense", "AmortisationIntangibleAssetsOtherThanGoodwill"]
ROU = ["DepreciationRightofuseAssets", "AdjustmentsForDepreciationOfRightofuseAssets", "DepreciationOfRightofuseAssets"]
LEASE = ["LeaseLiabilities"]
LEASE_PARTS = ["CurrentLeaseLiabilities", "NoncurrentLeaseLiabilities"]


def _local(tag):
    return tag.split("}")[-1].split(":")[-1]


def _parse_contexts(xml):
    """id → {start, end, instant, dims{axis: member}}"""
    ctx = {}
    for m in re.finditer(r"<(?:\w+:)?context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?context>", xml, re.S):
        cid, body = m.group(1), m.group(2)
        dims = {_local(a): _local(b.strip()) for a, b in
                re.findall(r"<(?:\w+:)?explicitMember[^>]*dimension=\"([^\"]+)\"[^>]*>([^<]+)</", body)}
        if re.search(r"<(?:\w+:)?typedMember", body):
            dims["_typed"] = "1"
        g = lambda t: (re.search(rf"<(?:\w+:)?{t}>\s*([0-9-]+)\s*</", body) or [None, None])[1]
        ctx[cid] = {"start": g("startDate"), "end": g("endDate"), "instant": g("instant"), "dims": dims}
    return ctx


def _scope(dims):
    """'C' 연결, 'S' 별도, 'N' 차원 없음, None 다른 차원(사용 안 함)"""
    if not dims:
        return "N"
    if set(dims) == {"ConsolidatedAndSeparateFinancialStatementsAxis"}:
        mem = dims["ConsolidatedAndSeparateFinancialStatementsAxis"]
        return "C" if "Consolidated" in mem else ("S" if "Separate" in mem else None)
    return None


def _num(s):
    try:
        return float(s.replace(",", "").strip())
    except (AttributeError, ValueError):
        return None


def parse_instance(xml):
    """인스턴스 문서 → [{'concept', 'scope', 'start', 'end', 'instant', 'value'}]"""
    ctx = _parse_contexts(xml)
    names = set(DA_TOTAL + DEP + AMORT + ROU + LEASE + LEASE_PARTS)
    facts = []
    for m in re.finditer(r"<([\w-]+:)?([A-Za-z0-9_]+)\b([^>]*\bcontextRef=\"([^\"]+)\"[^>]*)>([^<]*)</", xml):
        concept = m.group(2)
        if concept not in names:
            continue
        c = ctx.get(m.group(4))
        if not c:
            continue
        sc = _scope(c["dims"])
        v = _num(m.group(5))
        if sc is None or v is None:
            continue
        facts.append({"concept": concept, "scope": sc, "start": c["start"], "end": c["end"], "instant": c["instant"], "value": v})
    return facts


def _period(start, end, fy_end_month=12):
    """기간 → (회계연도, 'FY'|'H1'|'Q1'|'Q3') 또는 None(3개월 분기값 등)"""
    if not start or not end:
        return None
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    months = round(((e - s).days + 1) / 30.44)
    if s.month != (fy_end_month % 12) + 1 or s.day != 1:   # 회계연도 첫날부터의 누적만
        return None
    year = e.year if e.month <= fy_end_month else e.year + 1
    return {12: (year, "FY"), 6: (year, "H1"), 3: (year, "Q1"), 9: (year, "Q3")}.get(months, (None, None)) if months in (3, 6, 9, 12) else None


def _instant_period(instant, fy_end_month=12):
    d = dt.date.fromisoformat(instant)
    per = {fy_end_month: "FY", 6: "H1", 3: "Q1", 9: "Q3"}.get(d.month)
    return (d.year, per) if per else None


def extract(facts):
    """facts → {(year, period): {'acc_da_total'|'acc_dep'|'acc_amort'|'acc_rou_dep'|'acc_lease_xbrl': 값}}
    연결(C) 값이 있으면 연결만, 없으면 차원 없음(N) → 별도(S) 순."""
    have = {f["scope"] for f in facts}
    use = "C" if "C" in have else ("N" if "N" in have else "S")
    facts = [f for f in facts if f["scope"] == use]
    by = {}

    def put(key, per, concept, v):
        slot = by.setdefault(per, {})
        # 같은 계정은 우선순위가 높은(목록 앞쪽) 개념 하나만
        slot.setdefault(key, {})[concept] = v

    for f in facts:
        if f["instant"]:
            per = _instant_period(f["instant"])
            if per and f["concept"] in LEASE + LEASE_PARTS:
                put("lease", per, f["concept"], f["value"])
            continue
        per = _period(f["start"], f["end"])
        if not per or per[0] is None:
            continue
        for key, lst in (("da", DA_TOTAL), ("dep", DEP), ("amort", AMORT), ("rou", ROU)):
            if f["concept"] in lst:
                put(key, per, f["concept"], abs(f["value"]))
    out = {}
    pick = lambda d, order: next((d[c] for c in order if c in d), None)
    for per, slot in by.items():
        r = {}
        if "da" in slot:
            r["acc_da_total"] = pick(slot["da"], DA_TOTAL)
        dep_concept = next((c for c in DEP if c in slot.get("dep", {})), None)
        if "dep" in slot:
            r["acc_dep"] = pick(slot["dep"], DEP)
        if "amort" in slot:
            r["acc_amort"] = pick(slot["amort"], AMORT)
        # 사용권자산 상각은 감가상각비가 '현금흐름표 조정 항목'이나 '유형자산 감가상각'일 때만 더한다
        # (주석의 DepreciationExpense는 사용권자산 상각을 이미 포함할 수 있어 이중 계산을 피함)
        if "rou" in slot and dep_concept != "DepreciationExpense":
            r["acc_rou_dep"] = pick(slot["rou"], ROU)
        if "lease" in slot:
            lz = slot["lease"]
            r["acc_lease_xbrl"] = lz["LeaseLiabilities"] if "LeaseLiabilities" in lz else \
                sum(v for k, v in lz.items() if k in LEASE_PARTS)
        if r:
            out[per] = r
    return out


def parse_zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    inst = [n for n in z.namelist() if n.lower().endswith(".xbrl")]
    if not inst:
        raise ValueError("ZIP 안에 XBRL 인스턴스(.xbrl)가 없음")
    facts = []
    for n in inst:
        facts += parse_instance(z.read(n).decode("utf-8", "replace"))
    return extract(facts)


def path_for(code, rcept_no):
    return os.path.join(config.DATA_DIR, "raw", code, f"xbrl_{rcept_no}.zip")


def fetch(code, rcept_no, reprt_code):
    """보관본이 있으면 그것을, 없으면 DART에서 받아 보관한 뒤 해석 결과를 돌려준다."""
    p = path_for(code, rcept_no)
    if os.path.exists(p):
        with open(p, "rb") as f:
            return parse_zip(f.read())
    url = API + "?" + urllib.parse.urlencode({"crtfc_key": dart.api_key(), "rcept_no": rcept_no, "reprt_code": reprt_code})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": dart.UA}), timeout=60) as r:
            data = r.read()
    except Exception as e:
        raise dart.DartUnavailable(f"XBRL 원본 접속 실패: {e}") from e
    if not data.startswith(b"PK"):
        raise dart.DartUnavailable(f"XBRL 원본이 아님(응답: {data[:120]!r})")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return parse_zip(data)
