"""SQLite 저장소.

원칙
- 값은 덮어쓰지 않는다. 바뀌면 새 버전 행을 넣고 이전 행은 is_current=0으로 남긴다.
- 컨센서스는 수집 기준일(as_of)마다 별도 행이다. 과거 스냅샷은 지우지도 고치지도 않는다.
- 모든 외부 조회는 fetch_log에 남긴다. 재사용 여부는 이 표로 확인한다.
"""
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  stock_code TEXT PRIMARY KEY, corp_code TEXT, name TEXT, market TEXT,
  face_value INTEGER, fiscal_month INTEGER DEFAULT 12, added_at TEXT
);
-- 원값(계정·주가·컨센서스). 금액은 원 단위, 비율은 %.
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
  period TEXT NOT NULL DEFAULT 'FY',          -- FY, H1, Q3 ...
  item TEXT NOT NULL,                          -- revenue, op, ni_owner, eq_owner, eq, liab, eps, issued_capital, price_close, shares, bps ...
  kind TEXT NOT NULL,                          -- ACTUAL, ACTUAL_PARTIAL, CONSENSUS, MARKET
  value REAL, unit TEXT, basis TEXT,           -- basis: CFS(연결)/OFS(별도)
  source TEXT NOT NULL, source_ref TEXT, source_url TEXT,
  as_of TEXT NOT NULL DEFAULT '',              -- 자료 기준일(컨센서스·주가). 실적은 ''.
  collected_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'UNVERIFIED',   -- UNVERIFIED VERIFIED CONFLICT CORRECTED STALE MISSING
  status_note TEXT, verified_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, is_current INTEGER NOT NULL DEFAULT 1,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_key ON observations(stock_code, fiscal_year, period, item, kind, source, as_of, is_current);
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, fiscal_year INTEGER, period TEXT, item TEXT,
  kind TEXT, source TEXT, old_value REAL, new_value REAL, old_ref TEXT, new_ref TEXT,
  reason TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stock_code TEXT, source TEXT, target TEXT,
  channel TEXT,            -- api, web, raw-cache, manual, skipped-reuse
  result TEXT, detail TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS validation_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stock_code TEXT, fiscal_year INTEGER,
  item TEXT, check_name TEXT, result TEXT, detail TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, stock_code TEXT, base_year INTEGER, started_at TEXT, finished_at TEXT, summary TEXT
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    # ---------- 기업 ----------
    def upsert_company(self, stock_code, corp_code, name, market, face_value):
        self.con.execute(
            "INSERT INTO companies(stock_code,corp_code,name,market,face_value,added_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(stock_code) DO UPDATE SET corp_code=excluded.corp_code,name=excluded.name,"
            "market=excluded.market,face_value=excluded.face_value",
            (stock_code, corp_code, name, market, face_value, now()))

    def company(self, stock_code):
        return self.con.execute("SELECT * FROM companies WHERE stock_code=?", (stock_code,)).fetchone()

    # ---------- 원값 ----------
    def current(self, stock_code, **kw):
        sql = "SELECT * FROM observations WHERE stock_code=? AND is_current=1"
        args = [stock_code]
        for k, v in kw.items():
            if isinstance(v, (list, tuple)):
                sql += f" AND {k} IN ({','.join('?' * len(v))})"; args += list(v)
            else:
                sql += f" AND {k}=?"; args.append(v)
        return self.con.execute(sql + " ORDER BY fiscal_year, item", args).fetchall()

    def put(self, o, reason="신규 수집"):
        """o: dict. 같은 키(연도·기간·항목·종류·출처·기준일)의 현재 행과 비교한다.
        반환: 'insert' | 'same' | 'new_version'"""
        key = (o["stock_code"], o["fiscal_year"], o.get("period", "FY"), o["item"], o["kind"], o["source"], o.get("as_of", ""))
        cur = self.con.execute(
            "SELECT * FROM observations WHERE stock_code=? AND fiscal_year=? AND period=? AND item=? AND kind=? "
            "AND source=? AND as_of=? AND is_current=1", key).fetchone()
        cols = ["stock_code", "fiscal_year", "period", "item", "kind", "value", "unit", "basis", "source",
                "source_ref", "source_url", "as_of", "collected_at", "status", "status_note", "extra"]
        row = {c: o.get(c) for c in cols}
        row["period"] = row["period"] or "FY"
        row["as_of"] = row["as_of"] or ""
        row["status"] = row["status"] or "UNVERIFIED"
        if cur is not None:
            same = (cur["value"] is None and row["value"] is None) or (
                cur["value"] is not None and row["value"] is not None and abs(cur["value"] - row["value"]) < 1e-9)
            if same:
                return "same"
            # 값이 달라졌다: 이전 행은 남기고 새 버전을 만든다.
            self.con.execute("UPDATE observations SET is_current=0 WHERE id=?", (cur["id"],))
            self.con.execute(
                "INSERT INTO change_log(stock_code,fiscal_year,period,item,kind,source,old_value,new_value,old_ref,new_ref,reason,changed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key[:6], cur["value"], row["value"], cur["source_ref"], row["source_ref"], reason, now()))
            row["version"] = cur["version"] + 1
            if row["status"] == "UNVERIFIED" and cur["status"] == "VERIFIED":
                row["status"] = "CORRECTED"
                row["status_note"] = f"v{cur['version']} {cur['value']} → v{row['version']} ({reason})"
            self._insert(row)
            return "new_version"
        row["version"] = 1
        self._insert(row)
        return "insert"

    def _insert(self, row):
        cols = list(row.keys())
        self.con.execute(f"INSERT INTO observations({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         [row[c] for c in cols])

    def set_status(self, obs_id, status, note=None):
        self.con.execute("UPDATE observations SET status=?, status_note=?, verified_at=? WHERE id=?",
                         (status, note, now(), obs_id))

    # ---------- 로그 ----------
    def log_fetch(self, run_id, stock_code, source, target, channel, result, detail=""):
        self.con.execute("INSERT INTO fetch_log(run_id,stock_code,source,target,channel,result,detail,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                         (run_id, stock_code, source, target, channel, result, detail, now()))

    def log_check(self, run_id, stock_code, year, item, name, result, detail):
        self.con.execute("INSERT INTO validation_results(run_id,stock_code,fiscal_year,item,check_name,result,detail,checked_at) VALUES(?,?,?,?,?,?,?,?)",
                         (run_id, stock_code, year, item, name, result, detail, now()))

    def commit(self):
        self.con.commit()
