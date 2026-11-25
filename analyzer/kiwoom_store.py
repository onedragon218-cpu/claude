"""키움 자료 저장·요약 (2026-11-19) — 종목 기본정보(하루 1번), 투자자별 순매수(하루 1번). 주가는 pipeline의 주가 단계에서.
수급은 화면 표시만(판정 점수에는 넣지 않음 — 사용자 결정).
"""
import datetime as dt
import json

from . import db
from .collectors import kiwoom


def ensure(conn):
    db.ensure_tables(conn, """
    CREATE TABLE IF NOT EXISTS kiwoom_info (stock_code TEXT PRIMARY KEY, data TEXT, as_of TEXT, collected_at TEXT);
    CREATE TABLE IF NOT EXISTS kiwoom_flows (
      stock_code TEXT NOT NULL, dt TEXT NOT NULL, price REAL, foreign_net REAL, inst_net REAL, indiv_net REAL, collected_at TEXT,
      UNIQUE(stock_code, dt));
    """)


def collect(conn, code, stats=None, today=None):
    """키움 CLI가 있을 때만. 실패해도 분석은 계속(사유는 fetch_log)."""
    ensure(conn)
    if not kiwoom.find_cli():
        return
    today = today or dt.date.today()
    ymd = today.strftime("%Y%m%d")
    if not db.fetched_recently(conn, "kiwoom_info", code, 1, ok_only=True):
        try:
            if stats:
                stats.req("Kiwoom")
            info = kiwoom.stock_info(code)
            conn.execute("INSERT INTO kiwoom_info(stock_code, data, as_of, collected_at) VALUES (?,?,?,?) ON CONFLICT(stock_code) "
                         "DO UPDATE SET data=excluded.data, as_of=excluded.as_of, collected_at=excluded.collected_at",
                         (code, json.dumps(info, ensure_ascii=False), today.isoformat(), db.now()))
            db.log_fetch(conn, "kiwoom_info", code, code, True, 1)
        except kiwoom.KiwoomUnavailable as e:
            db.log_fetch(conn, "kiwoom_info", code, code, False, 0, str(e)[:200])
            if stats:
                stats.notes.append(f"키움 종목정보 미확보: {e}")
    if not db.fetched_recently(conn, "kiwoom_flows", code, 1, ok_only=True):
        try:
            if stats:
                stats.req("Kiwoom")
            rows = kiwoom.flows(code, ymd, pages=1)
            for r in rows:
                conn.execute("INSERT INTO kiwoom_flows(stock_code, dt, price, foreign_net, inst_net, indiv_net, collected_at) VALUES (?,?,?,?,?,?,?) "
                             "ON CONFLICT(stock_code, dt) DO UPDATE SET price=excluded.price, foreign_net=excluded.foreign_net, "
                             "inst_net=excluded.inst_net, indiv_net=excluded.indiv_net, collected_at=excluded.collected_at",
                             (code, r["dt"], r["price"], r["foreign"], r["inst"], r["indiv"], db.now()))
            db.log_fetch(conn, "kiwoom_flows", code, code, True, len(rows))
        except kiwoom.KiwoomUnavailable as e:
            db.log_fetch(conn, "kiwoom_flows", code, code, False, 0, str(e)[:200])
    conn.commit()


def summary(conn, code):
    """화면용: 연결 상태, 종목정보, 수급(최근 20·60거래일 합계, 억 원)"""
    ensure(conn)
    last = {s: (lambda r: dict(r) if r else None)(db.last_fetch(conn, s, code)) for s in ("kiwoom_info", "kiwoom_flows")}
    pr = conn.execute("SELECT * FROM fetch_log WHERE source='kiwoom_daily' AND key=? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    last["kiwoom_daily"] = dict(pr) if pr else None
    r = conn.execute("SELECT data, as_of FROM kiwoom_info WHERE stock_code=?", (code,)).fetchone()
    info = dict(json.loads(r["data"]), as_of=r["as_of"]) if r else None
    rows = [dict(x) for x in conn.execute("SELECT dt, price, foreign_net, inst_net, indiv_net FROM kiwoom_flows WHERE stock_code=? "
                                          "ORDER BY dt DESC LIMIT 60", (code,))]
    def tot(n, k):
        xs = [x[k] for x in rows[:n] if x[k] is not None]
        return round(sum(xs) / 100, 1) if xs else None        # 백만 원 → 억 원
    fl = None
    if rows:
        fl = {"last_dt": rows[0]["dt"], "n": len(rows),
              "d20": {k: tot(20, k) for k in ("foreign_net", "inst_net", "indiv_net")},
              "d60": {k: tot(60, k) for k in ("foreign_net", "inst_net", "indiv_net")},
              "recent": rows[:5]}
        f, i = fl["d20"]["foreign_net"] or 0, fl["d20"]["inst_net"] or 0
        fl["label"] = "외국인·기관 동반 순매수" if f > 0 and i > 0 else "외국인·기관 동반 순매도" if f < 0 and i < 0 else \
            "외국인 순매수 · 기관 순매도" if f > 0 else "외국인 순매도 · 기관 순매수" if i > 0 else "변화 없음"
    dart_sh = None
    for m, lab in (("shares_common", "DART 보통주"), ("shares_derived", "지배순이익÷EPS 역산")):
        r = conn.execute("SELECT fiscal_year, value FROM facts WHERE stock_code=? AND metric=? AND period='FY' "
                         "AND is_current=1 ORDER BY fiscal_year DESC LIMIT 1", (code, m)).fetchone()
        if r:
            dart_sh = {"fiscal_year": r["fiscal_year"], "value": r["value"], "label": lab}
            break
    return {"cli": bool(kiwoom.find_cli()), "last": last, "info": info, "flows": fl,
            "dart_shares": {"year": dart_sh["fiscal_year"], "value": dart_sh["value"], "label": dart_sh["label"]} if dart_sh else None}
