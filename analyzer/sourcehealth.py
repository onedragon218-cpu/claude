"""출처 고장 경보(2026-11-26) — 출처가 조용히 고장 나면 바로 알 수 있게

- 수집 기록(fetch_log)을 출처별로 묶어 최근 24시간·7일 성공률을 셈. '자료가 원래 없음'(공시 전·추정치 없음 등)은 실패로 세지 않음.
- 시험 수집(카나리아): 하루 한 번 삼성전자(005930)로 출처마다 받아 보기 — 늘 자료가 있는 회사라 실패하면 '출처 형식이 바뀌었거나 막힘'.
  (FnGuide가 개편돼 모든 회사에서 고장 났는데 WiseReport가 먼저 성공해 드러나지 않았던 일을 막기 위함)
- 경보: 시험 수집 실패, 또는 24시간 안에 시도 3번 이상인데 성공 0, 또는 시도 5번 이상인데 성공률 50% 미만.
  인터넷 자체가 끊긴 경우(모든 출처가 동시에 실패)는 '인터넷 연결 확인'으로 따로 안내.
"""
import datetime as dt
import re

from . import db

CANARY = "005930"
# 이름 → (수집 기록 출처, 대상 조건 SQL 또는 None)
SOURCES = [
    ("DART 공시", [("dart_disclosures", None), ("dart_events", None)]),
    ("DART 재무제표", [("dart_report", None), ("dart_xbrl", None)]),
    ("WiseReport 컨센서스", [("consensus_src", "key LIKE '%:WiseReport'"), ("consensus_hist", None)]),
    ("FnGuide", [("fg_highlight", None), ("consensus_src", "key LIKE '%:FnGuide'")]),
    ("주가(키움·네이버)", [("price", None), ("kiwoom_daily", None)]),
    ("키움 종목정보·수급", [("kiwoom_info", None), ("kiwoom_flows", None)]),
    ("뉴스", [("news", None), ("hj_news", None)]),
    ("기업개요(WiseReport·DART)", [("profile_wr", None), ("profile_dart", None)]),
]
# 실패로 세지 않을 메모(자료가 원래 없는 경우)
NOT_FAIL = re.compile(r"없음|아직|미공시|추정치|해당 없|공시 전|재무제표가 없는|오늘은")


def _is_fail(r):
    return not r["ok"] and not NOT_FAIL.search(str(r["note"] or ""))


def run_canary(conn, force=False):
    """하루 한 번 출처마다 삼성전자로 시험 수집. 결과는 수집 기록(source='canary', key=출처 이름)에 남김"""
    from .collectors import fnguide, kiwoom, news, prices, wisereport
    tests = [
        ("WiseReport 컨센서스", lambda: len(wisereport.fetch(CANARY)[1]) > 0),
        ("FnGuide", lambda: len(fnguide.fetch_highlight(CANARY)[1]) > 0 and len(fnguide.fetch(CANARY)[1]) > 0),
        ("주가(키움·네이버)", lambda: len((kiwoom.daily(CANARY, dt.date.today().strftime("%Y%m%d")) if kiwoom.find_cli()
                                     else prices.fetch_naver_daily(CANARY, count=30))["rows"]) > 0),
        ("뉴스", lambda: len(news.fetch("삼성전자", dt.date.today() - dt.timedelta(days=7))[2]) > 0),
    ]
    import time
    out = {}
    for name, fn in tests:
        last = db.last_fetch(conn, "canary", name)
        if not force and last is not None:
            age_h = (dt.datetime.now() - dt.datetime.fromisoformat(last["fetched_at"])).total_seconds() / 3600
            if (last["ok"] and age_h < 24) or (not last["ok"] and age_h < 2):    # 성공은 하루 한 번, 실패는 2시간 뒤 다시
                continue
        ok, note = False, None
        for attempt in range(2):            # 순간적인 연결 끊김으로 경보가 뜨지 않게 한 번 더(5초 뒤)
            try:
                ok = bool(fn())
                note = None if ok else "삼성전자인데 값이 비어 있음 — 출처 형식이 바뀌었을 수 있음"
            except Exception as e:      # noqa: BLE001
                ok, note = False, f"{type(e).__name__}: {e}"[:200]
            if ok:
                break
            if attempt == 0:
                time.sleep(5)
        db.log_fetch(conn, "canary", name, CANARY, ok, 1 if ok else 0, note)
        out[name] = ok
    return out


def status(conn, now=None):
    """출처별 상태. 반환: {'rows': [...], 'alarms': [...], 'offline': bool}"""
    now = now or dt.datetime.now()
    d1, d7 = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), (now - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows, alarms = [], []
    canary = {r["key"]: r for r in conn.execute(
        "SELECT key, ok, note, fetched_at FROM fetch_log WHERE id IN (SELECT MAX(id) FROM fetch_log WHERE source='canary' GROUP BY key)")}
    for name, parts in SOURCES:
        recs = []
        for src, cond in parts:
            recs += [dict(r) for r in conn.execute(
                f"SELECT ok, note, fetched_at FROM fetch_log WHERE source=? AND fetched_at>=? {('AND ' + cond) if cond else ''}", (src, d7))]
        r1 = [r for r in recs if r["fetched_at"] >= d1]
        f1 = sum(1 for r in r1 if _is_fail(r))
        f7 = sum(1 for r in recs if _is_fail(r))
        ok1, ok7 = len(r1) - f1, len(recs) - f7
        last_fail = next((r for r in sorted(recs, key=lambda r: r["fetched_at"], reverse=True) if _is_fail(r)), None)
        last_ok = max((r["fetched_at"] for r in recs if not _is_fail(r)), default=None)
        can = canary.get(name)
        row = {"name": name, "n1": len(r1), "ok1": ok1, "n7": len(recs), "ok7": ok7,
               "rate7": round(ok7 / len(recs) * 100) if recs else None, "last_ok": last_ok,
               "last_fail": last_fail["note"][:160] if last_fail and last_fail.get("note") else None,
               "canary": None if not can else {"ok": bool(can["ok"]), "at": can["fetched_at"], "note": can["note"]}}
        why = None
        if can and not can["ok"] and can["fetched_at"] >= d1:
            why = f"시험 수집(삼성전자) 실패 — {can['note'] or ''}"[:200]
        elif len(r1) >= 3 and ok1 == 0:
            why = f"최근 24시간 {len(r1)}번 모두 실패"
        elif len(r1) >= 5 and ok1 / len(r1) < 0.5:
            why = f"최근 24시간 성공 {ok1}/{len(r1)}"
        row["alarm"] = why
        rows.append(row)
    bad = [r for r in rows if r["alarm"]]
    tried = [r for r in rows if r["n1"] or r["canary"]]
    offline = len(tried) >= 3 and len(bad) == len(tried)          # 모든 출처가 같이 실패 → 인터넷 연결 문제일 가능성
    for r in bad:
        alarms.append({"name": r["name"], "why": r["alarm"], "last_fail": r["last_fail"]})
    return {"rows": rows, "alarms": alarms, "offline": offline, "at": now.strftime("%Y-%m-%d %H:%M")}
