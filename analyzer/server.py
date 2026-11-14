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
    timeout = 60          # 아무것도 보내지 않고 연결만 붙잡는 기기는 60초 뒤 끊음

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")   # 새 버전 화면이 바로 보이도록 브라우저 저장본을 쓰지 않음
        self._sec_headers()
        self.end_headers()
        self.wfile.write(b)

    def _sec_headers(self):
        # 다른 사이트가 이 화면을 틀 안에 넣거나(클릭 가로채기) 파일 종류를 바꿔 읽지 못하게
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")   # 같은 사이트 안에서만 출처를 보냄(로그인 출처 확인에 필요)

    def _redirect(self, loc, cookie=None):
        self.send_response(303)
        self.send_header("Location", loc)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self._sec_headers()
        self.end_headers()

    def _gate(self, method, u):
        """이 PC(127.0.0.1)는 그대로. 다른 기기는 https 포트 + 집 안 네트워크 + 로그인 + (보기 전용이면 읽기만).
        막았으면 False(응답은 이미 보냄)."""
        from . import lan
        ip = self.client_address[0]
        tls = getattr(self.server, "is_tls", False)
        if lan.is_local(ip) and not tls:
            return True
        if not tls or not lan.enabled() or not lan.is_home_network(ip):
            if method == "POST":
                self._drain()
            self._send(403, "<p>이 PC 밖에서의 접속은 허용되지 않았습니다.</p>")
            return False
        if method == "POST" and not self._same_origin():
            lan.log_access(ip, f"다른 출처의 요청 거부 {u.path}")
            self._drain()
            self._send(403, json.dumps({"error": "다른 사이트에서 온 요청은 받지 않습니다"}, ensure_ascii=False), "application/json")
            return False
        if u.path == "/login":
            if method == "GET":
                self._login_page((urllib.parse.parse_qs(u.query).get("next") or ["/"])[0])
                return False
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode("utf-8", "replace")) if n else {}
            nxt = (form.get("next") or ["/"])[0]
            who = lan.locked_out(ip)
            if who:
                self._login_page(nxt, f"비밀번호를 여러 번 틀려 {who}의 로그인을 10분 동안 막았습니다.")
                return False
            tok = lan.login(ip, (form.get("pw") or [""])[0])
            if not tok:
                self._login_page(nxt, "비밀번호가 맞지 않습니다.")
                return False
            self._redirect(self._safe_next(nxt), f"{lan.COOKIE}={tok}; Path=/; Max-Age={lan.SESSION_DAYS * 86400}; Secure; HttpOnly; SameSite=Strict")
            return False
        tok = lan.cookie_token(self.headers.get("Cookie"))
        if u.path == "/logout":
            lan.logout(tok)
            lan.log_access(ip, "로그아웃")
            self._redirect("/login", f"{lan.COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
            return False
        if not lan.valid(tok):
            if method == "GET" and not u.path.startswith("/api/"):
                self._redirect("/login?next=" + urllib.parse.quote(u.path + (("?" + u.query) if u.query else "")))
            else:
                self._drain()
                self._send(401, json.dumps({"error": "로그인이 필요합니다(화면을 새로 고치세요)"}, ensure_ascii=False), "application/json")
            return False
        if method == "POST" and not lan.allow_write():
            self._drain()
            self._send(403, json.dumps({"error": "태블릿 접속은 보기 전용으로 설정되어 있습니다(PC에서 하세요)"}, ensure_ascii=False), "application/json")
            return False
        return True

    def _drain(self):
        """거부하기 전에 남은 요청 본문을 버린다(안 읽고 닫으면 브라우저에 안내 대신 연결 오류가 보임)"""
        n = int(self.headers.get("Content-Length") or 0)
        if 0 < n <= 1_000_000:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _same_origin(self):
        host = self.headers.get("Host") or ""
        org = self.headers.get("Origin")
        if org:
            return org == f"https://{host}"
        ref = self.headers.get("Referer") or ""
        return ref.startswith(f"https://{host}/")

    @staticmethod
    def _safe_next(nxt):
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"

    def _login_page(self, nxt, err=None):
        from . import lan
        import html as _h
        page = lan.LOGIN_PAGE.replace("__NEXT__", _h.escape(nxt, quote=True)).replace(
            "__ERR__", f'<p class="err">{_h.escape(err)}</p>' if err else "")
        self._send(200 if not err else 401, page)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not self._gate("GET", u):
            return
        q = urllib.parse.parse_qs(u.query)
        by = int(q["base_year"][0]) if q.get("base_year") else None
        conn = db.connect()
        try:
            if u.path == "/api/whoami":
                from . import lan
                remote = getattr(self.server, "is_tls", False) and not lan.is_local(self.client_address[0])
                return self._send(200, json.dumps({"remote": bool(remote), "readonly": bool(remote and not lan.allow_write())}),
                                  "application/json")
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
        if not self._gate("POST", u):
            return
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
        if u.path == "/api/open-data" and not __import__("analyzer.lan", fromlist=["is_local"]).is_local(self.client_address[0]):
            return self._send(403, json.dumps({"error": "PC에서만 열 수 있습니다", "path": config.DATA_DIR}, ensure_ascii=False), "application/json")
        if u.path == "/api/open-data":
            # 데이터 폴더를 윈도우 탐색기로 연다(이 PC에서만 동작)
            try:
                if hasattr(os, "startfile"):
                    os.startfile(config.DATA_DIR)
                    return self._send(200, json.dumps({"opened": True, "path": config.DATA_DIR}, ensure_ascii=False), "application/json")
                return self._send(200, json.dumps({"opened": False, "path": config.DATA_DIR}, ensure_ascii=False), "application/json")
            except OSError as e:
                return self._send(200, json.dumps({"opened": False, "path": config.DATA_DIR, "error": str(e)}, ensure_ascii=False), "application/json")
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
                    pick = body.get("codes")          # 'DB에 있는 기업'에서 체크한 곳만 갱신(2026-11-13)
                    if pick is not None:
                        want = {str(c).strip().upper() for c in pick}
                        codes = [(c, nm) for c, nm in codes if c in want]
                        kind = "선택 갱신"
                        if not codes:
                            return self._send(400, json.dumps({"error": "고른 기업이 DB에 없습니다"}, ensure_ascii=False), "application/json")
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


class TLSServer(Server):
    """암호화 연결 맺기(핸드셰이크)를 접속마다 따로 처리해, 느린 기기 하나가 다른 접속을 막지 못하게 한다."""
    is_tls = True

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        import ssl
        request.settimeout(15)
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            request.close()
            return
        self.RequestHandlerClass(tls, client_address, self)


def _https_server(port):
    """태블릿용 https 서버(0.0.0.0:8766). 인증서가 없으면 만든다."""
    import ssl
    from . import lan, tlscert
    cert, key, _ = tlscert.ensure(lan.local_addresses())
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return TLSServer(("0.0.0.0", port), H, ctx)


def serve(port=8765):
    """PC용 http(127.0.0.1만)와, 태블릿 접속을 켰으면 https(8766)를 함께 연다.
    포트가 잠깐 잡혀 있으면(이전 서버 종료 중) 최대 15초 동안 다시 시도한다."""
    import time
    from . import __version__
    from . import lan
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
    if lan.enabled():
        tport = port + 1 if port != 8765 else lan.HTTPS_PORT
        try:
            hs = None
            for _ in range(15):
                try:
                    hs = _https_server(tport)
                    break
                except OSError as e:
                    last = e
                    time.sleep(1)
            if hs is None:
                raise last
            threading.Thread(target=hs.serve_forever, daemon=True).start()
            print("태블릿 접속 켜짐(https, 비밀번호 필요" + ("" if lan.allow_write() else ", 보기 전용") + "): " +
                  ", ".join(f"https://{ip}:{tport}" for ip in lan.local_addresses()), flush=True)
        except Exception as e:      # noqa: BLE001  태블릿 쪽이 실패해도 PC 화면은 연다
            print(f"[경고] 태블릿 접속(https {tport}) 서버를 열지 못했습니다: {e}", flush=True)
    httpd.serve_forever()
