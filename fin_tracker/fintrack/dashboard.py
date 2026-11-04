"""화면 자료 만들기와 공유용 HTML 내보내기.

- 화면 서버는 collect()가 만든 JSON을 받아 그린다.
- export()는 같은 화면을 파일 한 장(<작업 폴더>/output/<종목>.html)으로 저장한다. 인터넷 없이 열린다.
"""
import json, os
from . import config, paths, valuation

TEMPLATE = os.path.join(os.path.dirname(__file__), "app.html")
VENDOR = os.path.join(paths.CODE_DIR, "vendor", "chart.umd.4.4.1.js")
CDN = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def collect(store, code, p, series, run_id, w=None):
    c = store.company(code)
    con = store.con
    metrics = [{"key": k, "name": n, "unit": u,
                **{slot: {str(y): v for y, v in series[k][slot].items()} for slot in ("actual", "partial", "consensus", "unverified")}}
               for k, n, u in config.METRICS]
    raw = {}
    for r in store.current(code, kind="ACTUAL", period="FY"):
        raw.setdefault(str(r["fiscal_year"]), {})[r["item"]] = {"v": r["value"], "status": r["status"], "ref": r["source_ref"], "basis": r["basis"]}
    counts = _rows(con.execute("SELECT status, COUNT(*) n FROM observations WHERE stock_code=? AND is_current=1 GROUP BY status", (code,)))
    last = con.execute("SELECT MAX(collected_at) c, MAX(verified_at) v FROM observations WHERE stock_code=?", (code,)).fetchone()
    runs = _rows(con.execute("SELECT * FROM runs WHERE stock_code=? AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 5", (code,)))
    for r in runs:
        r["summary"] = json.loads(r["summary"]) if r["summary"] else None
        r["fetches"] = _rows(con.execute("SELECT source,target,channel,result,detail FROM fetch_log WHERE run_id=? ORDER BY id", (r["run_id"],)))
    checks = _rows(con.execute(
        "SELECT fiscal_year, item, check_name, result, detail FROM validation_results WHERE run_id=? ORDER BY "
        "CASE result WHEN 'FAIL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, fiscal_year", (run_id,)))
    changes = _rows(con.execute("SELECT * FROM change_log WHERE stock_code=? ORDER BY id DESC LIMIT 200", (code,)))
    snaps = _rows(con.execute(
        "SELECT fiscal_year, item, value, source, as_of, collected_at, status, status_note, version, is_current, extra "
        "FROM observations WHERE stock_code=? AND kind='CONSENSUS' AND item IN ('revenue','op','eps','bps','roe','debt_ratio') "
        "ORDER BY collected_at DESC, fiscal_year, item LIMIT 400", (code,)))
    gaps = [{"year": y, "metric": m["name"]} for y in [p["base"]] + p["future"] for m in metrics
            if str(y) not in m["consensus"] and m["key"] != "market_cap"]
    vser, vtable, vassume = valuation.build(store, code, p, series, w)
    val = [{"key": k, "name": n, "unit": u, "group": grp,
            **{slot: {str(y): v for y, v in vser[k][slot].items()} for slot in ("actual", "partial", "consensus", "unverified")}}
           for k, n, u, grp in valuation.VAL_METRICS]
    basis = {r["basis"] for r in store.current(code, kind="ACTUAL", period="FY") if r["basis"]}
    return {"company": {"code": code, "name": c["name"], "market": c["market"] or ""},
            "basis": "별도" if basis == {"OFS"} else "연결",
            "base": p["base"], "actualYears": p["actual"], "futureYears": p["future"],
            "metrics": metrics, "raw": raw, "counts": counts,
            "lastCollected": last["c"], "lastVerified": last["v"], "runs": runs, "checks": checks,
            "changes": changes, "snapshots": snaps, "gaps": gaps,
            "valuation": val, "valTable": {str(y): r for y, r in vtable.items()}, "assumptions": vassume}


def render_html(data=None, token="", static=False):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    boot = {"static": static, "token": token, "data": data}
    html = tpl.replace("__BOOT__", json.dumps(boot, ensure_ascii=False).replace("</", "<\\/"))
    title = f"{data['company']['name']} 재무 추이" if (static and data) else "기업 재무 추이"
    return html.replace("__TITLE__", title)


def _inline_vendor(html):
    if os.path.exists(VENDOR):
        return html.replace(CDN, "<script>" + open(VENDOR, encoding="utf-8").read().replace("</script", "<\\/script") + "</script>")
    return html


def full_document(fragment):
    return ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>\n'
            + fragment + "\n</body></html>\n")


def export(store, w, code, p, series, run_id):
    data = collect(store, code, p, series, run_id, w)
    frag = render_html(data, static=True)
    with open(os.path.join(w.output, f"{code}.fragment.html"), "w", encoding="utf-8") as f:
        f.write(frag)
    path = os.path.join(w.output, f"{code}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full_document(_inline_vendor(frag)))
    return path
