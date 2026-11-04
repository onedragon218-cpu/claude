"""수집 계획 → 필요한 것만 수집 → 저장.

핵심: DB에 VERIFIED로 이미 있는 값은 다시 찾지 않는다(fetch_log에 skipped-reuse로 남김).
갱신(refresh)은 '새로 생겼을 수 있는 것'만 본다: 정정 공시, 올해 새 분기 보고서, 컨센서스, 오늘 시세.
"""
import glob, json, os, re
from datetime import datetime, date

from . import config
from .db import now
from .secret import get_dart_key
from .sources import dart, fnguide, krx, wisereport

EOK = 1e8


def _jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _days_since(s):
    try:
        return (date.today() - datetime.strptime(s[:10], "%Y-%m-%d").date()).days
    except Exception:
        return 10 ** 6


class Collector:
    def __init__(self, store, w, stock_code, base_year, run_id, offline=False, refresh=False, progress=None, settings=None):
        self.s, self.w, self.code, self.run_id = store, w, stock_code, run_id
        self.offline, self.refresh = offline, refresh
        self.progress = progress or (lambda msg: None)
        self.settings = settings or {}
        self.p = config.periods(base_year)
        self.has_key = bool(get_dart_key())
        self._nokey_logged = False
        self.stats = {"fetched": 0, "reused": 0, "inserted": 0, "same": 0, "new_version": 0, "failed": 0, "missing": []}
        c = store.company(stock_code)
        if not c:
            hits = dart.search(w, stock_code) if not offline else []
            if not hits:
                raise LookupError(f"{stock_code}: DART 상장사 목록에서 찾지 못했습니다")
            h = hits[0]
            store.upsert_company(h["stock_code"], h["corp_code"], h["name"], None, None)
            c = store.company(stock_code)
        self.info = dict(c)

    # ---------- 공통 ----------
    def _put(self, o, reason="신규 수집"):
        o.setdefault("stock_code", self.code)
        o.setdefault("collected_at", now())
        r = self.s.put(o, reason)
        self.stats["inserted" if r == "insert" else r] += 1
        return r

    def log(self, source, target, channel, result, detail=""):
        self.s.log_fetch(self.run_id, self.code, source, target, channel, result, detail)
        self.progress(f"{'재사용' if channel == 'skipped-reuse' else ('완료' if result == 'ok' else '실패')} · {source} · {target}")
        if channel == "skipped-reuse":
            self.stats["reused"] += 1
        elif result == "ok":
            self.stats["fetched"] += 1
        else:
            self.stats["failed"] += 1

    def need_key(self, what):
        """인증키가 없으면 한 번만 알리고 False."""
        if self.has_key:
            return True
        if not self._nokey_logged:
            self.log("DART", "인증키 없음", "api", "fail", f"설정에서 DART 인증키를 등록하면 {what} 등을 받습니다. 지금은 보관 원본만 사용")
            self._nokey_logged = True
        return False

    def run_all(self):
        steps = [self.corrections, self.actuals, self.shares, self.partial, self.prices, self.consensus, self.manual_extra]
        for f in steps:
            try:
                f()
            except Exception as e:     # 한 출처가 실패해도 나머지는 계속
                self.log(f.__name__, "단계 오류", "error", "fail", f"{type(e).__name__}: {e}"[:300])
            self.s.commit()
        return self.stats

    # ---------- 0. 정정 공시(갱신 때만) ----------
    def corrections(self):
        if not self.refresh or self.offline or not self.need_key("정정 공시"):
            return
        a = self.p["actual"]
        found, msg = dart.corrections(self.info["corp_code"], f"{a[0] + 1}0101", date.today().strftime("%Y%m%d"))
        if found is None:
            self.log("DART 정정 공시", "사업보고서", "api", "fail", msg)
            return
        self.log("DART 정정 공시", f"사업보고서 정정 {len(found)}건", "api", "ok", "")
        for x in found:
            m = re.search(r"\((\d{4})\.\d{2}\)", x.get("report_nm", ""))
            if not m:
                continue
            y = int(m.group(1))
            seen = self.s.con.execute("SELECT 1 FROM fetch_log WHERE stock_code=? AND detail LIKE ? LIMIT 1",
                                      (self.code, f"%정정반영 {x['rcept_no']}%")).fetchone()
            if seen:
                continue
            for fs in ("CFS", "OFS"):          # 보관 원본을 지우고 정정본으로 다시 받는다
                p = os.path.join(self.w.rawdir("dart"), f"{self.code}_{fs}_11011_{y}.json.gz")
                if os.path.exists(p):
                    os.remove(p)
            self._fetch_annual([y], reason=f"정정 공시 {x['rcept_no']}", only=None)
            self.log("DART 정정 공시", f"{y} 사업보고서 재수집", "api", "ok", f"정정반영 {x['rcept_no']}")

    # ---------- 1. 과거 실적 ----------
    def actuals(self):
        years = self.p["helper"] + self.p["actual"]
        need, extra_need = [], []
        for y in years:
            rows = {r["item"]: r for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}
            ok = all(a in rows and rows[a]["status"] == "VERIFIED" for a in config.REQUIRED_ACCOUNTS)
            if ok and not ({"ocf", "debt"} <= set(rows)) and y in self.p["actual"]:
                extra_need.append(y)          # 가치평가용 계정 보충(보관 원본이 있으면 네트워크 없음)
                continue
            if ok or (y in self.p["helper"] and {"eq_owner", "debt"} <= set(rows)):
                continue
            need.append(y)
        if extra_need:
            self.log(dart.SOURCE, f"가치평가 계정 보충 {len(extra_need)}개년", "raw-cache", "ok", "법인세·현금흐름·차입금·현금")
            need += [y for y in extra_need if y not in need]
            if self.p["helper"][0] not in need:
                need.append(self.p["helper"][0])
        reused = [y for y in years if y not in need]
        if reused:
            self.log(dart.SOURCE, f"{min(reused)}~{max(reused)} 중 {len(reused)}개년", "skipped-reuse", "ok", "VERIFIED 원값 재사용, 조회 안 함")
        if need and (self.has_key or self.offline or all(
                os.path.exists(os.path.join(self.w.rawdir("dart"), f"{self.code}_CFS_11011_{ry}.json.gz")) for ry in dart.plan_reports(need))):
            self._fetch_annual(need)
        elif need:
            self.need_key("사업보고서")
        for y in self.p["actual"]:
            have = {r["item"] for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}
            self.stats["missing"] += [f"{y} {a}" for a in config.REQUIRED_ACCOUNTS if a not in have]

    def _fetch_annual(self, need, reason="재조회", only=True):
        for ry in dart.plan_reports(need):
            d, channel, desc = dart.statement(self.w, self.code, self.info["corp_code"], ry)
            if d is None and ry == max(need) and ry >= self.p["base"] - 1:
                # 작년 사업보고서가 아직 없으면 재작년 보고서로 나머지를 덮는다
                self.log(dart.SOURCE, f"{ry} 사업보고서", channel, "fail", desc)
                rest = [y for y in need if y < ry]
                if rest:
                    self._fetch_annual(rest, reason, only)
                return
            if d is None:
                self.log(dart.SOURCE, f"{ry} 사업보고서", channel, "fail", desc)
                continue
            by_year, rcept, fs = dart.parse(d)
            self.log(dart.SOURCE, f"{ry} 사업보고서({rcept})", channel, "ok", desc)
            for y, row in by_year.items():
                if only and y not in need:
                    continue
                for item, v in row.items():
                    if item.startswith("_"):
                        continue
                    ex = {"report_year": ry}
                    if item in ("debt", "cash"):
                        ex["accounts"] = row.get(f"_{item}_names", [])
                    self._put({"fiscal_year": y, "item": item, "kind": "ACTUAL", "value": v,
                               "unit": dart.UNITS.get(item, "원"), "basis": fs, "source": dart.SOURCE,
                               "source_ref": rcept, "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                               "extra": json.dumps(ex, ensure_ascii=False)}, reason=reason)

    # ---------- 2. 주식 수(연말) ----------
    def shares(self):
        if self.offline:
            return
        if not self.has_key and not any(os.path.exists(os.path.join(self.w.rawdir("dart"), f"{self.code}_shares_11011_{y}.json.gz")) for y in self.p["actual"]):
            self.need_key("주식 수")
            return
        todo = []
        for y in self.p["actual"]:
            rows = self.s.current(self.code, fiscal_year=y, item="shares_common", kind="ACTUAL")
            if not rows:
                todo.append(y)
        if len(todo) < len(self.p["actual"]):
            self.log("DART 주식총수", f"{len(self.p['actual']) - len(todo)}개년", "skipped-reuse", "ok", "DB 보유분 재사용")
        for y in todo:
            res, ch, desc = dart.shares(self.w, self.code, self.info["corp_code"], y)
            if not res or not res.get("common"):
                self.log("DART 주식총수", f"{y}", ch, "fail", desc)
                continue
            for item, key in (("shares_common", "common"), ("shares_treasury", "treasury"), ("shares_pref", "pref")):
                if res.get(key) is not None:
                    self._put({"fiscal_year": y, "item": item, "kind": "ACTUAL", "value": res[key], "unit": "주",
                               "source": "DART 주식총수 현황", "source_ref": res.get("rcept"),
                               "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={res.get('rcept')}"})
            self.log("DART 주식총수", f"{y}", ch, "ok", desc)

    # ---------- 3. 올해 부분 실적 ----------
    def partial(self):
        b = self.p["base"]
        have = self.s.current(self.code, fiscal_year=b, kind="ACTUAL_PARTIAL")
        if have and not self.refresh:
            self.log(dart.SOURCE_PARTIAL, f"{b} {have[0]['period']}", "skipped-reuse", "ok", "보유분 재사용(갱신 때 새 보고서 확인)")
            return
        if self.offline or not self.need_key("올해 분기·반기 보고서"):
            return self._manual_partial()
        reprt, d, ch = dart.latest_partial(self.w, self.code, self.info["corp_code"], b)
        if not reprt:
            self.log(dart.SOURCE_PARTIAL, f"{b}", "api", "fail", ch)
            return self._manual_partial()
        row, rcept = dart.parse_partial(d)
        per = dart.PERIOD[reprt]
        for r in have:                    # 더 최근 보고서가 나왔으면 이전 기간 행은 내려 둔다(삭제 아님)
            if r["period"] != per:
                self.s.con.execute("UPDATE observations SET is_current=0 WHERE id=?", (r["id"],))
        for item, v in row.items():
            self._put({"fiscal_year": b, "period": per, "item": item, "kind": "ACTUAL_PARTIAL", "value": v,
                       "unit": dart.UNITS.get(item, "원"), "basis": "CFS", "source": dart.SOURCE_PARTIAL,
                       "source_ref": rcept, "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"})
        self.log(dart.SOURCE_PARTIAL, f"{b} {dart.REPORTS[reprt]}({rcept})", ch, "ok", "")

    def _manual_partial(self):
        b = self.p["base"]
        for path in sorted(glob.glob(os.path.join(self.w.manual, f"{self.code}_{b}*.json"))):
            m = _jload(path)
            if m.get("fiscal_year") != b or "accounts" not in m:
                continue
            for item, v in m["accounts"].items():
                self._put({"fiscal_year": b, "period": m["period"], "item": item, "kind": "ACTUAL_PARTIAL",
                           "value": v, "unit": "원", "basis": m["basis"], "source": m["source"],
                           "source_ref": m["source_ref"], "source_url": m.get("source_url"), "collected_at": m.get("collected_at")})
            self.log(m["source"], f"{b} {m['period']}({m['source_ref']})", "manual", "ok", os.path.basename(path))

    # ---------- 4. 연말 시가총액 ----------
    def prices(self):
        have = {r["fiscal_year"] for r in self.s.current(self.code, kind="MARKET", item=("market_cap", "price_close"))
                if r["fiscal_year"] < self.p["base"]}
        todo = [y for y in self.p["actual"] if y not in have]
        if len(todo) < len(self.p["actual"]):
            self.log("연말 시가총액", f"{len(self.p['actual']) - len(todo)}개년", "skipped-reuse", "ok", "DB 보유분 재사용")
        if not todo:
            return
        if self.offline:
            return self._manual_prices(todo)
        naver = None
        for y in todo:
            res, ch, desc = krx.year_end(self.w, self.code, y)
            if res:
                common = {"source": res["source"], "source_ref": res["date"], "as_of": res["date"]}
                self._put({**common, "fiscal_year": y, "item": "market_cap", "kind": "MARKET", "value": res["market_cap"], "unit": "원"})
                self._put({**common, "fiscal_year": y, "item": "listed_shares", "kind": "MARKET", "value": res["listed_shares"], "unit": "주"})
                if res.get("close"):
                    self._put({**common, "fiscal_year": y, "item": "price_close", "kind": "MARKET", "value": res["close"], "unit": "원"})
                self.log(res["source"], f"{y} 연말", ch, "ok", desc)
                continue
            if naver is None:
                naver, nch = krx.naver_closes(self.w, self.code)
                if not isinstance(naver, dict):
                    self.log(krx.SOURCE_NAVER, "연말 종가", "web", "fail", f"{desc} / {nch}")
                    naver = {}
            if y in naver:
                dt, close = naver[y]
                self._put({"fiscal_year": y, "item": "price_close", "kind": "MARKET", "value": close, "unit": "원",
                           "source": krx.SOURCE_NAVER, "source_ref": dt, "as_of": dt,
                           "status_note": "차트 종가(수정주가일 수 있음)"})
                self.log(krx.SOURCE_NAVER, f"{y} 연말", "web", "ok", f"pykrx 사용 불가({desc})")
            else:
                self.log("연말 시가총액", f"{y}", "api", "fail", desc)
        self._manual_prices([y for y in todo if not self.s.current(self.code, fiscal_year=y, kind="MARKET")])

    def _manual_prices(self, todo):
        path = os.path.join(self.w.manual, f"{self.code}_prices.json")
        if not todo or not os.path.exists(path):
            return
        m = _jload(path)
        n = 0
        for y in todo:
            if str(y) in m["year_end_close"]:
                self._put({"fiscal_year": y, "item": "price_close", "kind": "MARKET", "value": m["year_end_close"][str(y)],
                           "unit": "원", "source": m["source"], "source_ref": m["source_ref"],
                           "as_of": f"{y}-12-31(마지막 거래일)", "collected_at": m["collected_at"]})
                n += 1
        if n:
            self.log(m["source"], f"연말 종가 {n}개년", "manual", "ok", os.path.basename(path))

    # ---------- 5. 컨센서스·현재 시세 ----------
    def consensus(self):
        last = self.s.con.execute(
            "SELECT MAX(collected_at) c FROM observations WHERE stock_code=? AND source=? AND kind='CONSENSUS'",
            (self.code, fnguide.SOURCE)).fetchone()["c"]
        if last and _days_since(last) < config.CONSENSUS_STALE_DAYS and not self.refresh:
            self.log(fnguide.SOURCE, "컨센서스·시세", "skipped-reuse", "ok",
                     f"마지막 수집 {last[:10]} (갱신 주기 {config.CONSENSUS_STALE_DAYS}일 이내)")
        else:
            today = fnguide.raw_path(self.w, self.code)
            if os.path.exists(today):
                path, ch, desc = today, "raw-cache", os.path.basename(today)
            elif self.offline:
                path, ch, desc = None, "web", "오프라인"
            else:
                path, desc = fnguide.fetch(self.w, self.code, self.settings.get("user_agent"))
                ch = "web"
            if path:
                f = fnguide.parse(path)
                if f.get("error"):
                    self.log(fnguide.SOURCE, "컨센서스·시세", ch, "fail", f"{f['error']} · 원본 {os.path.basename(path)}")
                else:
                    self.log(fnguide.SOURCE, f"컨센서스·시세(기준 {f.get('as_of')})", ch, "ok", desc)
                    self._store_fnguide(f, collected=date.today().isoformat())
            else:
                self.log(fnguide.SOURCE, "컨센서스·시세", ch, "fail", desc)
        self._wisereport()

    def _store_fnguide(self, f, collected):
        url = fnguide.URL.format(code=self.code)
        common = {"source": fnguide.SOURCE, "source_ref": f["raw_file"], "source_url": url,
                  "as_of": f.get("as_of", ""), "collected_at": collected, "basis": f.get("basis")}
        extra = json.dumps({k: f.get(k) for k in ("n_estimates", "target_price", "opinion", "eps_fy1", "per_fy1")}, ensure_ascii=False)
        units = {"eps": "원", "bps": "원", "dps": "원", "roe": "%", "debt_ratio": "%", "op_margin": "%", "per": "배", "pbr": "배", "shares": "주"}
        for y, row in f["years"].items():
            kind = row.get("kind")
            for item, v in row.items():
                if item == "kind":
                    continue
                val = v * EOK if item in fnguide.AMOUNT else v
                if kind == "E" and y >= self.p["base"]:
                    self._put({**common, "fiscal_year": y, "item": item, "kind": "CONSENSUS", "value": val,
                               "unit": units.get(item, "원"), "extra": extra})
                elif kind in ("A", "P"):
                    self._put({**common, "fiscal_year": y, "item": item, "kind": "REFERENCE", "value": val,
                               "unit": units.get(item, "원"), "status_note": "잠정실적(P)" if kind == "P" else None})
        mk = f.get("market", {})
        b = self.p["base"]
        for item, key, unit, mul in (("price_close", "price", "원", 1), ("shares", "shares_common", "주", 1),
                                     ("market_cap", "market_cap_eok", "원", EOK)):
            if mk.get(key) is not None:
                self._put({**common, "fiscal_year": b, "item": item, "kind": "MARKET", "value": mk[key] * mul, "unit": unit})

    def _wisereport(self):
        """교차검증용 두 번째 출처(올해 EPS·BPS 전망, 직전 연도 EPS·BPS)."""
        last = self.s.con.execute(
            "SELECT MAX(collected_at) c FROM observations WHERE stock_code=? AND source=? AND kind='CONSENSUS'",
            (self.code, wisereport.SOURCE)).fetchone()["c"]
        if last and _days_since(last) < config.CONSENSUS_STALE_DAYS and not self.refresh:
            self.log(wisereport.SOURCE, "교차검증", "skipped-reuse", "ok", f"마지막 수집 {last[:10]}")
            return
        today = date.today().strftime("%Y%m%d")
        path = os.path.join(self.w.rawdir("wisereport"), f"{self.code}_c1010001_{today}.html.gz")
        if os.path.exists(path):
            ch, desc = "raw-cache", os.path.basename(path)
        elif self.offline:
            cands = sorted(glob.glob(os.path.join(self.w.rawdir("wisereport"), f"{self.code}_c1010001_*.html.gz")))
            if not cands:
                return
            path, ch, desc = cands[-1], "raw-cache", os.path.basename(cands[-1])
        else:
            path, desc = wisereport.fetch(self.w.root, self.code, today, rawdir=self.w.rawdir("wisereport"))
            ch = "web"
        if not path:
            self.log(wisereport.SOURCE, "교차검증", ch, "fail", desc)
            return
        x = wisereport.parse(path)
        self.log(wisereport.SOURCE, f"교차검증(기준 {x.get('as_of')})", ch, "ok", desc)
        collected = f"{path[-16:-12]}-{path[-12:-10]}-{path[-10:-8]}"
        common = {"source": wisereport.SOURCE, "source_ref": x["raw_file"], "source_url": wisereport.URL.format(code=self.code),
                  "as_of": x.get("as_of", ""), "collected_at": collected}
        extra = json.dumps({k: x.get(k) for k in ("n_estimates", "target_price", "per_e", "opinion")}, ensure_ascii=False)
        ye, ya = x.get("year_e"), x.get("year_a")
        for item, key in (("eps", "eps_e"), ("bps", "bps_e")):
            if ye and x.get(key) is not None:
                self._put({**common, "fiscal_year": ye, "item": item, "kind": "CONSENSUS", "value": x[key], "unit": "원",
                           "basis": x.get("basis_e"), "extra": extra})
        for item, key in (("eps", "eps_a"), ("bps", "bps_a")):
            if ya and x.get(key) is not None:
                self._put({**common, "fiscal_year": ya, "item": item, "kind": "REFERENCE", "value": x[key], "unit": "원"})
        if not self.s.current(self.code, fiscal_year=self.p["base"], kind="MARKET", source=fnguide.SOURCE):
            for item, key, unit, mul in (("price_close", "price", "원", 1), ("shares", "shares", "주", 1),
                                         ("market_cap", "market_cap_eok", "원", EOK)):
                if x.get(key) is not None:
                    self._put({**common, "fiscal_year": self.p["base"], "item": item, "kind": "MARKET", "value": x[key] * mul, "unit": unit})

    # ---------- 6. 손으로 넣은 보조 전망(선택) ----------
    def manual_extra(self):
        path = os.path.join(self.w.manual, f"{self.code}_consensus_unverified.json")
        if not os.path.exists(path):
            return
        m = _jload(path)
        n = 0
        for y, row in m["years"].items():
            for item, v in row.items():
                val = v * EOK if item in ("revenue", "op") else v
                r = self._put({"fiscal_year": int(y), "item": item, "kind": "CONSENSUS", "value": val,
                               "unit": {"roe": "%"}.get(item, "원"), "source": m["source"], "source_ref": m["source_ref"],
                               "source_url": m.get("source_url"), "as_of": m.get("as_of", ""), "collected_at": m["collected_at"],
                               "status": "UNVERIFIED", "status_note": "원문 미확인"})
                n += r != "same"
        self.log(m["source"], "미검증 전망", "manual" if n else "skipped-reuse", "ok", f"{n}건 반영" if n else "DB 보유분 재사용")
