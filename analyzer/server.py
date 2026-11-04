"""표준 라이브러리만 쓰는 로컬 웹 서버.  python -m analyzer serve  →  http://localhost:8765"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, pipeline, report


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
