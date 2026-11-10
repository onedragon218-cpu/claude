"""표준 라이브러리만 쓰는 로컬 웹 서버.  python -m analyzer serve  →  http://localhost:8765"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, pipeline, report
import threading
import traceback

# 전체 갱신 작업 상태(서버 안에서 하나만 돈다)
JOB = {"running": False, "total": 0, "done": 0, "current": None, "results": [], "started_at": None,
       "finished_at": None, "fresh": False, "kind": "전체 갱신", "stop": False, "stopped": False}
JOB_LOCK = threading.Lock()


def _update_all(codes, base_year, fresh):
    for code, name in codes:
        with JOB_LOCK:
            if JOB.get("stop"):            # 사용자가 중지를 누르면 지금 기업까지만 하고 멈춤
                JOB["stopped"] = True
                break
            JOB["current"] = f"{name or ''}({code})"
        item = {"code": code, "name": name}
        try:
            conn = db.connect()
            res = pipeline.analyze(code, base_year, conn=conn, fresh=fresh)
            report.render(report.payload(conn, code, base_year, last_run=res["stats"]),
                          os.path.join(config.OUTPUT_DIR, f"{code}.html"))
            conn.close()
            st = res["stats"]
            item.update(ok=True, requests=st["total_requests"], new=st["new_facts"], changed=st["changed_facts"],
                        notes=[n for n in st["notes"] if "수집 필요" not in n][:6])
        except Exception as e:  # 한 기업이 실패해도 나머지는 계속
            item.update(ok=False, error=repr(e), trace=traceback.format_exc()[-800:])
        with JOB_LOCK:
            JOB["results"].append(item)
            JOB["done"] += 1
    with JOB_LOCK:
        JOB["running"] = False
        JOB["current"] = None
        JOB["finished_at"] = db.now()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")   # 새 버전 화면이 바로 보이도록 브라우저 저장본을 쓰지 않음
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        by = int(q["base_year"][0]) if q.get("base_year") else None
        conn = db.connect()
        try:
            if u.path == "/api/version":
                from . import __version__
                return self._send(200, json.dumps({"version": __version__, "root": config.ROOT, "data": config.DATA_DIR},
                                                  ensure_ascii=False), "application/json")
            if u.path == "/api/scan":
                from . import screener
                with screener.LOCK:
                    job = dict(screener.JOB)
                res = screener.results(conn) if q.get("results", ["0"])[0] == "1" else None
                return self._send(200, json.dumps({"job": job, "results": res}, ensure_ascii=False), "application/json")
            if u.path == "/api/job":
                with JOB_LOCK:
                    return self._send(200, json.dumps(JOB, ensure_ascii=False), "application/json")
            if u.path == "/api/search":
                from . import search
                res = search.search(conn, q.get("q", [""])[0], limit=12)
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json")
            if u.path.startswith("/company/"):
                code = u.path.split("/")[2]
                return self._send(200, report.render(report.payload(conn, code, by)))
            if u.path == "/":
                row = conn.execute("SELECT stock_code FROM companies ORDER BY updated_at DESC LIMIT 1").fetchone()
                if row:
                    return self._send(200, report.render(report.payload(conn, row["stock_code"], by)))
                return self._send(200, "<p>DB가 비어 있습니다. <code>python -m analyzer analyze 213500</code>을 먼저 실행하세요.</p>")
            self._send(404, "not found")
        finally:
            conn.close()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/scan":
            from . import screener
            started = screener.start(full=q.get("full", ["0"])[0] == "1")
            with screener.LOCK:
                job = dict(screener.JOB)
            return self._send(200, json.dumps({"started": started, "job": job}, ensure_ascii=False), "application/json")
        if u.path == "/api/universe/add":
            from . import universe
            n = int(self.headers.get("Content-Length") or 0)
            items = json.loads(self.rfile.read(n).decode("utf-8") or "[]") if n else []
            added = universe.add([(c, nm) for c, nm in items])
            return self._send(200, json.dumps({"added": added, "total": len(universe.load())}, ensure_ascii=False),
                              "application/json")
        if u.path == "/api/segments/keywords":
            from . import segments
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            code = str(d.get("code") or "")
            if not (code.isdigit() and len(code) == 6):
                return self._send(400, json.dumps({"error": "종목코드 오류"}, ensure_ascii=False), "application/json")
            clean = lambda m: {str(k)[:60]: [str(x).strip()[:30] for x in (v or []) if str(x).strip()][:20]
                               for k, v in (m or {}).items()}
            segments.save_user_keywords(code, clean(d.get("segments")), clean(d.get("topics")))
            return self._send(200, json.dumps({"saved": True}, ensure_ascii=False), "application/json")
        if u.path == "/api/good6/manual":
            from . import goodco
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            code = str(d.get("code") or "")
            if not (code.isdigit() and len(code) == 6):
                return self._send(400, json.dumps({"error": "종목코드 오류"}, ensure_ascii=False), "application/json")
            try:
                goodco.save_manual(code, d.get("mark"), d.get("text"), d.get("four_note"))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
            return self._send(200, json.dumps({"saved": True}, ensure_ascii=False), "application/json")
        if u.path == "/api/job/stop":
            with JOB_LOCK:
                if JOB["running"]:
                    JOB["stop"] = True
                job = dict(JOB)
            return self._send(200, json.dumps(job, ensure_ascii=False), "application/json")
        if u.path in ("/api/update-all", "/api/collect-list"):
            # /api/update-all = 'DB에 있는 기업' 전체 갱신, /api/collect-list = 기본 종목 목록에서 고른 기업만 수집
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            with JOB_LOCK:
                if JOB["running"]:
                    return self._send(200, json.dumps(dict(JOB, already=True), ensure_ascii=False), "application/json")
                if u.path == "/api/collect-list":
                    seen, codes = set(), []
                    for it in body.get("codes") or []:
                        c, nm = (str(it[0]), str(it[1])) if isinstance(it, (list, tuple)) else (str(it), "")
                        c = c.strip().upper()
                        if c not in seen and len(c) == 6 and c.isalnum():
                            seen.add(c); codes.append((c, nm))
                    kind = "목록 기업 수집"
                    if not codes:
                        return self._send(400, json.dumps({"error": "고른 종목이 없습니다"}, ensure_ascii=False), "application/json")
                else:
                    conn = db.connect()
                    codes = [(r["stock_code"], r["name"]) for r in
                             conn.execute("SELECT stock_code, name FROM companies ORDER BY name")]
                    conn.close()
                    kind = "전체 갱신"
                by = int(q["base_year"][0]) if q.get("base_year") else None
                fresh = q.get("fresh", ["0"])[0] == "1"
                JOB.update(running=True, total=len(codes), done=0, current=None, results=[], started_at=db.now(),
                           finished_at=None, fresh=fresh, kind=kind, stop=False, stopped=False,
                           targets=[f"{nm}({c})" for c, nm in codes[:50]])
            threading.Thread(target=_update_all, args=(codes, by, fresh), daemon=True).start()
            return self._send(200, json.dumps(JOB, ensure_ascii=False), "application/json")
        if u.path != "/api/analyze":
            return self._send(404, "not found")
        try:
            from . import search
            conn = db.connect()
            try:
                code = search.resolve(conn, q.get("code", [""])[0])
            except search.Ambiguous as e:
                conn.close()
                return self._send(200, json.dumps({"error": str(e), "candidates": e.cands}, ensure_ascii=False),
                                  "application/json")
            except LookupError as e:
                conn.close()
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
            by = int(q["base_year"][0]) if q.get("base_year") else None
            res = pipeline.analyze(code, by, conn=conn)
            # 저장된 화면 파일도 함께 갱신(바로가기·목록 클릭용)
            report.render(report.payload(conn, code, by, last_run=res["stats"]),
                          os.path.join(config.OUTPUT_DIR, f"{code}.html"))
            conn.close()
            self._send(200, json.dumps({"code": code, "stats": res["stats"]}, ensure_ascii=False), "application/json")
        except SystemExit as e:
            self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
        except Exception as e:  # 화면에 원인을 보여준다
            self._send(500, json.dumps({"error": repr(e)}, ensure_ascii=False), "application/json")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # 윈도우에서 같은 포트를 두 프로그램이 함께 잡지 않도록


def serve(port=8765):
    """포트가 잠깐 잡혀 있으면(이전 서버 종료 중) 최대 15초 동안 다시 시도한다."""
    import time
    from . import __version__
    last = None
    for _ in range(15):
        try:
            httpd = Server(("127.0.0.1", port), H)
            break
        except OSError as e:
            last = e
            time.sleep(1)
    else:
        print(f"[오류] {port} 포트를 열 수 없습니다: {last}. 다른 프로그램이 이 포트를 쓰고 있는지 확인하세요.")
        raise SystemExit(2)
    print(f"화면 서버 시작: http://localhost:{port}  버전 {__version__}  데이터 {config.DATA_DIR}  (종료: Ctrl+C)", flush=True)
    httpd.serve_forever()
