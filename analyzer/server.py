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
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        by = int(q["base_year"][0]) if q.get("base_year") else None
        conn = db.connect()
        try:
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


def serve(port=8765):
    print(f"http://localhost:{port}  (종료: Ctrl+C)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
