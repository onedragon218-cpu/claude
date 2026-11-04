"""명령줄 실행.

  python -m analyzer analyze 213500 [005930 ...] [--base-year 2026] [--offline]
  python -m analyzer update-all              # DB에 있는 모든 기업을 갱신(자동 실행용)
  python -m analyzer report 213500           # 수집 없이 화면만 다시 만들기
  python -m analyzer status 213500
  python -m analyzer import-consensus 213500 파일.csv
  python -m analyzer reverify 213500 [--year 2024]   # 사용자 재검증 요청
  python -m analyzer serve [--port 8765]
"""
import argparse
import json
import os
import sys

from . import config, db, pipeline, report


def resolve(conn, q):
    from . import search
    try:
        return search.resolve(conn, q)
    except (LookupError, search.Ambiguous) as e:
        raise SystemExit(str(e))


def do_analyze(code, base_year=None, offline=False, quiet=False):
    conn = db.connect()
    res = pipeline.analyze(code, base_year, offline=offline, conn=conn)
    out = os.path.join(config.OUTPUT_DIR, f"{code}.html")
    report.render(report.payload(conn, code, base_year, last_run=res["stats"]), out)
    conn.close()
    if not quiet:
        s = res["stats"]
        print(f"[{code}] 기준연도 {res['periods']['base_year']} · 기존 데이터 {'있음' if res['had_data'] else '없음'}")
        print(f"  외부 요청 {s['total_requests']}건 {s['requests']} · 재사용 {s['reused_facts']} · 신규 {s['new_facts']} · 변경 {s['changed_facts']}")
        for n in s["notes"]:
            print("  -", n)
        print(f"  검증 요약: {res['validation']['summary']}")
        print(f"  화면: {out}")
    return res


def _log_when_windowless():
    """작업 스케줄러가 pythonw로 실행하면 화면 출력이 없으므로 data/logs/ 에 기록한다."""
    if sys.stdout is None or os.environ.get("ANALYZER_LOG"):
        import datetime as _dt
        d = os.path.join(config.DATA_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, f"run_{_dt.date.today():%Y%m%d}.log"), "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== {_dt.datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(sys.argv[1:])} =====\n")
        sys.stdout = sys.stderr = f


def main(argv=None):
    _log_when_windowless()
    ap = argparse.ArgumentParser(prog="analyzer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze"); a.add_argument("codes", nargs="+"); a.add_argument("--base-year", type=int); a.add_argument("--offline", action="store_true")
    u = sub.add_parser("update-all"); u.add_argument("--base-year", type=int); u.add_argument("codes", nargs="*")
    r = sub.add_parser("report"); r.add_argument("code"); r.add_argument("--base-year", type=int)
    s = sub.add_parser("status"); s.add_argument("code")
    c = sub.add_parser("import-consensus"); c.add_argument("code"); c.add_argument("csv")
    v = sub.add_parser("reverify"); v.add_argument("code"); v.add_argument("--year", type=int)
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.cmd == "analyze":
        conn = db.connect(); codes = [resolve(conn, c) for c in args.codes]; conn.close()
        for code in codes:   # 여러 종목을 한 번에 추가할 수 있음
            do_analyze(code, args.base_year, args.offline)
    elif args.cmd == "update-all":
        conn = db.connect()
        codes = args.codes or [r["stock_code"] for r in conn.execute("SELECT stock_code FROM companies")]
        conn.close()
        if not codes:
            raise SystemExit("DB에 기업이 없습니다. 종목코드를 함께 입력하세요. 예: update-all 213500")
        summary = {}
        for code in codes:
            res = do_analyze(code, args.base_year)
            summary[code] = res["stats"]
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        json.dump(summary, open(os.path.join(config.OUTPUT_DIR, "last_update.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    elif args.cmd == "report":
        conn = db.connect()
        out = os.path.join(config.OUTPUT_DIR, f"{args.code}.html")
        report.render(report.payload(conn, args.code, args.base_year), out)
        print(out)
    elif args.cmd == "status":
        conn = db.connect()
        for row in conn.execute("SELECT status, COUNT(*) n FROM facts WHERE stock_code=? AND is_current=1 GROUP BY status",
                                (args.code,)):
            print(f"{row['status']:<11} {row['n']}")
        print("컨센서스 스냅숏:", conn.execute("SELECT COUNT(DISTINCT as_of) FROM consensus WHERE stock_code=?", (args.code,)).fetchone()[0])
        print("변경 이력:", len(db.history(conn, args.code)))
    elif args.cmd == "import-consensus":
        from .collectors import manual
        conn = db.connect()
        n = pipeline.ingest_consensus(conn, args.code, manual.read_consensus_csv(args.csv), "manual", None, args.csv)
        print(f"새 컨센서스 {n}건 저장(이미 있던 같은 시점 자료는 건너뜀)")
    elif args.cmd == "reverify":
        conn = db.connect()
        q, p = "SELECT id FROM facts WHERE stock_code=? AND is_current=1 AND source!='derived'", [args.code]
        if args.year:
            q += " AND fiscal_year=?"; p.append(args.year)
        ids = [r["id"] for r in conn.execute(q, p)]
        for i in ids:
            db.set_status(conn, i, "STALE", "사용자 재검증 요청")
        pat = f"{args.code}:{args.year}" if args.year else f"{args.code}"
        conn.execute("DELETE FROM fetch_log WHERE (key=? OR key LIKE ?) AND source IN "
                     "('dart_report','dart_shares','dart_disclosures')", (pat, pat + ":%"))
        conn.commit()
        print(f"{len(ids)}개 값을 STALE로 표시했습니다. 다음 analyze 실행 때 해당 자료를 다시 받아 비교합니다.")
    elif args.cmd == "serve":
        from . import server
        server.serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
