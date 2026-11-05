"""수집 계획 → 빠진 것만 수집 → 저장 → 지표 계산 → 검증.

핵심 규칙
- DB에 이미 있는 값은 다시 수집하지 않는다(5-2).
- 확인했는데 자료가 없던 항목은 TTL 동안 다시 묻지 않는다(반복 검색 방지).
- 정정 공시가 새로 보이면 그 연도만 STALE로 바꾸고 그 보고서만 다시 받는다(5-6).
"""
import glob
import json
import os
import re
import statistics

from . import config, db
from .collectors import dart, fnguide, manual, naver, prices, wisereport

CORE_ACC = ["acc_revenue", "acc_op", "acc_ni_owner", "acc_eq_owner", "acc_eq", "acc_liab", "acc_eps_basic"]
XBRL_ONLY_KEYS = ["acc_lease_xbrl"]   # XBRL 주석에서만 나오는 계정
ACC_UNIT = {k: ("원" if k != "acc_eps_basic" else "원/주") for k in dart.ALL_KEYS + XBRL_ONLY_KEYS}
RAW_DIR = os.path.join(config.DATA_DIR, "raw")


class Stats:
    """이번 실행에서 실제로 밖에 요청한 횟수와 재사용 개수를 센다."""

    def __init__(self):
        self.requests = {}
        self.reused = 0
        self.new = 0
        self.changed = 0
        self.notes = []

    def req(self, src):
        self.requests[src] = self.requests.get(src, 0) + 1

    def put(self, res):
        if res == "new":
            self.new += 1
        elif res == "changed":
            self.changed += 1

    def as_dict(self):
        return {"requests": self.requests, "total_requests": sum(self.requests.values()), "reused_facts": self.reused,
                "new_facts": self.new, "changed_facts": self.changed, "notes": self.notes}


# ---------------------------------------------------------------- 저장(ingest)
def ingest_statement(conn, code, d, fs_div="CFS", via="OpenDART API", stats=None):
    """DART 재무제표 응답을 관측값으로 저장하고, 연도별 대표값(facts)을 다시 고른다."""
    p = dart.parse_statement(d, fs_div)
    m = p["meta"]
    if not p["values"]:
        return 0
    for (y, period, key, v, col) in p["values"]:
        db.add_observation(conn, code, y, key, v, period=period, unit=ACC_UNIT[key], basis=fs_div, source="DART",
                           source_ref=m["rcept_no"], report_year=m["bsns_year"], col=col)
    conn.execute("INSERT OR IGNORE INTO disclosures VALUES (?,?,?,?,?,?,?,?)",
                 (m["rcept_no"], code, f"{dart.REPRT_NAME[m['reprt_code']]} ({m['bsns_year']})", m["rcept_no"][:8],
                  m["bsns_year"], m["reprt_code"], 0, db.now()))
    years = sorted({(y, period) for (y, period, *_r) in p["values"]})
    for (y, period) in years:
        select_facts(conn, code, y, period, via=via, stats=stats)
    conn.commit()
    return len(p["values"])


def select_facts(conn, code, y, period, via=None, stats=None):
    """같은 연도·계정의 여러 관측값 중 대표값을 고른다.
    1순위: 그 연도 자신의 보고서(정정 공시가 있으면 가장 최근 접수번호)
    2순위: 그 연도를 처음으로 담은 후속 보고서(원래 숫자에 가장 가까움)"""
    for key in dart.ALL_KEYS + XBRL_ONLY_KEYS:
        # 대표값은 DART 원본에서만 고른다(WiseReport 등 다른 출처는 교차 검증용 관측값)
        obs = [o for o in db.observations(conn, code, y, key, period) if o["source"] == "DART" and o["report_year"] is not None]
        if not obs:
            continue
        own = [o for o in obs if o["report_year"] == y]
        if own:
            best = max(own, key=lambda o: o["source_ref"])
            note = None
        else:
            first_year = min(o["report_year"] for o in obs)
            best = max([o for o in obs if o["report_year"] == first_year], key=lambda o: o["source_ref"])
            note = f"{best['report_year']}년 보고서의 {best['col']} 열 값(당해 보고서 미수집)"
        cur = db.current_fact(conn, code, y, key, period)
        reason = None
        status = "UNVERIFIED"
        if cur is not None and cur["source_ref"] != best["source_ref"] and cur["value"] is not None:
            same_report = conn.execute("SELECT 1 FROM disclosures WHERE rcept_no=? AND report_year=?",
                                       (cur["source_ref"], best["report_year"])).fetchone()
            if same_report and best["source_ref"] > cur["source_ref"]:
                reason, status = "정정 공시 반영", "CORRECTED"
            else:
                reason = "당해 보고서 원본으로 교체" if best["report_year"] == y else "대표값 출처 변경"
        res = db.put_fact(conn, code, y, key, best["value"], period=period, unit=best["unit"], basis=best["basis"],
                          data_type="ACTUAL" if period == "FY" else "PARTIAL", source="DART",
                          source_ref=best["source_ref"],
                          source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={best['source_ref']}",
                          as_of=_period_end(y, period), status=status,
                          status_note=(note or "") + (f" · 수집 경로: {via}" if via else ""), change_reason=reason)
        if res == "same" and cur is not None and cur["status"] == "STALE":
            db.set_status(conn, cur["id"], "UNVERIFIED", "재수집 결과 기존 값과 같음 → 재검증 대기")
        if stats:
            stats.put(res)


def _period_end(y, period):
    return {"FY": f"{y}-12-31", "H1": f"{y}-06-30", "Q1": f"{y}-03-31", "Q3": f"{y}-09-30"}.get(period)


def ingest_shares(conn, code, year, parsed, stats=None):
    ref = parsed.get("rcept_no")
    for k, metric in (("common", "shares_common"), ("pref", "shares_pref"),
                      ("treasury_common", "treasury_common"), ("treasury_pref", "treasury_pref")):
        if k in parsed:
            r = db.put_fact(conn, code, year, metric, parsed[k], unit="주", basis="-", source="DART", source_ref=ref,
                            source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ref}", as_of=f"{year}-12-31",
                            status_note="DART 주식의 총수 현황")
            if r == "same":
                cur = db.current_fact(conn, code, year, metric)
                if cur is not None and cur["status"] == "STALE":
                    db.set_status(conn, cur["id"], "UNVERIFIED", "재수집 결과 기존 값과 같음")
            if stats:
                stats.put(r)
    conn.commit()


def ingest_prices(conn, code, pdata, base_year, stats=None):
    ye = prices.year_end(pdata["rows"])
    for y, (d, c, note) in ye.items():
        date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if y < base_year:
            if int(d[4:]) < 1220:      # 연말 거래일이 아니면 연말 종가로 쓰지 않는다
                continue
            r = db.put_fact(conn, code, y, "price_close", c, unit="원", basis=pdata["basis"], source=pdata["source"],
                            source_ref=pdata["ref"], as_of=date, status_note=note)
        elif y == base_year:
            r = db.put_fact(conn, code, y, "price_close", c, period="ASOF", unit="원", basis=pdata["basis"],
                            source=pdata["source"], source_ref=f"{pdata['ref']} @{date}", as_of=date,
                            data_type="PARTIAL", status_note=note or "분석 기준일 주가",
                            change_reason="기준일 주가 갱신")
        else:
            continue
        if stats:
            stats.put(r)
    conn.commit()


def ingest_consensus(conn, code, items, provider, as_of, source_ref):
    n = 0
    for it in items:
        n += db.add_consensus(conn, code, it["target_year"], it["metric"], it["value"], provider=provider,
                              as_of=it.get("as_of", as_of), unit=it.get("unit"), broker=it.get("broker", "(평균)"),
                              n_estimates=it.get("n_estimates"), basis=it.get("basis", "연결"),
                              source_ref=it.get("source_ref", source_ref))
    conn.commit()
    return n


def save_raw(code, d):
    """받은 DART 응답 원본을 data/raw/<종목코드>/ 에 보관한다. 해석 규칙이 바뀌면 네트워크 없이 다시 해석한다."""
    lst = d.get("list") or []
    if not lst:
        return
    folder = os.path.join(RAW_DIR, code)
    os.makedirs(folder, exist_ok=True)
    fn = os.path.join(folder, f"dart_{lst[0]['bsns_year']}_{lst[0]['reprt_code']}_CFS_{lst[0]['rcept_no']}.json")
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))


def reparse_cached(conn, code, stats=None):
    """해석기 버전이 바뀌었으면 보관된 원본(seed + data/raw)을 다시 해석해 새 계정을 채운다(외부 요청 없음)."""
    key = f"{code}:v{dart.PARSER_VERSION}"
    if db.last_fetch(conn, "parse", key):
        return 0
    files = sorted(glob.glob(os.path.join(config.SEED_DIR, code, "dart_*_CFS*.json")) +
                   glob.glob(os.path.join(RAW_DIR, code, "dart_*_CFS*.json")))
    n = 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            n += ingest_statement(conn, code, json.load(fh), "CFS", via="보관 원본 재해석", stats=stats)
    db.log_fetch(conn, "parse", key, code, True, n, f"{len(files)}개 원본 재해석")
    return n


def import_seed(conn, code, base_year, stats=None):
    """analyzer/seed/<종목코드>/ 의 저장 원본(DART 응답 JSON, 시세 JSON, 컨센서스 CSV)을 가져온다."""
    folder = os.path.join(config.SEED_DIR, code)
    n = 0
    meta = os.path.join(folder, "company.json")
    if os.path.exists(meta):
        m = json.load(open(meta, encoding="utf-8"))
        db.ensure_company(conn, code, m.get("name"), m.get("corp_code"))
        if m.get("induty_code"):
            db.set_induty(conn, code, m["induty_code"])
    for f in sorted(glob.glob(os.path.join(folder, "dart_*_CFS.json"))):
        n += ingest_statement(conn, code, json.load(open(f, encoding="utf-8")), "CFS",
                              via="OpenDART 응답 저장본(PlayMCP opendart 경유)", stats=stats)
    for f in sorted(glob.glob(os.path.join(folder, "prices_*.json"))):
        ingest_prices(conn, code, prices.import_kis_json(f), base_year, stats=stats)
    for f in sorted(glob.glob(os.path.join(folder, "consensus_*.csv"))):
        n += ingest_consensus(conn, code, manual.read_consensus_csv(f), "manual", None, f)
    db.log_fetch(conn, "seed", code, code, True, n, folder)
    return n


# ---------------------------------------------------------------- 수집 계획
def plan(conn, code, P):
    """분석에 필요한 항목 중 DB에 없는 것만 돌려준다."""
    fm = db.fact_map(conn, code)
    need = {"statement_years": [], "share_years": [], "price_years": [], "interim": False,
            "asof_price": False, "consensus": False}
    years = [P["past"][0] - 1] + P["past"]          # 첫해 ROE 계산용 전년 자본 포함
    for y in years:
        keys = ["acc_eq_owner"] if y == years[0] else CORE_ACC
        if any(fm.get((y, k)) is None or fm[(y, k)]["value"] is None or fm[(y, k)]["status"] == "STALE" for k in keys):
            need["statement_years"].append(y)
    for y in P["past"]:
        if fm.get((y, "shares_common")) is None or fm[(y, "shares_common")]["status"] == "STALE":
            need["share_years"].append(y)
        if fm.get((y, "price_close")) is None:
            need["price_years"].append(y)
    cur = P["current"]
    have_interim = [r["period"] for r in conn.execute(
        "SELECT DISTINCT period FROM facts WHERE stock_code=? AND fiscal_year=? AND period IN ('Q1','H1','Q3') "
        "AND is_current=1", (code, cur))]
    need["interim"] = "Q3" not in have_interim
    need["asof_price"] = not db.fetched_recently(conn, "price", f"{code}:asof", config.TTL_DAYS["price_asof"], ok_only=True) and \
        _asof_age(conn, code, cur) != 0
    need["consensus"] = not db.fetched_recently(conn, "consensus", code, config.TTL_DAYS["consensus"], ok_only=True)
    return need


def _asof_age(conn, code, cur):
    import datetime as dt
    r = db.current_fact(conn, code, cur, "price_close", "ASOF")
    if r is None or not r["as_of"]:
        return None
    return (dt.date.today() - dt.date.fromisoformat(r["as_of"])).days


def collect(conn, code, P, stats, offline=False, fresh=False):
    """빠진 항목만 수집한다. offline=True면 바깥 요청 없이 DB만 사용."""
    need = plan(conn, code, P)
    if fresh:   # 사용자가 '지금 새로 받기'를 고른 경우: 컨센서스·기준일 주가는 기한과 관계없이 받는다
        need["consensus"] = True
        need["asof_price"] = True
    stats.notes.append("수집 필요 항목: " + json.dumps({k: v for k, v in need.items() if v}, ensure_ascii=False))
    if offline:
        import_seed_news(conn, code)
        return need
    corp = None
    has_key = dart.has_key()
    if not has_key:
        stats.notes.append("DART_API_KEY 없음 → DART 수집(공시 점검·사업보고서·주식 수·분기 실적) 건너뜀")

    def _corp():
        nonlocal corp
        if corp is None:
            row = conn.execute("SELECT corp_code FROM companies WHERE stock_code=?", (code,)).fetchone()
            if row and row["corp_code"]:
                corp = row["corp_code"]
            else:
                stats.req("DART")
                info = dart.corp_code(code)
                corp = info["corp_code"]
                db.ensure_company(conn, code, info["name"], corp)
        return corp

    # 1) 신규·정정 공시 점검(하루 1번)
    try:
        if has_key and not db.fetched_recently(conn, "dart_disclosures", code, config.TTL_DAYS["dart_disclosures"]):
            stats.req("DART")
            lst = dart.fetch_disclosures(_corp(), f"{P['past'][0]}0101")
            stale_years = check_disclosures(conn, code, lst)
            db.log_fetch(conn, "dart_disclosures", code, code, True, len(lst))
            for y in stale_years:
                if y not in need["statement_years"]:
                    need["statement_years"].append(y)
                stats.notes.append(f"{y}년 정정 공시 발견 → 해당 연도만 다시 수집")
    except dart.DartUnavailable as e:
        stats.notes.append(f"공시 목록 점검 건너뜀: {e}")

    # 1-2) 업종코드(업종 평균 기준선용): DART 기업개황에서 한 번만
    try:
        row = conn.execute("SELECT induty_code FROM companies WHERE stock_code=?", (code,)).fetchone()
        if has_key and (row is None or not row["induty_code"]) and \
                not db.fetched_recently(conn, "dart_company", code, config.TTL_DAYS["dart_report_missing"]):
            stats.req("DART")
            info = dart.get_json("company.json", corp_code=_corp())
            if info.get("induty_code"):
                db.set_induty(conn, code, info["induty_code"])
                db.ensure_company(conn, code, info.get("stock_name") or info.get("corp_name"))
            db.log_fetch(conn, "dart_company", code, code, bool(info.get("induty_code")), 1)
    except dart.DartUnavailable as e:
        stats.notes.append(f"업종코드 미확보: {e}")

    # 2) 빠진 연도의 사업보고서
    for y in (need["statement_years"] if has_key else []):
        key = f"{code}:{y}:11011"
        if db.fetched_recently(conn, "dart_report", key, config.TTL_DAYS["dart_report_missing"]):
            continue
        try:
            stats.req("DART")
            d = dart.fetch_statement(_corp(), y, "11011")
            save_raw(code, d)
            n = ingest_statement(conn, code, d, stats=stats)
            db.log_fetch(conn, "dart_report", key, code, n > 0, n, d.get("message"))
        except dart.DartUnavailable as e:
            db.log_fetch(conn, "dart_report", key, code, False, 0, str(e))
            stats.notes.append(f"{y} 사업보고서 미확보: {e}")
            if "API_KEY" in str(e) or "접속 실패" in str(e):
                break

    # 3) 연말 주식 수
    for y in (need["share_years"] if has_key else []):
        key = f"{code}:{y}"
        if db.fetched_recently(conn, "dart_shares", key, config.TTL_DAYS["dart_report_missing"]):
            continue
        try:
            stats.req("DART")
            parsed = dart.parse_shares(dart.fetch_shares(_corp(), y))
            if parsed.get("common"):
                ingest_shares(conn, code, y, parsed, stats)
            db.log_fetch(conn, "dart_shares", key, code, bool(parsed.get("common")), len(parsed))
        except dart.DartUnavailable as e:
            db.log_fetch(conn, "dart_shares", key, code, False, 0, str(e))
            stats.notes.append(f"{y} 주식 수 미확보: {e}")
            break

    # 4) 현재 연도 분기·반기 실적(최신 보고서부터)
    if need["interim"] and has_key:
        for reprt in ("11014", "11012", "11013"):
            key = f"{code}:{P['current']}:{reprt}"
            if db.fetched_recently(conn, "dart_report", key, config.TTL_DAYS["dart_report_missing"]):
                continue
            try:
                stats.req("DART")
                d = dart.fetch_statement(_corp(), P["current"], reprt)
                save_raw(code, d)
                n = ingest_statement(conn, code, d, stats=stats)
                db.log_fetch(conn, "dart_report", key, code, n > 0, n, d.get("message"))
                if n > 0:
                    break
            except dart.DartUnavailable as e:
                db.log_fetch(conn, "dart_report", key, code, False, 0, str(e))
                stats.notes.append(f"{P['current']} {dart.REPRT_NAME[reprt]} 미확보: {e}")
                break

    # 4-2) 감가상각비·무형자산상각비·리스부채: 재무제표 본문에 없는 연도만 XBRL 원본(주석)에서 보충
    if has_key:
        try:
            ensure_xbrl(conn, code, P, stats)
        except dart.DartUnavailable as e:
            stats.notes.append(f"XBRL 원본 미확보: {e}")

    # 5) 주가: 빠진 연말 종가 또는 기준일 주가
    if need["price_years"] or need["asof_price"]:
        try:
            stats.req("Naver")
            pdata = prices.fetch_naver_daily(code, count=260 * (len(P["past"]) + 2))
            ingest_prices(conn, code, pdata, P["current"], stats)
            db.log_fetch(conn, "price", f"{code}:asof", code, True, len(pdata["rows"]))
        except prices.PriceUnavailable as e:
            db.log_fetch(conn, "price", f"{code}:asof", code, False, 0, str(e))
            stats.notes.append(f"주가 미확보: {e}")

    # 6) 컨센서스: OpenDART에는 컨센서스가 없다. WiseReport(네이버 기업정보, 3년) → FnGuide → 네이버 금융 순서로 시도.
    #    성공한 뒤 7일 동안은 다시 받지 않고, 실패하면 다음 실행 때 다시 시도한다. 시점별로 새 스냅숏을 쌓는다.
    if need["consensus"]:
        got, errs = 0, []
        for name, mod in (("WiseReport", wisereport), ("FnGuide", fnguide), ("Naver", naver)):
            try:
                stats.req(name)
                res = mod.fetch(code)
                url, items = res[0], res[1]
                if name == "WiseReport" and len(res) > 2:
                    record_cross_check(conn, code, res[2], url)
                if not items:
                    raise mod.ConsensusUnavailable("표는 읽었지만 추정치(E) 열이 없음")
                n = ingest_consensus(conn, code, items, name, db.today(), url)
                db.log_fetch(conn, "consensus_src", f"{code}:{name}", code, True, len(items), f"새 {n}건")
                got += len(items)
                stats.notes.append(f"컨센서스 {name}: {len(items)}건(새로 저장 {n}건, 대상 연도 "
                                   f"{sorted({i['target_year'] for i in items})})")
                break
            except mod.ConsensusUnavailable as e:
                errs.append(f"{name}: {e}")
                db.log_fetch(conn, "consensus_src", f"{code}:{name}", code, False, 0, str(e))
        db.log_fetch(conn, "consensus", code, code, got > 0, got, "; ".join(errs) or None)
        if not got:
            stats.notes.append("컨센서스 미확보(다음 실행 때 다시 시도): " + " / ".join(errs))

    # 7) 컨센서스 추이(상향·하향 확인용): 올해~내후년 매출·영업이익(+EPS·목표주가) 월별 값. 7일에 한 번.
    if not db.fetched_recently(conn, "consensus_hist", code, config.TTL_DAYS["consensus_hist"], ok_only=True):
        try:
            years = [P["current"]] + list(P["future"])
            for _ in range(len(years) * 2):
                stats.req("WiseReport")
            urls, rows = wisereport.history_rows(code, years)
            n = db.add_consensus_history(conn, code, rows, urls[0] if urls else None)
            ok = any(r[3] is not None and r[0] for r in rows)
            db.log_fetch(conn, "consensus_hist", code, code, ok, n, None if ok else "추이 자료 없음")
            stats.notes.append(f"컨센서스 추이: 새로 저장 {n}건(대상 {years})")
        except wisereport.ConsensusUnavailable as e:
            db.log_fetch(conn, "consensus_hist", code, code, False, 0, str(e))
            stats.notes.append(f"컨센서스 추이 미확보(다음 실행 때 다시 시도): {e}")

    # 8) 사업부 분석(화면 L): 주요제품 매출구성 → 공시 제목 → 뉴스 제목
    collect_business(conn, code, stats, has_key, _corp)
    return need


def import_seed_news(conn, code):
    """analyzer/seed/<코드>/news_titles.tsv (날짜<TAB>제목) → news 표. 이미 있으면 건너뛴다(오프라인에서도 동작)."""
    f = os.path.join(config.SEED_DIR, code, "news_titles.tsv")
    if not os.path.exists(f):
        return 0
    items = []
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0][:4].isdigit():
                items.append({"published": parts[0], "title": parts[1]})
    return db.add_news(conn, code, items, "KIS 뉴스 제목(Claude 연결로 수집, 2026-11-05)", "seed")


def collect_business(conn, code, stats, has_key=False, corp_fn=None):
    import datetime as _dt
    from . import segments
    from .collectors import news
    import_seed_news(conn, code)
    if not db.fetched_recently(conn, "segments", code, config.TTL_DAYS["segments"], ok_only=True):
        try:
            stats.req("WiseReport")
            url, as_of, rows = wisereport.products(code)
            n = db.add_segments(conn, code, as_of or db.today()[:7], [(nm, pct, None, "%", None) for nm, pct in rows],
                                "WiseReport 주요제품 매출구성", url)
            db.log_fetch(conn, "segments", code, code, True, n)
        except wisereport.ConsensusUnavailable as e:
            db.log_fetch(conn, "segments", code, code, False, 0, str(e))
            stats.notes.append(f"사업부 구성 미확보: {e}")
    today = _dt.date.today()
    since = today - _dt.timedelta(days=365)
    if has_key and corp_fn and not db.fetched_recently(conn, "dart_events", code, config.TTL_DAYS["dart_events"], ok_only=True):
        try:
            got = 0
            for ty, kind in (("B", "주요사항"), ("I", "거래소공시")):
                stats.req("OpenDART")
                lst = dart.fetch_disclosures(corp_fn(), since.strftime("%Y%m%d"), pblntf_ty=ty, max_pages=3)
                got += db.add_events(conn, code, [{"rcept_no": d["rcept_no"], "rcept_dt": d["rcept_dt"],
                                                   "title": (d.get("report_nm") or "").strip(),
                                                   "link": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + d["rcept_no"]}
                                                  for d in lst], kind)
            db.log_fetch(conn, "dart_events", code, code, True, got)
        except Exception as e:      # 공시 제목은 보조 자료라 실패해도 분석을 멈추지 않는다
            db.log_fetch(conn, "dart_events", code, code, False, 0, str(e)[:200])
    if not db.fetched_recently(conn, "news", code, config.TTL_DAYS["news"], ok_only=True):
        row = conn.execute("SELECT name FROM companies WHERE stock_code=?", (code,)).fetchone()
        name = row["name"] if row else code
        last = conn.execute("SELECT MAX(published) d FROM news WHERE stock_code=?", (code,)).fetchone()["d"]
        start = max(since, _dt.date.fromisoformat(last) - _dt.timedelta(days=7)) if last else since
        _, gs, _ = segments.groups(conn, code)
        queries = [name] + [f"{name} {kw}" for g in gs for kw in g["keywords"][:2]][:8]
        got, errs, src_used = 0, [], set()
        for i, q in enumerate(queries):
            try:
                if i == 0:
                    src, n, items = news.fetch(q, start)
                elif news.naver_key():
                    n, items = news.naver(q, start, max_pages=3)
                    src = "네이버 뉴스 API"
                else:                       # 키가 없으면 키워드 검색은 최근 100건만(요청 수를 줄이려고)
                    n, items = news.google(q, max(start, today - _dt.timedelta(days=31)))
                    src = "구글 뉴스 RSS"
                for _ in range(n):
                    stats.req(src)
                got += db.add_news(conn, code, items, src, q)
                src_used.add(src)
            except news.NewsUnavailable as e:
                errs.append(f"{q}: {e}")
                if i == 0:
                    break
        ok = not errs or got > 0
        db.log_fetch(conn, "news", code, code, ok, got, "; ".join(errs)[:300] or None)
        stats.notes.append(f"뉴스 제목: 새로 저장 {got}건({', '.join(sorted(src_used)) or '출처 없음'})" +
                           (f" · 실패 {len(errs)}건" if errs else ""))


def record_cross_check(conn, code, actuals, url):
    """WiseReport의 과거 실적(매출액·영업이익)을 교차 검증용 관측값으로 저장한다(검증 항목 10)."""
    for a in actuals:
        key = {"revenue": "acc_revenue", "op_income": "acc_op", "ebitda": "ebitda_wr"}.get(a["metric"])
        if key:
            db.add_observation(conn, code, a["target_year"], key, a["value"] * 1e8, unit="원",
                               basis=a.get("basis"), source="WiseReport", source_ref=url.split("&sDT")[0],
                               report_year=None, col="실적(억원 반올림)")
    conn.commit()


def _has_da(conn, code, y, per):
    for k in ("acc_da_total", "acc_dep"):
        r = db.current_fact(conn, code, y, k, per)
        if r is not None and r["value"] is not None:
            return True
    return False


def _report_rcept(conn, code, y, reprt):
    """그 연도·보고서 종류의 접수번호(정정 공시가 있으면 가장 최근). 공시 목록 → 이미 받은 재무제표 순으로 찾는다."""
    kind = {"11011": "사업보고서", "11012": "반기보고서", "11013": "분기보고서", "11014": "분기보고서"}[reprt]
    month = {"11011": "12", "11012": "06", "11013": "03", "11014": "09"}[reprt]
    r = conn.execute("SELECT rcept_no FROM disclosures WHERE stock_code=? AND report_nm LIKE ? ORDER BY rcept_no DESC LIMIT 1",
                     (code, f"%{kind}%({y}.{month})%")).fetchone()
    if r:
        return r["rcept_no"]
    per = dart.REPRT_PERIOD[reprt]
    r = conn.execute("SELECT source_ref FROM observations WHERE stock_code=? AND fiscal_year=? AND period=? AND "
                     "report_year=? AND source='DART' ORDER BY source_ref DESC LIMIT 1", (code, y, per, y)).fetchone()
    return r["source_ref"] if r else None


def ingest_xbrl(conn, code, rcept_no, report_year, values, stats=None):
    for (y, per), vals in values.items():
        for k, v in vals.items():
            db.add_observation(conn, code, y, k, v, period=per, unit="원", basis="CFS", source="DART",
                               source_ref=rcept_no, report_year=report_year, col="XBRL주석")
        select_facts(conn, code, y, per, via="DART XBRL 원본(주석)", stats=stats)
    conn.commit()


def ensure_xbrl(conn, code, P, stats):
    """감가상각비가 없는 연도만 그 연도 보고서의 XBRL을 받는다(한 파일에 당기·전기 2개 연도). 받은 파일은 보관."""
    from .collectors import dart_xbrl
    jobs = []
    for y in sorted(P["past"], reverse=True):
        if not _has_da(conn, code, y, "FY"):
            jobs.append((y, "11011"))
    cy = P["current"]
    per = next((p for p in ("Q3", "H1", "Q1") if db.current_fact(conn, code, cy, "acc_revenue", p)), None)
    if per and not _has_da(conn, code, cy, per):
        jobs.append((cy, {"Q3": "11014", "H1": "11012", "Q1": "11013"}[per]))
    for y, reprt in jobs:
        per = dart.REPRT_PERIOD[reprt]
        if _has_da(conn, code, y, per):          # 앞서 받은 파일의 전기 값으로 채워졌으면 건너뜀
            continue
        rc = _report_rcept(conn, code, y, reprt)
        if not rc:
            stats.notes.append(f"{y} {dart.REPRT_NAME[reprt]} 접수번호를 몰라 XBRL을 받지 못함(공시 목록 확인 후 다시 시도)")
            continue
        key = f"{code}:{rc}"
        if db.fetched_recently(conn, "dart_xbrl", key, config.TTL_DAYS["dart_report_missing"]) and \
                not os.path.exists(dart_xbrl.path_for(code, rc)):
            continue
        try:
            if not os.path.exists(dart_xbrl.path_for(code, rc)):
                stats.req("DART")
            vals = dart_xbrl.fetch(code, rc, reprt)
            ingest_xbrl(conn, code, rc, y, vals, stats)
            db.log_fetch(conn, "dart_xbrl", key, code, True, len(vals))
        except (dart.DartUnavailable, ValueError) as e:
            db.log_fetch(conn, "dart_xbrl", key, code, False, 0, str(e))
            stats.notes.append(f"{y} XBRL 원본 미확보: {e}")
            if "API_KEY" in str(e) or "접속 실패" in str(e):
                break


def check_disclosures(conn, code, lst):
    """공시 목록을 저장하고, 이미 쓰고 있는 보고서에 대해 새 정정 공시가 있으면 그 연도를 돌려준다."""
    stale = set()
    for it in lst:
        nm = it.get("report_nm", "")
        mt = re.search(r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)", nm)
        if not mt:
            continue
        year = int(mt.group(2))
        corr = 1 if re.search(r"\[(기재|첨부)?정정\]", nm) else 0
        new = conn.execute("INSERT OR IGNORE INTO disclosures VALUES (?,?,?,?,?,?,?,?)",
                           (it["rcept_no"], code, nm, it.get("rcept_dt"), year,
                            "11011" if mt.group(1) == "사업보고서" else None, corr, db.now())).rowcount
        if new and corr and mt.group(1) == "사업보고서":
            for r in conn.execute("SELECT id FROM facts WHERE stock_code=? AND fiscal_year=? AND period='FY' AND "
                                  "is_current=1 AND source='DART' AND metric LIKE 'acc_%'", (code, year)):
                db.set_status(conn, r["id"], "STALE", f"정정 공시 {it['rcept_no']} 확인, 재수집 필요")
            conn.execute("DELETE FROM fetch_log WHERE source='dart_report' AND key=?", (f"{code}:{year}:11011",))
            stale.add(year)
    conn.commit()
    return sorted(stale)


# ---------------------------------------------------------------- 지표 계산
def _v(fm, y, k):
    r = fm.get((y, k))
    return None if r is None else r["value"]


def _st(fm, y, k):
    r = fm.get((y, k))
    return None if r is None else r["status"]


def shares_for_bps(fm, y):
    """BPS용 유통주식수 = (보통주+우선주) − 자기주식. 공식 주식 수가 없으면 None."""
    c = _v(fm, y, "shares_common")
    if c is None:
        return None
    return c + (_v(fm, y, "shares_pref") or 0) - (_v(fm, y, "treasury_common") or 0) - (_v(fm, y, "treasury_pref") or 0)


def compute(conn, code, P, stats=None):
    """원천 계정으로 7개 지표를 계산해 facts(source='derived')로 저장한다."""
    fm = db.fact_map(conn, code)
    # 역산 주식수(참고용): 지배순이익 / 기본 EPS
    derived_shares = {}
    for y in [P["past"][0] - 1] + P["past"]:
        ni, eps = _v(fm, y, "acc_ni_owner"), _v(fm, y, "acc_eps_basic")
        if ni is not None and eps:
            derived_shares[y] = ni / eps
            db.put_fact(conn, code, y, "shares_derived", round(ni / eps), unit="주", basis="-", data_type="DERIVED",
                        source="derived", source_ref="acc_ni_owner/acc_eps_basic",
                        status_note="공식 주식 수가 없을 때만 쓰는 참고값. 우선주·가중평균 영향으로 오차 있음")
    conn.commit()
    fm = db.fact_map(conn, code)
    # 역산 주식수: EPS 절댓값이 300원 이상인 해는 그해 값, 작은 해는 반올림 오차가 커서 신뢰 연도 중앙값을 쓴다
    reliable = [derived_shares[y] for y in derived_shares if abs(_v(fm, y, "acc_eps_basic")) >= 300]
    med_all = statistics.median(reliable or list(derived_shares.values())) if derived_shares else None

    def dshares(y):
        if y in derived_shares and abs(_v(fm, y, "acc_eps_basic")) >= 300:
            return derived_shares[y], f"{y}년 역산 주식수 {derived_shares[y]:,.0f}주"
        return med_all, f"역산 주식수 중앙값 {med_all:,.0f}주" if med_all else None
    med = med_all

    def put(y, metric, value, inputs, period="FY", data_type="ACTUAL", extra_note=None):
        sts = [(_st(fmp, y2, k) if per == "FY" else _stp(y2, k, per)) for (y2, k, per) in inputs]
        if value is None:
            status, note = "MISSING", "입력값 없음: " + ", ".join(f"{y2}:{k}" for (y2, k, per), s in zip(inputs, sts) if s is None)
        elif all(s == "VERIFIED" for s in sts):
            status, note = "VERIFIED", None
        elif any(s == "CONFLICT" for s in sts):
            status, note = "CONFLICT", "입력값 불일치"
        else:
            status, note = "UNVERIFIED", "입력값 일부 미검증"
        if extra_note:
            note = (note + " · " if note else "") + extra_note
        unit = config.METRIC_UNIT[metric]
        r = db.put_fact(conn, code, y, metric, None if value is None else round(value, 4), period=period, unit=unit,
                        basis="CFS", data_type=data_type, source="derived",
                        source_ref="; ".join(f"{y2}:{k}" for (y2, k, per) in inputs), status=status,
                        status_note=note, change_reason="입력값 변경으로 재계산")
        if r == "same":
            cur = db.current_fact(conn, code, y, metric, period)
            if cur and (cur["status"] != status or cur["status_note"] != note) and cur["value"] is not None:
                db.set_status(conn, cur["id"], status, note)
        if stats:
            stats.put(r)

    fmp = fm
    interim = {(r["fiscal_year"], r["metric"], r["period"]): r for r in conn.execute(
        "SELECT * FROM facts WHERE stock_code=? AND period IN ('Q1','H1','Q3','ASOF') AND is_current=1", (code,))}

    def _stp(y, k, per):
        r = interim.get((y, k, per))
        return None if r is None else r["status"]

    def _vp(y, k, per):
        r = interim.get((y, k, per))
        return None if r is None else r["value"]

    for y in P["past"]:
        rev, op = _v(fm, y, "acc_revenue"), _v(fm, y, "acc_op")
        put(y, "revenue", rev / 1e8 if rev is not None else None, [(y, "acc_revenue", "FY")])
        put(y, "op_margin", op / rev * 100 if rev and op is not None else None,
            [(y, "acc_op", "FY"), (y, "acc_revenue", "FY")])
        put(y, "eps", _v(fm, y, "acc_eps_basic"), [(y, "acc_eps_basic", "FY")])
        eqo, liab, eq = _v(fm, y, "acc_eq_owner"), _v(fm, y, "acc_liab"), _v(fm, y, "acc_eq")
        sh = shares_for_bps(fm, y)
        if sh:
            put(y, "bps", eqo / sh if eqo is not None else None, [(y, "acc_eq_owner", "FY"), (y, "shares_common", "FY")])
        elif eqo is not None and med:
            ds, dn = dshares(y)
            put(y, "bps", eqo / ds, [(y, "acc_eq_owner", "FY"), (y, "shares_derived", "FY")],
                extra_note=f"주식 수 미확보로 {dn} 사용(주의)")
        else:
            put(y, "bps", None, [(y, "acc_eq_owner", "FY"), (y, "shares_common", "FY")])
        prev = _v(fm, y - 1, "acc_eq_owner")
        nio = _v(fm, y, "acc_ni_owner")
        if prev is not None and eqo is not None and nio is not None:
            put(y, "roe", nio / ((prev + eqo) / 2) * 100,
                [(y, "acc_ni_owner", "FY"), (y, "acc_eq_owner", "FY"), (y - 1, "acc_eq_owner", "FY")])
        elif eqo and nio is not None:
            put(y, "roe", nio / eqo * 100, [(y, "acc_ni_owner", "FY"), (y, "acc_eq_owner", "FY")],
                extra_note="전년 자본이 없어 기말 자본으로 계산(평균 아님)")
        else:
            put(y, "roe", None, [(y, "acc_ni_owner", "FY"), (y, "acc_eq_owner", "FY")])
        put(y, "debt_ratio", liab / eq * 100 if eq and liab is not None else None,
            [(y, "acc_liab", "FY"), (y, "acc_eq", "FY")])
        px = _v(fm, y, "price_close")
        c = _v(fm, y, "shares_common")
        if px is not None and c:
            put(y, "market_cap", px * c / 1e8, [(y, "price_close", "FY"), (y, "shares_common", "FY")],
                extra_note="보통주 기준(우선주 제외)")
        elif px is not None and med:
            ds, dn = dshares(y)
            put(y, "market_cap", px * ds / 1e8, [(y, "price_close", "FY"), (y, "shares_derived", "FY")],
                extra_note=f"주식 수 미확보로 {dn} 사용(주의, 우선주 포함 가능)")
        else:
            put(y, "market_cap", None, [(y, "price_close", "FY"), (y, "shares_common", "FY")])

    # 현재 연도: 가장 최근 분기·반기 누적 실적(PARTIAL)
    cy = P["current"]
    per = next((p for p in ("Q3", "H1", "Q1") if (cy, "acc_revenue", p) in interim), None)
    if per:
        rev, op = _vp(cy, "acc_revenue", per), _vp(cy, "acc_op", per)
        put(cy, "revenue", rev / 1e8 if rev else None, [(cy, "acc_revenue", per)], period=per, data_type="PARTIAL",
            extra_note=f"{per} 누적(연간 아님)")
        put(cy, "op_margin", op / rev * 100 if rev and op is not None else None,
            [(cy, "acc_op", per), (cy, "acc_revenue", per)], period=per, data_type="PARTIAL", extra_note=f"{per} 누적")
        put(cy, "eps", _vp(cy, "acc_eps_basic", per), [(cy, "acc_eps_basic", per)], period=per, data_type="PARTIAL",
            extra_note=f"{per} 누적(연간 아님)")
        liab, eq, eqo = _vp(cy, "acc_liab", per), _vp(cy, "acc_eq", per), _vp(cy, "acc_eq_owner", per)
        put(cy, "debt_ratio", liab / eq * 100 if eq and liab is not None else None,
            [(cy, "acc_liab", per), (cy, "acc_eq", per)], period=per, data_type="PARTIAL", extra_note=f"{per}말")
        last = max([y for y in P["past"] if shares_for_bps(fm, y)] or [None], key=lambda v: v or 0)
        sh = shares_for_bps(fm, last) if last else None
        if eqo is not None and (sh or med):
            put(cy, "bps", eqo / (sh or med), [(cy, "acc_eq_owner", per), (last or cy - 1, "shares_common" if sh else "shares_derived", "FY")],
                period=per, data_type="PARTIAL",
                extra_note=f"{per}말 자본 / " + (f"{last}년말 주식 수" if sh else f"역산 주식수 중앙값(주의)"))
    px = _vp(cy, "price_close", "ASOF")
    if px is not None:
        last = max([y for y in P["past"] if _v(fm, y, "shares_common")] or [0])
        c = _v(fm, last, "shares_common") if last else None
        put(cy, "market_cap", px * (c or med) / 1e8 if (c or med) else None,
            [(cy, "price_close", "ASOF"), (last or cy - 1, "shares_common" if c else "shares_derived", "FY")],
            period="ASOF", data_type="PARTIAL",
            extra_note=f"기준일 {interim[(cy, 'price_close', 'ASOF')]['as_of']} 주가" + ("" if c else " × 역산 주식수(주의)"))
    conn.commit()


# ---------------------------------------------------------------- 전체 실행
def analyze(code, base_year=None, offline=False, conn=None, use_seed=True, fresh=False):
    from . import validate
    P = config.periods(base_year)
    own = conn is None
    conn = conn or db.connect()
    stats = Stats()
    existed = conn.execute("SELECT COUNT(*) FROM facts WHERE stock_code=?", (code,)).fetchone()[0]
    db.ensure_company(conn, code)
    if use_seed and not db.last_fetch(conn, "seed", code) and os.path.isdir(os.path.join(config.SEED_DIR, code)):
        import_seed(conn, code, P["current"], stats)
        stats.notes.append("저장 원본(seed) 가져오기 완료")
    meta = os.path.join(config.SEED_DIR, code, "company.json")
    row = conn.execute("SELECT induty_code FROM companies WHERE stock_code=?", (code,)).fetchone()
    if os.path.exists(meta) and (row is None or not row["induty_code"]):
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
        db.ensure_company(conn, code, m.get("name"), m.get("corp_code"))
        if m.get("induty_code"):
            db.set_induty(conn, code, m["induty_code"])
    if reparse_cached(conn, code, stats):
        stats.notes.append(f"해석 규칙 v{dart.PARSER_VERSION}로 보관 원본 재해석(외부 요청 없음)")
    stats.reused = existed
    need = collect(conn, code, P, stats, offline=offline, fresh=fresh)
    validate.validate_accounts(conn, code, P)
    compute(conn, code, P, stats)
    report = validate.run(conn, code, P)
    from . import valuation
    val = valuation.run(conn, code, P)
    report["items"] += val["checks"]
    for c in val["checks"]:
        report["summary"][c["level"]] = report["summary"].get(c["level"], 0) + 1
    if own:
        conn.close()
    return {"periods": P, "stats": stats.as_dict(), "need": need, "validation": report, "had_data": existed > 0}
