"""컨센서스 종목 찾기.

1) 전체 상장 종목 목록: 한국거래소 정보데이터시스템 종목 검색 자료(로그인 불필요, 시장 구분 포함).
   한국거래소에 접속할 수 없으면 DART 고유번호 목록(DART 키 필요)으로 대신한다.
2) 제외(이름·코드 규칙, 결과 화면에 사유별 개수 표시):
   - 우선주·기타 종류주: 종목코드 끝자리가 0이 아님(예: 005935, 00104K) 또는 이름이 '우'·'우B' 등으로 끝남
   - 스팩: 이름에 '스팩'·'기업인수목적'
   - 리츠: 이름이 '리츠'로 끝남, '이리츠코크렙', '부동산투자회사'
     ('메리츠금융지주'·'블리츠웨이엔터테인먼트'처럼 이름 중간에 '리츠'가 든 일반 기업은 제외하지 않음)
   - 인프라펀드: 맥쿼리인프라, KB발해인프라, 이름이 '투융자회사'로 끝남
     ('바이오인프라'·'NICE인프라' 같은 일반 기업은 제외하지 않음)
3) 남은 종목마다 WiseReport 컨센서스 자료에 추정치(E)가 있는지 확인(가벼운 요청 1건).
   있는 종목만 기업개요를 한 번 더 읽어 시장·업종·추정기관 수·목표주가를 붙인다.
3-1) 컨센서스가 있는 종목은 DART로 상장 유지 위험을 점검한다(risk.py: 관리종목·투자주의환기·상장폐지·실질심사·
   거래정지·감사의견 비적정·회생·파산·자본잠식 50% 이상은 '제외', 관리종목 지정 우려·부분 자본잠식은 '주의').
4) 결과는 data/consensus_scan.json 에 저장. 7일 안에 확인한 종목은 다시 묻지 않는다(중간에 끊겨도 이어서 진행).

재무 자료를 DB에 넣지 않는다. DB에 넣는 것은 사용자가 고른 종목만(기본 목록 추가 또는 수집).
"""
import datetime as dt
import json
import os
import re
import threading
import time

from . import config, universe
from .collectors import dart, wisereport

CACHE = lambda: os.path.join(config.DATA_DIR, "consensus_scan.json")
RECHECK_DAYS = 7
DELAY = 0.25   # 요청 사이 간격(초)

JOB = {"running": False, "phase": None, "total": 0, "done": 0, "found": 0, "current": None, "error": None,
       "started_at": None, "finished_at": None}
LOCK = threading.Lock()


def classify(code, name):
    """제외 사유(없으면 None)"""
    n = (name or "").replace(" ", "")
    if code and code[-1] != "0":
        return "우선주·기타 종류주"
    if re.search(r"(\d?우[A-C]?|우선주)$", n):
        return "우선주·기타 종류주"
    if "스팩" in n or "기업인수목적" in n or re.search(r"SPAC", n, re.I):
        return "스팩"
    if n.endswith("리츠") or n.startswith("이리츠") or "부동산투자회사" in n or re.search(r"REITs?$", n, re.I):
        return "리츠"
    if n in INFRA_FUNDS or n.endswith("투융자회사"):
        return "인프라펀드"
    return None


INFRA_FUNDS = {"맥쿼리인프라", "KB발해인프라"}


def load():
    if os.path.exists(CACHE()):
        try:
            with open(CACHE(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"checked": {}, "excluded": {}, "started_at": None, "finished_at": None, "total_listed": 0}


def save(data):
    os.makedirs(os.path.dirname(CACHE()), exist_ok=True)
    tmp = CACHE() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CACHE())


def listed():
    """[(code, name, market)] 과 출처 이름"""
    from .collectors import krx
    try:
        return [(r["code"], r["name"], r.get("market")) for r in krx.listing()], "한국거래소"
    except krx.KrxUnavailable as e:
        corp = dart.corp_list(refresh_days=7)
        if not corp:
            raise RuntimeError(f"전체 상장 종목 목록을 받지 못했습니다({e}). DART 인증키가 있으면 DART 목록으로 대신합니다.")
        return [(c, v["name"], None) for c, v in corp.items()], "DART(한국거래소 접속 실패로 대신 사용)"


def candidates():
    """(전체 종목 수, 후보 [(code, name, market)], 제외 {사유: [[code, name]]}, 출처)"""
    rows, src = listed()
    cands, excl = [], {}
    for code, name, market in sorted(rows):
        why = classify(code, name)
        if why:
            excl.setdefault(why, []).append([code, name])
        else:
            cands.append((code, name, market))
    return len(rows), cands, excl, src


def _fresh(item):
    try:
        return (dt.date.today() - dt.date.fromisoformat(item.get("checked_at", "1900-01-01"))).days < RECHECK_DAYS
    except ValueError:
        return False


def run(full=False):
    """백그라운드 작업 본체. full=True면 7일 안에 확인한 종목도 다시 확인."""
    data = load()
    try:
        with LOCK:
            JOB.update(phase="상장사 목록 받는 중")
        total, cands, excl, src = candidates()
        data.update(total_listed=total, excluded=excl, source=src,
                    started_at=data.get("started_at") or dt.datetime.now().isoformat(" ", "seconds"))
        markets = {c: m for c, _, m in cands}
        key = dart.has_key()
        todo = [(c, n) for c, n, _ in cands if full or not _fresh(data["checked"].get(c, {}))
                or (key and data["checked"][c].get("has") and not (data["checked"][c].get("risk") or {}).get("checked"))]
        lim = int(os.environ.get("ANALYZER_SCAN_LIMIT", "0") or 0)   # 시험용: 앞의 N종목만
        if lim:
            keep = {"213500", "253450", "086900", "005930", "0126Z0", "060310", "035720"}
            todo = [t for t in todo if t[0] in keep] + [t for t in todo if t[0] not in keep][:max(0, lim - len(keep))]
            cands = [t for t in cands if t[0] in {c for c, _ in todo}]
        with LOCK:
            JOB.update(phase="컨센서스 확인 중", total=len(cands), done=len(cands) - len(todo),
                       found=sum(1 for v in data["checked"].values() if v.get("has")))
        for i, (code, name) in enumerate(todo):
            with LOCK:
                JOB["current"] = f"{name}({code})"
            item = {"name": name, "checked_at": dt.date.today().isoformat(), "market": markets.get(code)}
            prev = data["checked"].get(code, {})
            try:
                if not full and _fresh(prev) and prev.get("has"):
                    item.update({k: v for k, v in prev.items() if k != "risk"})   # 위험 점검만 새로
                else:
                    item.update(wisereport.check(code))
                    time.sleep(DELAY)
                    if item.get("has"):
                        item.update(wisereport.profile(code))
                        time.sleep(DELAY)
            except wisereport.ConsensusUnavailable as e:
                item.update(has=False, error=str(e)[:120])
            # 상장 유지 위험(관리종목·상장폐지·거래정지·자본잠식 등): 컨센서스가 있는 종목만, DART 키가 있을 때
            if item.get("has"):
                from . import risk
                if key:
                    try:
                        item["risk"] = risk.assess(dart.corp_code(code)["corp_code"])
                    except Exception as e:   # DART 응답 문제는 '미확인'으로 남김(제외하지 않음)
                        item["risk"] = {"checked": False, "level": "미확인", "note": str(e)[:120]}
                else:
                    item["risk"] = {"checked": False, "level": "미확인", "note": "DART 키 없음"}
            data["checked"][code] = item
            with LOCK:
                JOB["done"] += 1
                JOB["found"] = sum(1 for v in data["checked"].values() if v.get("has"))
            if i % 25 == 0:
                save(data)
        # 상장 목록에서 빠진(상장폐지 등) 종목은 결과에서 지운다
        live = {c for c, _, _ in cands}
        data["checked"] = {c: v for c, v in data["checked"].items() if c in live}
        data["finished_at"] = dt.datetime.now().isoformat(" ", "seconds")
        save(data)
    except Exception as e:   # 화면에 원인 표시
        with LOCK:
            JOB["error"] = str(e)
        save(data)
    finally:
        with LOCK:
            JOB.update(running=False, current=None, phase="완료" if not JOB["error"] else "실패",
                       finished_at=dt.datetime.now().isoformat(" ", "seconds"))


def start(full=False):
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, phase="시작", total=0, done=0, found=0, current=None, error=None,
                   started_at=dt.datetime.now().isoformat(" ", "seconds"), finished_at=None)
    threading.Thread(target=run, args=(full,), daemon=True).start()
    return True


def results(conn=None):
    """화면용 결과: 컨센서스가 있는 종목만 + 요약"""
    data = load()
    in_univ = {c for c, _ in universe.load()}
    in_db = set()
    if conn is not None:
        in_db = {r[0] for r in conn.execute("SELECT DISTINCT stock_code FROM facts")}
    rows = []
    for code, v in data.get("checked", {}).items():
        if not v.get("has"):
            continue
        rk = v.get("risk") or {"level": "미확인"}
        rows.append(dict(risk_level=rk.get("level", "미확인"), risk_flags=rk.get("flags", []), risk_note=rk.get("note"),
                         impairment=rk.get("impairment"),
                         code=code, name=v.get("name"), market=v.get("market"), wics=v.get("wics"),
                         krx_sector=v.get("krx_sector"), n_estimates=v.get("n_estimates"),
                         target_price=v.get("target_price"), years=v.get("years"), first_year=v.get("first_year"),
                         revenue=v.get("revenue"), op_income=v.get("op_income"), eps=v.get("eps"), roe=v.get("roe"),
                         checked_at=v.get("checked_at"), in_universe=code in in_univ, in_db=code in in_db))
    checked = data.get("checked", {})
    return {"rows": rows, "summary": {
        "total_listed": data.get("total_listed", 0), "source": data.get("source"),
        "excluded": {k: len(v) for k, v in data.get("excluded", {}).items()},
        "checked": len(checked), "with_consensus": len(rows),
        "errors": sum(1 for v in checked.values() if v.get("error")),
        "risk": {lv: sum(1 for r in rows if r["risk_level"] == lv) for lv in ("제외", "주의", "정상", "미확인")},
        "finished_at": data.get("finished_at")}}
