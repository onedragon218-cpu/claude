"""사용법
  python3 -m fintrack run 213500                 # 기준 연도 = 올해(자동)
  python3 -m fintrack run 213500 --base-year 2027
  python3 -m fintrack run 213500 --reverify      # 저장값 재검증(조회는 하지 않음)
  python3 -m fintrack run 213500 --refresh       # 컨센서스 갱신 주기 무시하고 새로 조회
  python3 -m fintrack status 213500              # DB 보유 현황만 출력
"""
import argparse, json, os
from datetime import datetime

from . import config, dashboard
from .db import Store, now
from .metrics import build
from .pipeline import Collector
from .validate import Validator


def status(store, code):
    rows = store.con.execute(
        "SELECT kind, status, COUNT(*) n FROM observations WHERE stock_code=? AND is_current=1 GROUP BY kind, status ORDER BY kind, status",
        (code,)).fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser(prog="fintrack")
    ap.add_argument("cmd", choices=["run", "status"])
    ap.add_argument("code")
    ap.add_argument("--base-year", type=int)
    ap.add_argument("--reverify", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--offline", action="store_true", help="네트워크 조회 없이 보관 원본과 DB만 사용")
    ap.add_argument("--db", default=config.DB_PATH)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.db), exist_ok=True)
    s = Store(a.db)
    if a.cmd == "status":
        print(json.dumps(status(s, a.code), ensure_ascii=False, indent=1)); return

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    p = config.periods(a.base_year)
    existed = s.company(a.code) is not None
    s.con.execute("INSERT INTO runs(run_id,stock_code,base_year,started_at) VALUES(?,?,?,?)", (run_id, a.code, p["base"], now()))
    print(f"[{a.code}] 기준연도 {p['base']} · 실적 {p['actual'][0]}~{p['actual'][-1]} · 전망 {p['base']}~{p['future'][-1]}"
          f" · DB 기존 보유: {'예' if existed else '아니오(첫 분석)'}")

    c = Collector(s, a.code, p["base"], run_id, offline=a.offline)
    c.actuals()
    c.partial()
    c.prices()
    c.consensus(force=a.refresh)
    s.commit()
    v = Validator(s, a.code, p, run_id, reverify=a.reverify).run()
    series = build(s, a.code, p)
    st = c.stats
    summary = {"외부·원본 조회": st["fetched"], "재사용(조회 생략)": st["reused"], "신규 저장": st["inserted"],
               "변경 없음": st["same"], "새 버전": st["new_version"], "미확보 계정": len(st["missing"]), "검증": v}
    s.con.execute("UPDATE runs SET finished_at=?, summary=? WHERE run_id=?", (now(), json.dumps(summary, ensure_ascii=False), run_id))
    s.commit()
    for r in s.con.execute("SELECT source,target,channel,result,detail FROM fetch_log WHERE run_id=?", (run_id,)):
        print(f"  - {r['channel']:<13} {r['result']:<4} {r['source']} | {r['target']} | {r['detail']}")
    print("  요약:", json.dumps(summary, ensure_ascii=False))
    out = dashboard.write(s, a.code, p, series, run_id)
    print("  화면:", os.path.relpath(out, config.ROOT))


if __name__ == "__main__":
    main()
