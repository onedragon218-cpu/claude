"""수집 계획 → 필요한 것만 수집 → 저장.

핵심: DB에 VERIFIED로 이미 있는 값은 다시 찾지 않는다(fetch_log에 skipped-reuse로 남김).
"""
import glob, json, os
from datetime import datetime, date

from . import config
from .db import now
from .sources import dart, wisereport

EOK = 1e8


def _days_since(s):
    try:
        return (date.today() - datetime.strptime(s[:10], "%Y-%m-%d").date()).days
    except Exception:
        return 10 ** 6


class Collector:
    def __init__(self, store, stock_code, base_year, run_id, root=config.ROOT, offline=False):
        self.s, self.code, self.run_id, self.root, self.offline = store, stock_code, run_id, root, offline
        self.p = config.periods(base_year)
        info = config.companies().get(stock_code)
        if not info:
            raise SystemExit(f"{stock_code}: data/companies.json에 없음. corp_code와 액면가를 먼저 등록하세요.")
        self.info = info
        store.upsert_company(stock_code, info["corp_code"], info["name"], info.get("market"), info.get("face_value"))
        self.stats = {"fetched": 0, "reused": 0, "inserted": 0, "same": 0, "new_version": 0, "missing": []}

    def _put(self, o, reason="신규 수집"):
        o.setdefault("stock_code", self.code)
        o.setdefault("collected_at", now())
        r = self.s.put(o, reason)
        self.stats["inserted" if r == "insert" else r] += 1
        return r

    def log(self, source, target, channel, result, detail=""):
        self.s.log_fetch(self.run_id, self.code, source, target, channel, result, detail)
        if channel == "skipped-reuse":
            self.stats["reused"] += 1
        elif result == "ok":
            self.stats["fetched"] += 1

    # ---------- 1. 과거 실적 ----------
    def actuals(self, force=False):
        years = self.p["helper"] + self.p["actual"]
        need = []
        for y in years:
            rows = {r["item"]: r for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}
            ok = all(a in rows and rows[a]["status"] == "VERIFIED" for a in config.BASE_ACCOUNTS)
            if ok and not force:
                continue
            if y in self.p["helper"] and {"eq_owner"} <= set(rows):  # 보조연도는 평균자본용 지배자본만 있으면 충분
                continue
            need.append(y)
        reused = [y for y in years if y not in need]
        if reused:
            self.log(dart.SOURCE, f"{min(reused)}~{max(reused)} 중 {len(reused)}개년", "skipped-reuse", "ok",
                     "VERIFIED 원값 재사용, 조회 안 함")
        for ry in dart.plan_reports(need):
            d, channel, desc = dart.load(self.root, self.code, self.info["corp_code"], ry)
            if d is None:
                self.log(dart.SOURCE, f"{ry} 사업보고서", channel, "fail", desc)
                continue
            by_year, rcept = dart.parse(d)
            self.log(dart.SOURCE, f"{ry} 사업보고서({rcept})", channel, "ok", desc)
            for y, row in by_year.items():
                if y not in need:
                    continue
                for item, v in row.items():
                    self._put({"fiscal_year": y, "item": item, "kind": "ACTUAL", "value": v,
                               "unit": dart.UNITS.get(item, "원"), "basis": "CFS", "source": dart.SOURCE,
                               "source_ref": rcept,
                               "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                               "extra": json.dumps({"report_year": ry}, ensure_ascii=False)},
                              reason=f"{ry} 사업보고서 재조회")
        for y in self.p["actual"]:
            have = {r["item"] for r in self.s.current(self.code, fiscal_year=y, kind="ACTUAL", period="FY")}
            for a in config.BASE_ACCOUNTS:
                if a not in have:
                    self.stats["missing"].append(f"{y} {a}")

    # ---------- 2. 올해 부분 실적(반기·분기) ----------
    def partial(self):
        b = self.p["base"]
        for path in sorted(glob.glob(os.path.join(self.root, "data", "manual", f"{self.code}_{b}*.json"))):
            m = json.load(open(path, encoding="utf-8"))
            if m.get("fiscal_year") != b or "accounts" not in m:
                continue
            have = self.s.current(self.code, fiscal_year=b, period=m["period"], kind="ACTUAL_PARTIAL", source_ref=m["source_ref"])
            if len(have) >= len(m["accounts"]) and all(r["status"] == "VERIFIED" for r in have):
                self.log(m["source"], f"{b} {m['period']}", "skipped-reuse", "ok", "VERIFIED 재사용")
                continue
            for item, v in m["accounts"].items():
                self._put({"fiscal_year": b, "period": m["period"], "item": item, "kind": "ACTUAL_PARTIAL",
                           "value": v, "unit": "원", "basis": m["basis"], "source": m["source"],
                           "source_ref": m["source_ref"], "source_url": m.get("source_url"),
                           "collected_at": m.get("collected_at")})
            self.log(m["source"], f"{b} {m['period']}({m['source_ref']})", "manual", "ok", os.path.basename(path))

    # ---------- 3. 연말 주가 ----------
    def prices(self):
        path = os.path.join(self.root, "data", "manual", f"{self.code}_prices.json")
        if not os.path.exists(path):
            self.log("주가", "연말 종가", "manual", "fail", "data/manual/<종목>_prices.json 없음")
            return
        m = json.load(open(path, encoding="utf-8"))
        years = self.p["actual"]
        have = {r["fiscal_year"] for r in self.s.current(self.code, item="price_close", kind="MARKET")}
        todo = [y for y in years if y not in have and str(y) in m["year_end_close"]]
        if len(todo) < len(years):
            self.log(m["source"], f"연말 종가 {len(years) - len(todo)}개년", "skipped-reuse", "ok", "DB 보유분 재사용")
        for y in todo:
            self._put({"fiscal_year": y, "item": "price_close", "kind": "MARKET", "value": m["year_end_close"][str(y)],
                       "unit": "원", "source": m["source"], "source_ref": m["source_ref"],
                       "as_of": f"{y}-12-31(마지막 거래일)", "collected_at": m["collected_at"]})
        if todo:
            self.log(m["source"], f"연말 종가 {len(todo)}개년", "manual", "ok", os.path.basename(path))

    # ---------- 4. 컨센서스·현재 시세 ----------
    def consensus(self, force=False):
        last = self.s.con.execute(
            "SELECT MAX(collected_at) c FROM observations WHERE stock_code=? AND source=? AND kind='CONSENSUS'",
            (self.code, wisereport.SOURCE)).fetchone()["c"]
        if last and _days_since(last) < config.CONSENSUS_STALE_DAYS and not force:
            self.log(wisereport.SOURCE, "컨센서스·시세", "skipped-reuse", "ok",
                     f"마지막 수집 {last[:10]} (갱신 주기 {config.CONSENSUS_STALE_DAYS}일 이내)")
        else:
            today = date.today().strftime("%Y%m%d")
            path = os.path.join(wisereport.raw_dir(self.root), f"{self.code}_c1010001_{today}.html.gz")
            if os.path.exists(path):
                channel, desc = "raw-cache", os.path.basename(path)
            elif self.offline:
                path, channel, desc = None, "web", "오프라인 모드"
            else:
                path, desc = wisereport.fetch(self.root, self.code, today)
                channel = "web"
            if not path:
                self.log(wisereport.SOURCE, "컨센서스·시세", channel, "fail", desc)
            else:
                w = wisereport.parse(path)
                self.log(wisereport.SOURCE, f"컨센서스·시세(기준 {w.get('as_of')})", channel, "ok", desc)
                self._store_wise(w, collected=f"{path[-16:-12]}-{path[-12:-10]}-{path[-10:-8]}")
        self.unverified_manual()

    def _store_wise(self, w, collected):
        url = wisereport.URL.format(code=self.code)
        common = {"source": wisereport.SOURCE, "source_ref": w["raw_file"], "source_url": url,
                  "as_of": w.get("as_of", ""), "collected_at": collected}
        extra = json.dumps({k: w.get(k) for k in ("n_estimates", "target_price", "per_e", "opinion")}, ensure_ascii=False)
        ye, ya = w.get("year_e"), w.get("year_a")
        for item, key in (("eps", "eps_e"), ("bps", "bps_e")):
            if ye and w.get(key) is not None:
                self._put({**common, "fiscal_year": ye, "item": item, "kind": "CONSENSUS", "value": w[key],
                           "unit": "원", "basis": w.get("basis_e"), "extra": extra})
        for item, key in (("eps", "eps_a"), ("bps", "bps_a")):  # 교차검증용 참고값
            if ya and w.get(key) is not None:
                self._put({**common, "fiscal_year": ya, "item": item, "kind": "REFERENCE", "value": w[key], "unit": "원"})
        b = self.p["base"]
        for item, key, unit, mul in (("price_close", "price", "원", 1), ("shares", "shares", "주", 1),
                                     ("market_cap", "market_cap_eok", "원", EOK)):
            if w.get(key) is not None:
                self._put({**common, "fiscal_year": b, "item": item, "kind": "MARKET", "value": w[key] * mul, "unit": unit})

    def unverified_manual(self):
        path = os.path.join(self.root, "data", "manual", f"{self.code}_consensus_unverified.json")
        if not os.path.exists(path):
            return
        m = json.load(open(path, encoding="utf-8"))
        n = 0
        for y, row in m["years"].items():
            for item, v in row.items():
                val = v * EOK if item in ("revenue", "op") else v
                unit = {"roe": "%", "eps": "원", "bps": "원"}.get(item, "원")
                r = self._put({"fiscal_year": int(y), "item": item, "kind": "CONSENSUS", "value": val, "unit": unit,
                               "source": m["source"], "source_ref": m["source_ref"], "source_url": m.get("source_url"),
                               "as_of": m.get("as_of", ""), "collected_at": m["collected_at"],
                               "status": "UNVERIFIED", "status_note": "원문 미확인. 추정 기관 수·기준일 모름"})
                n += r != "same"
        self.log(m["source"], "미검증 컨센서스", "manual" if n else "skipped-reuse", "ok",
                 f"{n}건 반영" if n else "DB 보유분 재사용")
