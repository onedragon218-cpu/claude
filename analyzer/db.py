"""SQLite 저장소. 값은 덮어쓰지 않고 버전으로 쌓는다."""
import datetime as dt
import os
import re
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  stock_code TEXT PRIMARY KEY, corp_code TEXT, name TEXT,
  created_at TEXT, updated_at TEXT
);
-- 분석에 쓰는 '선택된 값'. 값이 바뀌면 새 버전을 만들고 이전 버전은 is_current=0으로 남긴다.
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
  period TEXT NOT NULL DEFAULT 'FY',        -- FY, H1, Q1, Q3, ASOF
  metric TEXT NOT NULL, value REAL, unit TEXT,
  basis TEXT,                               -- CFS(연결), OFS(별도), -
  data_type TEXT,                           -- ACTUAL, PARTIAL, ESTIMATE, DERIVED
  source TEXT, source_ref TEXT, source_url TEXT,
  collected_at TEXT, as_of TEXT,
  status TEXT NOT NULL DEFAULT 'UNVERIFIED', status_note TEXT, verified_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, is_current INTEGER NOT NULL DEFAULT 1,
  change_reason TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_facts_cur ON facts(stock_code, fiscal_year, period, metric, is_current);
-- 출처별 원본 관측값(후속 보고서의 전기·전전기 열 포함). 교차 검증과 재작성 확인에 쓴다.
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT, fiscal_year INTEGER, period TEXT, metric TEXT, value REAL, unit TEXT,
  basis TEXT, source TEXT, source_ref TEXT, report_year INTEGER, col TEXT,
  collected_at TEXT, as_of TEXT,
  UNIQUE(stock_code, fiscal_year, period, metric, source, source_ref, col)
);
-- 컨센서스 시점별 스냅숏. 절대 수정·삭제하지 않는다(INSERT OR IGNORE만 사용).
CREATE TABLE IF NOT EXISTS consensus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT NOT NULL, target_year INTEGER NOT NULL, metric TEXT NOT NULL,
  value REAL, unit TEXT, broker TEXT NOT NULL DEFAULT '(평균)', n_estimates INTEGER,
  provider TEXT NOT NULL, basis TEXT, as_of TEXT NOT NULL, collected_at TEXT, source_ref TEXT,
  UNIQUE(stock_code, target_year, metric, broker, provider, as_of)
);
-- 컨센서스 월별 추이(WiseReport '컨센서스 추이'). 시점별 값을 쌓기만 한다(INSERT OR IGNORE).
CREATE TABLE IF NOT EXISTS consensus_history (
  stock_code TEXT NOT NULL, target_year INTEGER NOT NULL, metric TEXT NOT NULL,
  as_of TEXT NOT NULL, value REAL, unit TEXT, provider TEXT NOT NULL DEFAULT 'WiseReport',
  collected_at TEXT, source_ref TEXT,
  UNIQUE(stock_code, target_year, metric, as_of, provider)
);
-- 사업부(주요제품) 매출 구성. 시점별로 쌓는다.
CREATE TABLE IF NOT EXISTS segments (
  stock_code TEXT NOT NULL, as_of TEXT NOT NULL, name TEXT NOT NULL, pct REAL, value REAL, unit TEXT,
  fiscal_year INTEGER, kind TEXT NOT NULL DEFAULT 'mix', source TEXT, source_ref TEXT, collected_at TEXT,
  UNIQUE(stock_code, as_of, name, kind, source)
);
-- 뉴스 제목(본문은 저장하지 않음). 링크가 없으면 제목 검색 링크를 쓴다.
CREATE TABLE IF NOT EXISTS news (
  stock_code TEXT NOT NULL, published TEXT NOT NULL, title TEXT NOT NULL, link TEXT, press TEXT,
  description TEXT, source TEXT, query TEXT, collected_at TEXT,
  UNIQUE(stock_code, published, title)
);
-- DART 주요사항·거래소 공시(공급계약, 시설투자, 잠정실적 등) 제목
CREATE TABLE IF NOT EXISTS events (
  rcept_no TEXT PRIMARY KEY, stock_code TEXT, rcept_dt TEXT, title TEXT, kind TEXT, link TEXT, collected_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_code ON events(stock_code, rcept_dt);
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, key TEXT, stock_code TEXT, fetched_at TEXT, ok INTEGER, n_items INTEGER, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_fetch ON fetch_log(source, key, fetched_at);
CREATE INDEX IF NOT EXISTS ix_fetch_code ON fetch_log(stock_code, id);
-- DART 정기공시 목록(정정 공시 탐지용)
CREATE TABLE IF NOT EXISTS disclosures (
  rcept_no TEXT PRIMARY KEY, stock_code TEXT, report_nm TEXT, rcept_dt TEXT,
  report_year INTEGER, reprt_code TEXT, is_correction INTEGER, seen_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_disc_code ON disclosures(stock_code, rcept_dt);
CREATE TABLE IF NOT EXISTS validation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT, run_at TEXT, check_no INTEGER, check_name TEXT,
  fiscal_year INTEGER, metric TEXT, level TEXT, message TEXT
);
"""


def now():
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today():
    return dt.date.today().isoformat()


_SCHEMA_OK = {}


def ensure_tables(conn, script):
    """'없으면 만들기' 묶음을 실제로 없는 표·색인이 있을 때만 실행(2026-11-25).
    SQLite는 이미 있는 표라도 CREATE … IF NOT EXISTS를 실행하면 쓰기 잠금을 잡아, 수집이 쓰는 중이면 화면이 'database is locked'로 멈췄음."""
    names = re.findall(r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", script, re.I)
    try:
        path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    except sqlite3.Error:
        path = ""
    key = (path, hash(script))
    if _SCHEMA_OK.get(key):
        return
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    if not all(n in have for n in names):
        conn.executescript(script)
    if path and path != ":memory:":
        _SCHEMA_OK[key] = True


def set_wal(path=None, wait=30):
    """DB를 WAL 방식으로 바꿀 때까지 최대 wait초 다시 시도(서버·백그라운드 수집이 시작할 때). 반환: 바뀐 뒤 방식('wal' 등)
    바꾸려면 잠깐 혼자 써야 해서, 다른 프로그램이 쓰는 중이면 실패할 수 있음 → 잠깐씩 기다리며 되풀이."""
    import time
    path = path or config.DB_PATH
    if not os.path.exists(path):
        return None
    t0, mode = time.time(), None
    while True:
        try:
            c = sqlite3.connect(path, timeout=2)
            try:
                mode = c.execute("PRAGMA journal_mode").fetchone()[0].lower()
                if mode != "wal":
                    mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()
            finally:
                c.close()
        except sqlite3.OperationalError:
            mode = None
        if mode == "wal" or time.time() - t0 >= wait:
            return mode
        time.sleep(1)


def connect(path=None, timeout=30):
    """timeout: 다른 프로그램(백그라운드 수집 등)이 쓰는 중이면 기다리는 최대 초(화면은 짧게).
    2026-11-25: WAL 방식 — 수집이 쓰는 동안에도 화면이 읽을 수 있게(예전 방식은 화면이 쓰려다 5초 뒤 'database is locked'로 빈 화면)."""
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:      # 다른 쪽이 쓰는 중이면 다음 연결 때 바꿈
        pass
    ensure_tables(conn, SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)")]
    if "induty_code" not in cols:   # 업종코드(한국표준산업분류) 칸 추가
        conn.execute("ALTER TABLE companies ADD COLUMN induty_code TEXT")
        conn.commit()
    _drop_doca_tables(conn)
    return conn


# 2026-11-15 화면 R·S(되돌림)에서 만들었던 표. 그 기능에서만 쓰던 표라 지워도 기존 자료에 영향 없음(2026-11-16 사용자 지시)
DOCA_TABLES = ("nodes", "company_nodes", "node_signal_defs", "node_signals", "node_judgments", "company_node_verdict",
               "segment_fin", "peer_groups", "peer_foreign", "doca_refs")


def _drop_doca_tables(conn):
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    gone = [t for t in DOCA_TABLES if t in have]
    for t in gone:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    if gone:
        conn.commit()
    return gone


def backup(path=None, tag="before_delete"):
    """DB 파일을 데이터 폴더 backup\\ 에 통째로 복사(SQLite 백업 기능, 쓰는 중에도 안전). 반환: 백업 파일 경로"""
    src = path or config.DB_PATH
    d = os.path.join(os.path.dirname(src), "backup")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"analyzer_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.db")
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        b.close(); a.close()
    return dst


def delete_companies(conn, codes):
    """'DB에 있는 기업'에서 지우기: 종목코드 칸이 있는 모든 표에서 그 기업 줄을 지운다. 반환: {표: 지운 줄 수}"""
    codes = [c for c in dict.fromkeys(str(x).strip().upper() for x in codes) if len(c) == 6 and c.isalnum()]
    out = {}
    if not codes:
        return out
    q = ",".join("?" * len(codes))
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        if "stock_code" in cols:
            n = conn.execute(f"DELETE FROM {t} WHERE stock_code IN ({q})", codes).rowcount
            if n:
                out[t] = n
    conn.commit()
    return out


def doca_added(conn):
    """작업 폴더 가져오기(2026-11-15, 되돌림)가 '데이터베이스에 있는 기업'에 넣은 것으로 보이는 기업.
    기준: 기업 등록 시각이 가져오기 기록(fetch_log 'doca_import') 직전 10분 안. 반환: 종목코드 목록"""
    out = []
    for r in conn.execute("SELECT fetched_at FROM fetch_log WHERE source='doca_import'"):
        try:
            end = dt.datetime.fromisoformat(r["fetched_at"])
        except (TypeError, ValueError):
            continue
        start = (end - dt.timedelta(minutes=10)).isoformat(sep=" ")
        out += [x["stock_code"] for x in conn.execute("SELECT stock_code FROM companies WHERE created_at BETWEEN ? AND ?",
                                                      (start, end.isoformat(sep=" ")))]
    return sorted(set(out))


def set_induty(conn, stock_code, induty_code):
    conn.execute("UPDATE companies SET induty_code=? WHERE stock_code=?", (induty_code, stock_code))
    conn.commit()


def ensure_company(conn, stock_code, name=None, corp_code=None):
    row = conn.execute("SELECT * FROM companies WHERE stock_code=?", (stock_code,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO companies (stock_code, corp_code, name, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (stock_code, corp_code, name, now(), now()))
    else:
        conn.execute("UPDATE companies SET name=COALESCE(?,name), corp_code=COALESCE(?,corp_code), updated_at=? "
                     "WHERE stock_code=?", (name, corp_code, now(), stock_code))
    conn.commit()
    return conn.execute("SELECT * FROM companies WHERE stock_code=?", (stock_code,)).fetchone()


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def current_fact(conn, code, year, metric, period="FY"):
    return conn.execute("SELECT * FROM facts WHERE stock_code=? AND fiscal_year=? AND period=? AND metric=? "
                        "AND is_current=1", (code, year, period, metric)).fetchone()


def put_fact(conn, code, year, metric, value, period="FY", unit=None, basis=None, data_type="ACTUAL",
             source=None, source_ref=None, source_url=None, as_of=None, status="UNVERIFIED",
             status_note=None, change_reason=None, collected_at=None, clear=False):
    """값을 저장한다. 같은 값이면 아무것도 하지 않고, 다르면 새 버전을 만든다.
    반환: 'new' | 'same' | 'changed'"""
    cur = current_fact(conn, code, year, metric, period)
    if cur is not None and _same(cur["value"], value) and (cur["source_ref"] == source_ref or value is None):
        return "same"
    if cur is not None and cur["value"] is not None and value is None and not clear:
        return "same"   # 이미 값이 있는데 '없음'으로 덮어쓰지 않는다(clear=True: 값이 틀렸다고 확인된 경우만 — 자료 충돌·재해석, 2026-11-28)
    if cur is not None and value is None and clear and cur["value"] is None and cur["status_note"] == status_note:
        return "same"
    version = 1
    if cur is not None:
        version = cur["version"] + 1
        conn.execute("UPDATE facts SET is_current=0 WHERE id=?", (cur["id"],))
        if change_reason is None:
            change_reason = "출처 변경" if _same(cur["value"], value) else "값 변경"
    conn.execute(
        "INSERT INTO facts (stock_code,fiscal_year,period,metric,value,unit,basis,data_type,source,source_ref,"
        "source_url,collected_at,as_of,status,status_note,version,is_current,change_reason,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (code, year, period, metric, value, unit, basis, data_type, source, source_ref, source_url,
         collected_at or today(), as_of, status if value is not None or status == "CONFLICT" else "MISSING", status_note, version,
         change_reason, now()))
    return "new" if cur is None else "changed"


def set_status(conn, fact_id, status, note=None):
    conn.execute("UPDATE facts SET status=?, status_note=?, verified_at=? WHERE id=?",
                 (status, note, now() if status == "VERIFIED" else None, fact_id))


def facts(conn, code, current_only=True):
    q = "SELECT * FROM facts WHERE stock_code=?" + (" AND is_current=1" if current_only else "")
    return conn.execute(q + " ORDER BY fiscal_year, period, metric, version", (code,)).fetchall()


def fact_map(conn, code, period="FY"):
    """{(year, metric): row} 현재 값만."""
    return {(r["fiscal_year"], r["metric"]): r for r in conn.execute(
        "SELECT * FROM facts WHERE stock_code=? AND period=? AND is_current=1", (code, period))}


def history(conn, code):
    """값이 바뀐 이력: 각 버전과 직전 버전을 짝지어 돌려준다."""
    rows = conn.execute(
        "SELECT n.fiscal_year, n.period, n.metric, o.value AS old_value, n.value AS new_value, n.change_reason, "
        "o.source_ref AS old_ref, n.source_ref AS new_ref, n.created_at FROM facts n JOIN facts o "
        "ON o.stock_code=n.stock_code AND o.fiscal_year=n.fiscal_year AND o.period=n.period AND o.metric=n.metric "
        "AND o.version=n.version-1 WHERE n.stock_code=? ORDER BY n.created_at", (code,)).fetchall()
    return rows


def add_observation(conn, code, year, metric, value, period="FY", unit=None, basis=None, source=None,
                    source_ref=None, report_year=None, col=None, as_of=None):
    conn.execute("INSERT OR IGNORE INTO observations (stock_code,fiscal_year,period,metric,value,unit,basis,source,"
                 "source_ref,report_year,col,collected_at,as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (code, year, period, metric, value, unit, basis, source, source_ref, report_year, col, today(), as_of))


def observations(conn, code, year=None, metric=None, period="FY"):
    q, a = "SELECT * FROM observations WHERE stock_code=? AND period=?", [code, period]
    if year is not None:
        q += " AND fiscal_year=?"; a.append(year)
    if metric is not None:
        q += " AND metric=?"; a.append(metric)
    return conn.execute(q + " ORDER BY report_year", a).fetchall()


def add_consensus(conn, code, target_year, metric, value, provider, as_of, unit=None, broker="(평균)",
                  n_estimates=None, basis=None, source_ref=None):
    """컨센서스 스냅숏 추가. 같은 (대상연도, 지표, 증권사, 제공기관, 기준일)이 있으면 무시한다."""
    cur = conn.execute("INSERT OR IGNORE INTO consensus (stock_code,target_year,metric,value,unit,broker,n_estimates,"
                       "provider,basis,as_of,collected_at,source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (code, target_year, metric, value, unit, broker or "(평균)", n_estimates, provider, basis,
                        as_of, today(), source_ref))
    return cur.rowcount


def latest_consensus(conn, code):
    """대상 연도·지표별로 가장 최근 기준일의 평균값. 같은 날 여러 출처면 직접 입력 > FnGuide > Naver 순."""
    rank = {"manual": 0, "FnGuide": 1, "Naver": 2}
    best = {}
    for r in conn.execute("SELECT * FROM consensus WHERE stock_code=? AND broker='(평균)'", (code,)):
        k = (r["target_year"], r["metric"])
        key = (r["as_of"], -rank.get(r["provider"], 9))
        if k not in best or key > best[k][0]:
            best[k] = (key, r)
    return [v[1] for v in best.values()]


def add_consensus_history(conn, code, rows, source_ref=None):
    """rows: [(target_year, metric, as_of 'YYYY-MM-DD', value, unit)] → 새로 저장한 건수"""
    n = 0
    for y, m, d, v, u in rows:
        if v is None:
            continue
        cur = conn.execute("INSERT OR IGNORE INTO consensus_history(stock_code, target_year, metric, as_of, value, unit, "
                           "collected_at, source_ref) VALUES (?,?,?,?,?,?,?,?)", (code, y, m, d, v, u, now(), source_ref))
        n += cur.rowcount
    conn.commit()
    return n


def consensus_history(conn, code):
    return [dict(r) for r in conn.execute("SELECT target_year, metric, as_of, value, unit FROM consensus_history "
                                          "WHERE stock_code=? ORDER BY target_year, metric, as_of", (code,))]


def add_segments(conn, code, as_of, rows, source, source_ref=None, kind="mix"):
    """rows: [(name, pct, value, unit, fiscal_year)]"""
    n = 0
    for name, pct, value, unit, fy in rows:
        n += conn.execute("INSERT OR IGNORE INTO segments(stock_code, as_of, name, pct, value, unit, fiscal_year, kind, source, "
                          "source_ref, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (code, as_of, name, pct, value, unit, fy, kind, source, source_ref, now())).rowcount
    conn.commit()
    return n


def add_news(conn, code, items, source, query=None):
    """items: [{published 'YYYY-MM-DD', title, link, press, description}]"""
    n = 0
    for it in items:
        if not it.get("title") or not it.get("published"):
            continue
        n += conn.execute("INSERT OR IGNORE INTO news(stock_code, published, title, link, press, description, source, query, "
                          "collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (code, it["published"], it["title"], it.get("link"), it.get("press"), it.get("description"),
                           source, query, now())).rowcount
    conn.commit()
    return n


def add_events(conn, code, items, kind):
    n = 0
    for it in items:
        n += conn.execute("INSERT OR IGNORE INTO events(rcept_no, stock_code, rcept_dt, title, kind, link, collected_at) "
                          "VALUES (?,?,?,?,?,?,?)", (it["rcept_no"], code, it["rcept_dt"], it["title"], kind,
                                                     it.get("link"), now())).rowcount
    conn.commit()
    return n


_TL = threading.local()     # 갱신 단계별 시간(이 스레드에서 analyze가 도는 동안만)


def timing_start():
    _TL.acc, _TL.t = {}, time.time()


def timing_mark(name):
    """앞 표시 뒤로 흐른 시간을 name에 더함(수집 기록을 남길 때마다 그 출처 이름으로 자동 호출)"""
    acc = getattr(_TL, "acc", None)
    if acc is None:
        return
    now = time.time()
    acc[name] = acc.get(name, 0.0) + now - _TL.t
    _TL.t = now


def timing_end():
    acc = getattr(_TL, "acc", None) or {}
    _TL.acc = None
    return {k: round(v, 2) for k, v in sorted(acc.items(), key=lambda x: -x[1])}


def log_fetch(conn, source, key, code, ok, n_items=0, note=None):
    timing_mark(source)
    conn.execute("INSERT INTO fetch_log (source,key,stock_code,fetched_at,ok,n_items,note) VALUES (?,?,?,?,?,?,?)",
                 (source, key, code, now(), 1 if ok else 0, n_items, note))
    conn.commit()


def last_fetch(conn, source, key):
    return conn.execute("SELECT * FROM fetch_log WHERE source=? AND key=? ORDER BY id DESC LIMIT 1",
                        (source, key)).fetchone()


FORCE = set()     # B탭 [다시 받기]: 여기 든 출처는 기다리는 기간을 무시하고 이번 실행에서 다시 받음


def fetched_recently(conn, source, key, ttl_days, ok_only=False):
    """ok_only=True면 '성공해서 값을 받은' 요청만 센다(실패는 다음 실행 때 다시 시도)."""
    if source in FORCE:
        return False
    if ok_only:
        r = conn.execute("SELECT * FROM fetch_log WHERE source=? AND key=? AND ok=1 AND n_items>0 "
                         "ORDER BY id DESC LIMIT 1", (source, key)).fetchone()
    else:
        r = last_fetch(conn, source, key)
    if r is None:
        return False
    t = dt.datetime.fromisoformat(r["fetched_at"])
    return (dt.datetime.now() - t).days < ttl_days
