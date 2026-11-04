"""PC 안에서만 열리는 화면 서버(127.0.0.1).

보안
- 127.0.0.1에만 묶는다. 같은 PC의 브라우저만 접속할 수 있다.
- 모든 /api 요청은 실행할 때마다 새로 만든 토큰(X-FT-Token)과 Host 머리글을 확인한다.
  다른 웹사이트가 몰래 이 서버를 호출하는 것을 막기 위해서다.
- 인증키는 받아서 Windows 자격 증명 관리자에 넣기만 하고, 응답·로그에 되돌려 주지 않는다.
"""
import json, secrets, threading, traceback, uuid, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import dashboard, paths, secret, service, config
from .sources import dart

JOBS, LOCK = {}, threading.Lock()


class App:
    def __init__(self, port):
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.w = paths.W(paths.workdir())

    def state(self):
        return {"workdir": self.w.root, "key_set": bool(secret.get_dart_key()), "key_where": secret.where(),
                "companies": service.companies(self.w), "base_year": config.periods()["base"],
                "settings_file": paths.settings_path()}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):   # 요청 기록은 화면에 찍지 않음(키가 섞일 일 차단)
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _ok_origin(self):
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                return False
            return self.path.startswith("/api/") is False or self.headers.get("X-FT-Token") == app.token

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return {}

        def do_GET(self):
            if not self._ok_origin():
                return self._send(403, {"error": "허용되지 않은 요청"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, dashboard._inline_vendor(dashboard.full_document(dashboard.render_html(token=app.token))),
                                      "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._send(200, app.state())
                if u.path == "/api/search":
                    return self._send(200, self._search(q.get("q", "")))
                if u.path.startswith("/api/company/"):
                    code = u.path.rsplit("/", 1)[-1]
                    d = service.company_data(app.w, code, int(q["base"]) if q.get("base") else None)
                    return self._send(200 if d else 404, d or {"error": "아직 분석하지 않은 종목"})
                if u.path.startswith("/api/job/"):
                    j = JOBS.get(u.path.rsplit("/", 1)[-1])
                    return self._send(200 if j else 404, j or {"error": "작업 없음"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "없는 주소"})

        def do_POST(self):
            if not self._ok_origin():
                return self._send(403, {"error": "허용되지 않은 요청"})
            u = urlparse(self.path)
            b = self._body()
            try:
                if u.path == "/api/analyze":
                    code = str(b.get("code", "")).strip()
                    if not (len(code) == 6 and code.isalnum()):
                        return self._send(400, {"error": "6자리 종목코드가 필요합니다"})
                    return self._send(200, {"job": start_job(app, code, bool(b.get("refresh")), b.get("base"))})
                if u.path == "/api/key":
                    try:
                        secret.set_dart_key(b.get("key", ""))
                    except (ValueError, RuntimeError) as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True, "key_where": secret.where()})
                if u.path == "/api/key/delete":
                    secret.delete_dart_key()
                    return self._send(200, {"ok": True})
                if u.path == "/api/workdir":
                    d = b.get("path") or paths.pick_folder_dialog()
                    if not d:
                        return self._send(400, {"error": "폴더를 고르지 않았습니다"})
                    app.w = paths.W(paths.set_workdir(d))
                    return self._send(200, app.state())
                if u.path == "/api/sample":
                    codes = service.import_sample(app.w)
                    return self._send(200, {"job": start_job(app, codes[0], False, None, offline=True)})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "없는 주소"})

        def _search(self, q):
            q = q.strip()
            if not q:
                return {"items": []}
            local = [c for c in service.companies(app.w) if q in c["code"] or q.replace(" ", "") in c["name"].replace(" ", "")]
            items = [{"stock_code": c["code"], "name": c["name"], "analyzed": True} for c in local]
            if not secret.get_dart_key():
                return {"items": items, "note": "DART 인증키를 등록하면 전체 상장사에서 찾습니다"}
            try:
                for h in dart.search(app.w, q):
                    if not any(i["stock_code"] == h["stock_code"] for i in items):
                        items.append({"stock_code": h["stock_code"], "name": h["name"], "analyzed": False})
            except Exception as e:
                return {"items": items, "note": str(e)}
            return {"items": items[:15]}
    return H


def start_job(app, code, refresh, base, offline=False):
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "code": code, "status": "running", "lines": [], "result": None, "error": None}
    JOBS[jid] = job

    def work():
        with LOCK:      # 한 번에 한 종목씩(DB 쓰기 충돌 방지)
            try:
                job["result"] = service.analyze(app.w, code, base_year=int(base) if base else None, refresh=refresh,
                                                offline=offline, progress=job["lines"].append,
                                                settings=paths.load_settings())
                job["status"] = "done"
            except Exception as e:
                job["error"] = f"{type(e).__name__}: {e}"
                job["lines"].append("오류: " + job["error"])
                job["trace"] = traceback.format_exc()[-1500:]
                job["status"] = "error"
    threading.Thread(target=work, daemon=True).start()
    return jid


def serve(port=8765, open_browser=True):
    app = App(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print(f"fintrack 화면: {url}\n작업 폴더: {app.w.root}\n끝내려면 이 창을 닫거나 Ctrl+C")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
