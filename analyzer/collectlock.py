"""수집 순서 정하기·충돌 막기·오늘 받은 곳 기록 (2026-11-23)

- 백그라운드 수집(작업 스케줄러: 로그인할 때 + 평일 07:13)과 화면의 수동 갱신(전체·선택·주가만)이 동시에 돌지 않게
  데이터 폴더 state\\collect.lock 파일 하나로 순서를 정한다. 파일에는 누가(background/server)·진행·마지막 신호 시각이 들어 있다.
- 마지막 신호가 3분 넘게 없으면(꺼짐·잠자기·강제 종료) 끝난 것으로 보고 다른 쪽이 가져갈 수 있다.
- 회사마다 '오늘 받음'은 DB 표 collect_done에 남긴다(백그라운드·수동 전체 갱신 모두). 다음 실행은 오늘 아직 안 받은 곳부터 이어서.
"""
import datetime as dt
import json
import os
import time

from . import config, db

STALE_SEC = 180


def _dir():
    return os.path.join(config.DATA_DIR, "state")      # 데이터 폴더 바로 아래가 아닌 하위 폴더(화면 저장본 서명에 안 걸리게)


def lock_path():
    return os.path.join(_dir(), "collect.lock")


def _read():
    try:
        with open(lock_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def status():
    """지금 수집 중인 쪽(살아 있는 잠금만). 없으면 None"""
    d = _read()
    if not d:
        return None
    age = time.time() - float(d.get("heartbeat") or 0)
    if age > STALE_SEC:
        return None
    return dict(d, age=round(age))


def acquire(who, **info):
    """잠금을 가져오면 True. 살아 있는 다른 잠금이 있으면 False(죽은 잠금은 치우고 가져옴)"""
    p = lock_path()
    os.makedirs(_dir(), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if status() is None:            # 죽은 잠금
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(info, who=who, pid=os.getpid(), started=db.now(), heartbeat=time.time()), f, ensure_ascii=False)
        return True
    return False


def mine():
    d = _read()
    return bool(d) and d.get("pid") == os.getpid()


def heartbeat(**info):
    """진행 상황과 함께 '살아 있음' 신호. 잠금이 내 것이 아니면 False(다른 쪽이 가져감 → 멈춰야 함)"""
    d = _read()
    if not d or d.get("pid") != os.getpid():
        return False
    d.update(info, heartbeat=time.time())
    tmp = lock_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, lock_path())
    return True


def release():
    if mine():
        try:
            os.remove(lock_path())
        except OSError:
            pass


# ---------------------------------------------------------------- 오늘 받은 곳
def ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS collect_done (stock_code TEXT PRIMARY KEY, day TEXT, at TEXT, by TEXT)")


def mark_done(conn, code, by):
    ensure(conn)
    conn.execute("INSERT INTO collect_done(stock_code, day, at, by) VALUES (?,?,?,?) ON CONFLICT(stock_code) DO UPDATE SET "
                 "day=excluded.day, at=excluded.at, by=excluded.by", (code, dt.date.today().isoformat(), db.now(), by))
    conn.commit()


def done_today(conn):
    ensure(conn)
    return {r["stock_code"]: r["at"] for r in conn.execute("SELECT stock_code, at FROM collect_done WHERE day=?",
                                                            (dt.date.today().isoformat(),))}


def todo_order(conn, codes):
    """오늘 아직 안 받은 곳만, 마지막으로 받은 지 오래된 곳부터"""
    ensure(conn)
    last = {r["stock_code"]: (r["day"] or "") for r in conn.execute("SELECT stock_code, day FROM collect_done")}
    today = dt.date.today().isoformat()
    return sorted([c for c in codes if last.get(c[0] if isinstance(c, tuple) else c) != today],
                  key=lambda c: last.get(c[0] if isinstance(c, tuple) else c, ""))


# ---------------------------------------------------------------- 회사마다 '갱신 필요' (2026-11-23 사용자 요청)
# 기한(일)은 수집 규칙(config.TTL_DAYS)과 같다 — 화면에 '갱신 필요'가 뜨면 누를 때 실제로 받을 것이 있다는 뜻.
DUE_ITEMS = [("disc", "공시", "dart_disclosures", False), ("news", "뉴스", "news", True), ("cons", "컨센서스", "consensus", True)]


def company_status(conn, today=None):
    """{코드: {'price': 기준일 주가 받은 날, 'disc'·'news'·'cons': 마지막으로 받은 날, 'due': [갱신 필요한 항목 이름]}}"""
    from . import config
    from .collectors import dart
    now = dt.datetime.now() if today is None else dt.datetime.combine(today, dt.time(12))
    has_key = dart.has_key()
    last = {}
    for r in conn.execute("SELECT stock_code, source, MAX(fetched_at) t FROM fetch_log WHERE source IN ('dart_disclosures','news','consensus') "
                          "AND (source='dart_disclosures' OR (ok=1 AND n_items>0)) GROUP BY stock_code, source"):
        last[(r["stock_code"], r["source"])] = r["t"]
    price = {r["code"]: r["t"] for r in conn.execute(
        "SELECT substr(key, 1, 6) code, MAX(fetched_at) t FROM fetch_log WHERE source='price' AND key LIKE '%:asof' AND ok=1 AND n_items>0 GROUP BY code")}
    out = {}
    for (code,) in conn.execute("SELECT stock_code FROM companies").fetchall():
        st = {"price": price.get(code), "due": [], "disc_na": not has_key}
        for k, name, src, _ in DUE_ITEMS:
            t = last.get((code, src))
            st[k] = t
            if k == "disc" and not has_key:
                continue
            ttl = config.TTL_DAYS[src if src != "dart_disclosures" else "dart_disclosures"]
            try:
                age = (now - dt.datetime.fromisoformat(t)).days if t else None
            except ValueError:
                age = None
            if age is None or age >= ttl:
                st["due"].append(name)
        out[code] = st
    return out


def due_codes(conn):
    return [c for c, st in company_status(conn).items() if st["due"]]
