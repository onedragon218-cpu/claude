"""표준 라이브러리만 쓰는 로컬 웹 서버.  python -m analyzer serve  →  http://localhost:8765"""
import html
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


def _update_all(codes, base_year, fresh, mode="full"):
    from . import pcache, collectlock
    try:
        with pcache.batch(), collectlock.keepalive():
            _update_loop(codes, base_year, fresh, mode)
    finally:
        collectlock.release()
        COLLECTOR_PAUSE.clear()            # 수동 갱신이 끝나면 자동 수집기가 다시 이어서
    with JOB_LOCK:
        JOB["running"] = False
        JOB["current"] = None
        JOB["finished_at"] = db.now()


def _update_loop(codes, base_year, fresh, mode="full"):
    import time as _t
    from . import collectlock
    for i, (code, name) in enumerate(codes):
        with JOB_LOCK:
            if JOB.get("stop"):            # 사용자가 중지를 누르면 지금 기업까지만 하고 멈춤
                JOB["stopped"] = True
                break
            JOB["current"] = f"{name or ''}({code})"
        collectlock.heartbeat(current=f"{name or ''}({code})", done=i, total=len(codes), kind=JOB.get("kind"))
        item = {"code": code, "name": name}
        if mode == "price":                # [주가만 새로 받기]: 주가·파생값만
            try:
                t0 = _t.time()
                conn = db.connect()
                res = pipeline.update_price(code, base_year, conn=conn)
                conn.close()
                st = res["stats"]
                item.update(ok=res["ok"], requests=st["total_requests"], new=st["new_facts"], changed=st["changed_facts"],
                            notes=st["notes"][:3], sec=round(_t.time() - t0, 1), timing=st.get("timing"),
                            **({} if res["ok"] else {"error": "; ".join(st["notes"][:2]) or "주가를 받지 못함"}))
            except Exception as e:  # noqa: BLE001
                item.update(ok=False, error=repr(e))
            with JOB_LOCK:
                JOB["results"].append(item)
                JOB["done"] += 1
            continue
        try:
            t0 = _t.time()
            conn = db.connect()
            res = pipeline.analyze(code, base_year, conn=conn, fresh=fresh)
            t1 = _t.time()
            report.render(report.payload(conn, code, base_year, last_run=res["stats"]),
                          os.path.join(config.OUTPUT_DIR, f"{code}.html"))
            collectlock.mark_done(conn, code, "화면")
            conn.close()
            st = res["stats"]
            tm = dict(st.get("timing") or {})
            tm["화면 파일 저장"] = round(_t.time() - t1, 2)
            item.update(ok=True, requests=st["total_requests"], new=st["new_facts"], changed=st["changed_facts"],
                        notes=[n for n in st["notes"] if "수집 필요" not in n][:6],
                        sec=round(_t.time() - t0, 1), timing=dict(sorted(tm.items(), key=lambda x: -x[1])))
        except Exception as e:  # 한 기업이 실패해도 나머지는 계속
            item.update(ok=False, error=repr(e), trace=traceback.format_exc()[-800:])
        with JOB_LOCK:
            JOB["results"].append(item)
            JOB["done"] += 1


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
        try:
            return self._get(u, q, by)
        except Exception as e:     # noqa: BLE001  어떤 오류든 빈 화면 대신 이유를 보여 줌
            import traceback
            busy = "locked" in str(e) or "busy" in str(e)
            msg = ("자료를 받는 중이라 DB가 잠시 바쁩니다. 5초 뒤 저절로 다시 엽니다." if busy else f"화면을 만들다 오류가 났습니다: {type(e).__name__}: {e}")
            if u.path.startswith("/api/"):
                return self._send(503 if busy else 500, json.dumps({"error": msg}, ensure_ascii=False), "application/json")
            extra = ""
            if busy:
                from . import collectlock
                st = collectlock.status() or {}
                if True:
                    try:
                        db.set_wal(wait=0)       # 이 틈에 WAL로 바꿔 보기(바뀌면 다음부터는 수집 중에도 열림)
                    except Exception:      # noqa: BLE001
                        pass
                who = (f"<p>지금 {html.escape(str(st.get('kind') or '수집'))} 중: {st.get('done', 0)}/{st.get('total', '?')}곳"
                       f"{(' · ' + html.escape(str(st.get('current')))) if st.get('current') else ''}</p>") if st else ""
                extra = (who + "<p>1분 넘게 계속되면 아래 버튼으로 백그라운드 수집을 멈추세요. 지금 회사까지만 하고 멈추며, 다음 로그인·평일 07:13에 이어서 받습니다.</p>"
                         "<button onclick=\"fetch('/api/collect/stop',{method:'POST'}).then(r=>r.json()).then(j=>{document.getElementById('m').textContent=j.message})\">백그라운드 수집 멈추기</button> "
                         "<span id='m' style='color:#555'></span>"
                         "<p style='color:#555'>버튼으로 안 되면: Windows 검색에서 '작업 스케줄러' → 왼쪽 '작업 스케줄러 라이브러리' → <b>CorpAnalyzer-Update</b>를 마우스 오른쪽 → <b>끝내기</b>.</p>")
            body = (f"<!doctype html><meta charset='utf-8'>{'<meta http-equiv=refresh content=5>' if busy else ''}"
                    f"<div style='font-family:sans-serif;max-width:760px;margin:40px auto;line-height:1.6'><h2>{html.escape(msg)}</h2>{extra}"
                    + ("" if busy else f"<p>아래 내용을 그대로 복사해 알려 주세요.</p><pre style='white-space:pre-wrap;background:#f4f4f2;padding:12px'>{html.escape(traceback.format_exc()[-2500:])}</pre>")
                    + "</div>")
            return self._send(503 if busy else 500, body)

    def _get(self, u, q, by):
        conn = db.connect(timeout=8)          # 읽기는 조금 기다림(옛 방식에서 수집이 저장하는 순간 잠깐 막힘). 쓰기는 가치평가에서 짧게 시도하고 건너뜀
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
            if u.path == "/api/company_status":
                # A탭: 회사마다 공시·뉴스·컨센서스 받은 때(수집이 도는 동안 화면을 새로 고치지 않아도 칸이 바뀌게)
                from . import collectlock
                return self._send(200, json.dumps({"status": collectlock.company_status(conn), "done_today": collectlock.done_today(conn)},
                                                  ensure_ascii=False), "application/json")
            if u.path == "/api/job":
                from . import collectlock
                with JOB_LOCK:
                    j = dict(JOB)
                bg = collectlock.status()
                n_all = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                j["prep"] = dict(pipeline.PREP)
                j["collector"] = dict(COLLECTOR)
                j["collect"] = {"lock": bg if bg and bg.get("who") == "background" else None,
                                "done_today": len(collectlock.done_today(conn)), "total": n_all}
                return self._send(200, json.dumps(j, ensure_ascii=False), "application/json")
            if u.path == "/api/hj/request":
                # 화면 T: Claude에 올릴 한장조사 요청 글(마크다운, 10곳까지). 작업 폴더에는 쓰지 않고 내려받기로만
                from . import hanjang
                hanjang.ensure(conn)
                P = config.periods(by)
                codes = [c for c in q.get("codes", [""])[0].split(",") if c]
                purpose = "refresh" if q.get("purpose", ["new"])[0] == "refresh" else "new"
                text, used = hanjang.request_md(conn, codes, P, purpose)
                if not used:
                    return self._send(400, json.dumps({"error": "회사가 없습니다"}, ensure_ascii=False), "application/json")
                return self._send(200, text, "text/markdown; charset=utf-8")
            if u.path == "/api/nodes/export":
                from . import nodeview
                P = config.periods(by)
                if q.get("scope", ["one"])[0] == "all":
                    codes = [r["stock_code"] for r in conn.execute("SELECT c.stock_code FROM companies c WHERE EXISTS "
                                                                   "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code) ORDER BY c.name")]
                else:
                    codes = [q.get("code", [""])[0]]
                return self._send(200, json.dumps(nodeview.export(conn, codes, P), ensure_ascii=False, indent=1), "application/json")
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
        if u.path in ("/api/nodes/link", "/api/nodes/review"):
            # 화면 R: 사업부 ↔ 노드 직접 연결 · Claude 검토 결과 넣기(PC에서만)
            from . import lan, nodeview
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if 0 < n < 5_000_000 else "{}"
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            try:
                d = json.loads(raw or "{}")
            except ValueError as e:
                return self._send(200, json.dumps({"error": f"JSON을 읽지 못했습니다: {e}"}, ensure_ascii=False), "application/json")
            if u.path == "/api/nodes/link":
                code, seg, nid = str(d.get("code") or ""), str(d.get("segment") or ""), d.get("node") or None
                K = nodeview.knowledge()
                if not (code.isalnum() and len(code) == 6) or not seg or (nid and nid not in K["nodes"]):
                    return self._send(400, json.dumps({"error": "입력 오류"}, ensure_ascii=False), "application/json")
                conn = db.connect()
                try:
                    nodeview.set_link(conn, code, seg, nid)
                finally:
                    conn.close()
                return self._send(200, json.dumps({"saved": True}, ensure_ascii=False), "application/json")
            body = d.get("review") if isinstance(d.get("review"), dict) else d
            if isinstance(d.get("text"), str):          # 붙여 넣은 글: 앞뒤 설명·```json 표시를 걷어 내고 JSON만
                t = d["text"]
                i, j = t.find("{"), t.rfind("}")
                try:
                    body = json.loads(t[i:j + 1]) if i >= 0 and j > i else None
                except ValueError as e:
                    return self._send(200, json.dumps({"error": f"JSON을 읽지 못했습니다: {e}"}, ensure_ascii=False), "application/json")
            try:
                clean, notes = nodeview.validate_review(body)
            except ValueError as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
            got = {k: len(clean[k]) for k in ("themes", "nodes", "links", "verdicts")}
            if not any(got.values()):
                return self._send(200, json.dumps({"error": "넣을 내용이 없습니다", "notes": notes}, ensure_ascii=False), "application/json")
            total = nodeview.save_review(clean)
            return self._send(200, json.dumps({"saved": got, "total": total, "notes": notes}, ensure_ascii=False), "application/json")
        if u.path == "/api/collect/stop":
            from . import collectlock, lan
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            self._drain()
            import time as _t
            COLLECTOR["stopped_until"] = _t.time() + 3600      # 화면 서버 안 자동 수집기도 1시간 쉼
            ok = collectlock.request_stop()
            return self._send(200, json.dumps({"ok": ok, "message": "멈춤을 요청했습니다. 지금 회사가 끝나면 멈추고 화면이 저절로 열립니다(1~2분)." if ok
                                               else "돌고 있는 백그라운드 수집을 찾지 못했습니다. 아래 안내대로 작업 스케줄러에서 멈춰 주세요."}, ensure_ascii=False), "application/json")
        if u.path == "/api/refetch":
            # B탭 [다시 받기]: 실패한 출처만 기다리는 기간을 무시하고 지금 다시 받기(PC에서만)
            from . import lan
            n = int(self.headers.get("Content-Length") or 0)
            if not lan.is_local(self.client_address[0]):
                self._drain()
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n < 100_000 else {}
            except ValueError:
                d = {}
            code = str(d.get("code") or "")
            if not (code.isalnum() and len(code) == 6):
                return self._send(400, json.dumps({"error": "종목코드 오류"}, ensure_ascii=False), "application/json")
            with JOB_LOCK:
                if JOB.get("running"):
                    return self._send(200, json.dumps({"error": "전체 갱신이 돌고 있습니다. 끝난 뒤 눌러 주세요"}, ensure_ascii=False), "application/json")
            srcs = [x for x in (d.get("sources") or []) if x in pipeline.REFETCH]
            if not srcs:
                return self._send(200, json.dumps({"error": "다시 받을 수 있는 출처가 아닙니다"}, ensure_ascii=False), "application/json")
            try:
                res = pipeline.refetch(code, srcs, int(q["base_year"][0]) if q.get("base_year") else None)
            except Exception as e:     # noqa: BLE001
                return self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}, ensure_ascii=False), "application/json")
            return self._send(200, json.dumps({"ok": True, "result": res}, ensure_ascii=False), "application/json")
        if u.path == "/api/hj/calib":
            # 화면 T 점검: 앱 참고 판정을 Claude 판정에 맞추기 — 진단·배우기·검증(검증만, 화면 판정은 바꾸지 않음)
            from . import hj_calib, lan
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            self._drain()
            conn = db.connect()
            try:
                P = config.periods(int(q["base_year"][0]) if q.get("base_year") else None)
                res = hj_calib.run(conn, P)
            except Exception as e:     # noqa: BLE001
                return self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False), "application/json")
            finally:
                conn.close()
            return self._send(200, json.dumps(res, ensure_ascii=False), "application/json")
        if u.path == "/api/hj/request_save":
            # 화면 T: Claude 요청 파일을 한장조사 폴더/_앱요청/에 새 파일로 저장(PC에서만, 70곳씩)
            from . import hanjang, lan
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n < 1_000_000 else {}
            except ValueError:
                d = {}
            conn = db.connect()
            try:
                hanjang.ensure(conn)
                P = config.periods(int(q["base_year"][0]) if q.get("base_year") else None)
                purpose = "refresh" if d.get("purpose") == "refresh" else "new"
                codes = [x["code"] for x in hanjang.db_not_in_claude(conn)] if d.get("scope") == "missing" else list(d.get("codes") or [])
                try:
                    rep = hanjang.save_requests(conn, codes, P, purpose)
                except (ValueError, OSError) as e:
                    return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
                return self._send(200, json.dumps(rep, ensure_ascii=False), "application/json")
            finally:
                conn.close()
        if u.path in ("/api/hj/folder", "/api/hj/sync"):
            # 화면 T: 작업 폴더 지정(PC에서만) · 지금 읽기
            from . import hanjang, lan
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 할 수 있습니다"}, ensure_ascii=False), "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n < 100_000 else {}
            except ValueError:
                d = {}
            conn = db.connect()
            try:
                if u.path == "/api/hj/folder":
                    try:
                        hanjang.folder_set(d.get("path", ""))
                    except (ValueError, OSError) as e:
                        return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json")
                rep = hanjang.sync_folder(conn, force=bool(d.get("force")) or u.path == "/api/hj/folder")
                return self._send(200, json.dumps(rep, ensure_ascii=False, default=str), "application/json")
            finally:
                conn.close()
        if u.path in ("/api/hj/run", "/api/hj/run_all", "/api/hj/import"):
            # 화면 T「노드 현재 판정」: 근거 모으기·앱 판정 · 전체 다시 계산 · Claude 조사 결과(JSON·zip) 넣기
            from . import hanjang, lan
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if 0 < n < 30_000_000 else b""
            if u.path == "/api/hj/import" and not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 넣을 수 있습니다"}, ensure_ascii=False), "application/json")
            P = config.periods(int(q["base_year"][0]) if q.get("base_year") else None)
            conn = db.connect()
            try:
                if u.path == "/api/hj/run":
                    code = q.get("code", [""])[0]
                    if not (code.isalnum() and len(code) == 6):
                        return self._send(400, json.dumps({"error": "종목코드 오류"}, ensure_ascii=False), "application/json")
                    with JOB_LOCK:
                        busy = JOB.get("running")
                    note = None
                    if q.get("collect", ["1"])[0] == "1" and not busy:
                        try:
                            pipeline.analyze(code, P["base_year"], conn=conn)       # 빠진 자료·새 공시·뉴스만 받음
                        except Exception as e:     # noqa: BLE001
                            note = f"수집 일부 실패: {type(e).__name__}: {e}"[:200]
                    doc, warns = hanjang.run_app(conn, code, P, collect_news=q.get("collect", ["1"])[0] == "1")
                    return self._send(200, json.dumps({"ok": True, "warnings": warns, "note": note}, ensure_ascii=False), "application/json")
                if u.path == "/api/hj/run_all":
                    codes = [r["stock_code"] for r in conn.execute("SELECT c.stock_code FROM companies c WHERE EXISTS "
                                                                   "(SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code)")]
                    bad = []
                    for c in codes:
                        try:
                            hanjang.run_app(conn, c, P)
                        except Exception as e:     # noqa: BLE001
                            bad.append(f"{c}: {type(e).__name__}")
                    try:                           # 앱이 모은 글이 쌓였으니 낱말 모델을 그 글로 다시 배움
                        from . import hj_learn
                        hj_learn.train(conn)
                    except Exception as e:     # noqa: BLE001
                        bad.append(f"낱말 모델 다시 배우기: {type(e).__name__}")
                    return self._send(200, json.dumps({"ok": True, "n": len(codes), "failed": bad, "agree": hanjang.agreement(conn)},
                                                      ensure_ascii=False), "application/json")
                # 넣기: zip(작업 폴더 한장조사) 또는 JSON(Claude 결과 — 앞뒤 설명 글은 걷어 냄)
                if raw[:2] == b"PK":
                    rep = hanjang.import_zip(conn, raw)
                    from . import hj_learn
                    rep["learn"] = hj_learn.train(conn)
                    return self._send(200, json.dumps({"ok": True, "zip": rep}, ensure_ascii=False), "application/json")
                t = raw.decode("utf-8-sig", "replace")
                i, j = min([x for x in (t.find("{"), t.find("[")) if x >= 0] or [-1]), max(t.rfind("}"), t.rfind("]"))
                try:
                    obj = json.loads(t[i:j + 1]) if i >= 0 and j > i else None
                except ValueError as e:
                    return self._send(200, json.dumps({"error": f"JSON을 읽지 못했습니다: {e}"}, ensure_ascii=False), "application/json")
                if obj is None:
                    return self._send(200, json.dumps({"error": "넣을 내용이 없습니다"}, ensure_ascii=False), "application/json")
                docs, notes = hanjang.validate_claude(obj)
                saved = {}
                for d in docs:
                    saved[d["code"]] = hanjang.save(conn, d, "claude", "Claude 조사 결과 넣기", "Claude")
                if docs:
                    from . import hj_learn
                    hj_learn.train(conn)                   # 새 Claude 판정으로 다시 학습
                return self._send(200, json.dumps({"ok": bool(docs), "saved": {k: len(v) for k, v in saved.items()}, "notes": notes,
                                                   "warnings": saved}, ensure_ascii=False), "application/json")
            finally:
                conn.close()
        if u.path == "/api/companies/delete":
            # 'DB에 있는 기업' 지우기(PC에서만). 지우기 전에 DB 파일을 backup\ 에 통째로 복사한다
            from . import lan
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n < 1_000_000 else {}
            if not lan.is_local(self.client_address[0]):
                return self._send(403, json.dumps({"error": "PC에서만 지울 수 있습니다"}, ensure_ascii=False), "application/json")
            codes = [str(c) for c in (d.get("codes") or [])][:500]
            if not codes or d.get("confirm") is not True:
                return self._send(400, json.dumps({"error": "지울 기업이 없거나 확인이 없습니다"}, ensure_ascii=False), "application/json")
            with JOB_LOCK:
                if JOB.get("running"):
                    return self._send(200, json.dumps({"error": "수집·갱신 작업이 진행 중입니다. 끝난 뒤 지워 주세요"}, ensure_ascii=False), "application/json")
            try:
                bk = db.backup()
            except Exception as e:      # noqa: BLE001  백업이 안 되면 지우지 않는다
                return self._send(200, json.dumps({"error": f"백업을 만들지 못해 지우지 않았습니다: {e}"}, ensure_ascii=False), "application/json")
            conn = db.connect()
            try:
                res = db.delete_companies(conn, codes)
            finally:
                conn.close()
            for c in codes:
                f = os.path.join(config.OUTPUT_DIR, f"{c}.html")
                if c.isalnum() and os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            return self._send(200, json.dumps({"deleted": res, "backup": bk, "codes": codes}, ensure_ascii=False), "application/json")
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
        if u.path in ("/api/update-all", "/api/collect-list", "/api/update-price"):
            # /api/update-all = 'DB에 있는 기업' 전체 갱신(오늘 아직 안 받은 곳만), /api/collect-list = 기본 종목 목록에서 고른 기업만 수집,
            # /api/update-price = 주가만 새로 받기(체크한 기업 또는 전체)
            from . import collectlock
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            st0 = collectlock.status()
            if st0 and st0.get("pid") == os.getpid() and st0.get("kind") == "자동 수집(화면 서버)":
                COLLECTOR_PAUSE.set()             # 화면 서버 안 자동 수집기는 지금 회사까지만 하고 차례를 넘김
                import time as _t
                for _ in range(120):              # 최대 2분
                    st1 = collectlock.status()
                    if not st1 or st1.get("pid") != os.getpid() or st1.get("kind") != "자동 수집(화면 서버)":
                        break
                    _t.sleep(1)
            with JOB_LOCK:
                if JOB["running"]:
                    return self._send(200, json.dumps(dict(JOB, already=True), ensure_ascii=False), "application/json")
                bg = collectlock.status()
                if bg and bg.get("who") == "background":
                    return self._send(200, json.dumps({"error": f"백그라운드 수집이 진행 중입니다({bg.get('done', 0)}/{bg.get('total', '?')}곳, 지금 {bg.get('current', '')}). "
                                                                "끝나면 다시 눌러 주세요 — 동시에 돌면 자료가 꼬일 수 있어 기다립니다."}, ensure_ascii=False), "application/json")
                mode = "price" if u.path == "/api/update-price" else "full"
                if mode == "price":
                    conn = db.connect()
                    codes = [(r["stock_code"], r["name"]) for r in conn.execute("SELECT stock_code, name FROM companies ORDER BY name")]
                    conn.close()
                    pick = body.get("codes")
                    if pick is not None:
                        want = {str(c).strip().upper() for c in pick}
                        codes = [(c, nm) for c, nm in codes if c in want]
                    if not codes:
                        return self._send(400, json.dumps({"error": "고른 기업이 없습니다"}, ensure_ascii=False), "application/json")
                    kind = "주가만 새로 받기" + ("(체크한 기업)" if pick is not None else "(전체)")
                    by = int(q["base_year"][0]) if q.get("base_year") else None
                    if not collectlock.acquire("server", kind=kind, total=len(codes)):
                        COLLECTOR_PAUSE.clear()
                    return self._send(200, json.dumps({"error": "다른 수집이 진행 중입니다. 잠시 뒤 다시 눌러 주세요"}, ensure_ascii=False), "application/json")
                    JOB.update(running=True, total=len(codes), done=0, current=None, results=[], started_at=db.now(),
                               finished_at=None, fresh=False, kind=kind, stop=False, stopped=False, targets=[f"{nm}({c})" for c, nm in codes[:50]])
                    threading.Thread(target=_update_all, args=(codes, by, False, "price"), daemon=True).start()
                    return self._send(200, json.dumps(JOB, ensure_ascii=False), "application/json")
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
                    fresh_q = q.get("fresh", ["0"])[0] == "1"
                    if pick is not None:
                        want = {str(c).strip().upper() for c in pick}
                        codes = [(c, nm) for c, nm in codes if c in want]
                        kind = "선택 갱신"
                        if not codes:
                            return self._send(400, json.dumps({"error": "고른 기업이 DB에 없습니다"}, ensure_ascii=False), "application/json")
                    elif not fresh_q:                 # 전체 갱신 = '갱신 필요'(기한 지난 공시·뉴스·컨센서스가 있는) 기업만
                        conn = db.connect()
                        n_all = len(codes)
                        due = set(collectlock.due_codes(conn))
                        codes = [x for x in codes if x[0] in due]
                        conn.close()
                        kind = f"갱신 필요한 기업 갱신({len(codes)}곳 / 전체 {n_all}곳)"
                        if not codes:
                            return self._send(200, json.dumps({"error": f"{n_all}곳 모두 기한 안에 받아 둔 상태라 갱신할 것이 없습니다. 주가는 [주가만 새로 받기]로 받으세요."}, ensure_ascii=False), "application/json")
                by = int(q["base_year"][0]) if q.get("base_year") else None
                fresh = q.get("fresh", ["0"])[0] == "1"
                if not collectlock.acquire("server", kind=kind, total=len(codes)):
                    return self._send(200, json.dumps({"error": "다른 수집이 진행 중입니다. 잠시 뒤 다시 눌러 주세요"}, ensure_ascii=False), "application/json")
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


WARM_EVERY = 60     # 초


def warm_once(state):
    """자료가 바뀐 뒤 1분 동안 더 바뀌지 않으면(갱신이 끝남) 모든 회사 계산을 미리 해 둔다 — 갱신 뒤 첫 화면도 빠르게.
    state: {'last': 직전 서명}. 반환: 미리 계산했나"""
    from . import pcache
    with JOB_LOCK:
        if JOB.get("running"):
            state["last"] = None
            return False
    conn = db.connect()
    try:
        s = pcache.signature(conn)
        try:                               # 출처 시험 수집(삼성전자, 하루 한 번 — 출처가 조용히 고장 나면 바로 경보)
            from . import sourcehealth
            sourcehealth.run_canary(conn)
            s = pcache.signature(conn)
        except Exception:      # noqa: BLE001
            pass
        stable, state["last"] = s == state.get("last"), s
        if stable and not state.get("ckpt") == s:      # 한가할 때 임시 기록(WAL) 정리
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:      # noqa: BLE001
                pass
            state["ckpt"] = s
        if not stable or pcache.is_warm(s):
            return False
        row = conn.execute("SELECT stock_code FROM companies ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            return False
        report.payload(conn, row["stock_code"], None)
        return True
    finally:
        conn.close()


COLLECTOR = {"state": "시작 전", "why": None, "done": 0, "total": 0, "current": None, "since": None, "last_round": None}
COLLECTOR_PAUSE = threading.Event()        # 화면의 수동 갱신이 차례를 달라고 할 때
COLLECTOR_RECENT_H = 6                     # 한 번 받은 회사는 6시간 안에 다시 돌지 않음(받기 실패가 이어져도 같은 회사를 계속 돌지 않게)


def collector_round():
    """화면 서버 안 자동 수집기 한 번(2026-11-26): 갱신 필요(기한 지난 공시·뉴스·컨센서스)한 회사를 차례로 받음.
    작업 스케줄러의 백그라운드 수집과 같은 수집 잠금을 써서 동시에 돌지 않음. 반환: 처리한 회사 수"""
    import datetime as _dt
    from . import collectlock, pcache
    import time as _t
    with JOB_LOCK:
        busy = JOB.get("running")
    if COLLECTOR_PAUSE.is_set() and not busy:          # 수동 갱신이 시작되지 않고 끝났으면(거절 등) 2분 뒤 스스로 다시
        COLLECTOR.setdefault("paused_at", _t.time())
        if _t.time() - COLLECTOR["paused_at"] > 120:
            COLLECTOR_PAUSE.clear()
    else:
        COLLECTOR.pop("paused_at", None)
    if busy or COLLECTOR_PAUSE.is_set():
        COLLECTOR.update(state="대기", why="화면의 수동 갱신이 도는 중")
        return 0
    if COLLECTOR.get("stopped_until") and _t.time() < COLLECTOR["stopped_until"]:
        COLLECTOR.update(state="멈춤", why="[수집 멈추기]를 눌러 1시간 쉼")
        return 0
    conn = db.connect()
    try:
        collectlock.ensure(conn)
        due = set(collectlock.due_codes(conn))
        recent = (_dt.datetime.now() - _dt.timedelta(hours=COLLECTOR_RECENT_H)).strftime("%Y-%m-%d %H:%M:%S")
        skip = {r[0] for r in conn.execute("SELECT stock_code FROM collect_done WHERE at >= ?", (recent,))}
        names = {r["stock_code"]: r["name"] for r in conn.execute("SELECT stock_code, name FROM companies")}
        codes = [c for c in collectlock.todo_order(conn, sorted(due)) if c not in skip] if due else []
        codes += [c for c in sorted(due) if c not in skip and c not in codes]
    finally:
        conn.close()
    if not codes:
        COLLECTOR.update(state="대기", why="갱신 필요한 회사 없음" if not due else f"갱신 필요 {len(due)}곳은 6시간 안에 이미 받아 봄(받기 실패가 이어지면 B탭 기록 확인)",
                         current=None)
        return 0
    if not collectlock.acquire("background", kind="자동 수집(화면 서버)", total=len(codes)):
        st = collectlock.status() or {}
        COLLECTOR.update(state="대기", why=f"다른 수집이 도는 중: {st.get('kind') or '?'} {st.get('done', 0)}/{st.get('total', '?')}")
        return 0
    n = 0
    COLLECTOR.update(state="수집 중", why=None, done=0, total=len(codes), since=db.now())
    try:
        with pcache.batch(), collectlock.keepalive():
            for i, code in enumerate(codes):
                with JOB_LOCK:
                    busy = JOB.get("running")
                if busy or COLLECTOR_PAUSE.is_set():
                    break
                label = f"{names.get(code) or ''}({code})"
                if not collectlock.heartbeat(current=label, done=i, total=len(codes), kind="자동 수집(화면 서버)"):
                    break
                COLLECTOR.update(done=i, current=label)
                try:
                    c2 = db.connect()
                    pipeline.analyze(code, None, conn=c2)
                    collectlock.mark_done(c2, code, "화면 서버")
                    c2.close()
                    n += 1
                except Exception:      # noqa: BLE001  한 곳이 실패해도 다음 곳(사유는 수집 기록에)
                    continue
    finally:
        collectlock.release()
        COLLECTOR.update(state="대기", why=None, done=n, current=None, last_round=db.now())
    return n


def _collector_loop():
    """로그인하면 켜지는 화면 서버가 늘 돌림 — 브라우저로 화면을 열기 전부터, 절전에서 깨어난 뒤에도 갱신 필요한 회사를 받음"""
    import time as _t
    _t.sleep(45)
    while True:
        try:
            collector_round()
        except Exception as e:     # noqa: BLE001
            COLLECTOR.update(state="오류", why=f"{type(e).__name__}: {e}"[:200])
        _t.sleep(60)


def _prep_loop():
    """새 버전 설치 뒤 한 번: ROA 채우기(30초 뒤 시작, 백그라운드 수집이 돌면 끝날 때까지 기다림)"""
    import time
    time.sleep(30)
    try:
        pipeline.prepare_roa()
    except Exception:      # noqa: BLE001
        pass


def _warm_loop():
    import time
    state = {}
    while True:
        time.sleep(WARM_EVERY)
        try:
            warm_once(state)
        except Exception:      # noqa: BLE001  미리 계산이 실패해도 화면은 평소처럼 연다
            pass


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
    mode = db.set_wal(wait=20)          # 수집이 쓰는 중에도 화면이 읽을 수 있게(2026-11-25)
    print(f"DB 방식: {mode or '확인 못 함(다른 프로그램이 쓰는 중 — 다음 연결 때 다시 시도)'}", flush=True)
    threading.Thread(target=_warm_loop, daemon=True).start()      # 갱신 뒤 첫 화면을 미리 계산
    threading.Thread(target=_prep_loop, daemon=True).start()      # ROA가 없는 회사 채우기(새 버전 설치 뒤 한 번)
    threading.Thread(target=_collector_loop, daemon=True).start() # 자동 수집기(갱신 필요한 회사를 계속 받음)
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
