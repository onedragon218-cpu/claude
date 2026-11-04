"""업종 평균(한국은행 기업경영분석) 기준선.

- 업종 연결: DART 기업개황의 업종코드(induty_code, 한국표준산업분류) 앞 2자리 → ECOS 업종 항목(예: 171 → C17, 59114 → J59).
- 자료: analyzer/benchmarks.json(저장본) + data/benchmarks_ecos.json(ECOS_API_KEY가 있을 때 자동으로 받은 업종).
- 해당 업종 자료가 없으면 기준선을 그리지 않고 '전산업'만 참고로 보여 준다(값을 만들지 않음).
"""
import json
import os
import urllib.request

from . import config

BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmarks.json")
CACHE = lambda: os.path.join(config.DATA_DIR, "benchmarks_ecos.json")

# 한국표준산업분류 중분류(2자리) → 대분류 문자
SECTIONS = [((1, 3), "A"), ((5, 8), "B"), ((10, 34), "C"), ((35, 35), "D"), ((36, 39), "E"), ((41, 42), "F"),
            ((45, 47), "G"), ((49, 52), "H"), ((55, 56), "I"), ((58, 63), "J"), ((64, 66), "K"), ((68, 68), "L"),
            ((70, 73), "M"), ((74, 76), "N"), ((84, 84), "O"), ((85, 85), "P"), ((86, 87), "Q"), ((90, 91), "R"),
            ((94, 96), "S")]


def ecos_item(induty_code):
    if not induty_code or not str(induty_code)[:2].isdigit():
        return None
    two = int(str(induty_code)[:2])
    for (a, b), letter in SECTIONS:
        if a <= two <= b:
            return f"{letter}{two:02d}"
    return None


def _load():
    with open(BUNDLED, encoding="utf-8") as f:
        data = json.load(f)
    if os.path.exists(CACHE()):
        try:
            with open(CACHE(), encoding="utf-8") as f:
                data["industries"].update(json.load(f).get("industries", {}))
        except (OSError, ValueError):
            pass
    return data


def fetch_ecos(item, years=(2015, 2025)):
    """ECOS Open API로 업종 부채비율·이자보상비율을 받아 data/benchmarks_ecos.json 에 추가한다(ECOS_API_KEY 필요).
    주의: 이 개발 환경에서는 ECOS 직접 접속이 막혀 실제 호출은 시험하지 못했다."""
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key or not item:
        return False
    res = {"name": item, "debt_ratio": {}, "interest_coverage_pct": {}}
    for stat, code, field in (("501Y007", "707", "debt_ratio"), ("501Y006", "627", "interest_coverage_pct")):
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/{stat}/A/{years[0]}/{years[1]}/"
               f"{item}/A/{code}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                rows = json.loads(r.read().decode("utf-8")).get("StatisticSearch", {}).get("row", [])
        except Exception:
            return False
        for row in rows:
            if row.get("DATA_VALUE") not in (None, ""):
                res[field][row["TIME"]] = float(row["DATA_VALUE"])
                res["name"] = row.get("ITEM_NAME1") or res["name"]
    if not res["debt_ratio"]:
        return False
    cache = {"industries": {}}
    if os.path.exists(CACHE()):
        with open(CACHE(), encoding="utf-8") as f:
            cache = json.load(f)
    cache["industries"][item] = res
    with open(CACHE(), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return True


def for_company(induty_code):
    """→ {item, name, debt_ratio{year:v}, icr{year:배}, all{...}, source} 또는 업종 없음 표시"""
    data = _load()
    item = ecos_item(induty_code)
    if item and item not in data["industries"]:
        if fetch_ecos(item):
            data = _load()
    ind = data["industries"].get(item) if item else None
    allind = data["industries"].get("ZZZ00")
    conv = lambda d: {int(y): v / 100 for y, v in (d or {}).items()}
    return {
        "item": item if ind else None, "name": ind["name"] if ind else None,
        "debt_ratio": {int(y): v for y, v in ind["debt_ratio"].items()} if ind else {},
        "icr": conv(ind.get("interest_coverage_pct")) if ind else {},
        "all_debt_ratio": {int(y): v for y, v in allind["debt_ratio"].items()} if allind else {},
        "all_icr": conv(allind.get("interest_coverage_pct")) if allind else {},
        "source": data.get("_source"),
        "missing_reason": None if ind else (f"업종코드 {induty_code}({item})의 한국은행 업종 평균 자료가 없음"
                                            if induty_code else "업종코드 없음(DART 기업개황 미수집)"),
    }
