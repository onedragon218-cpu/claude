"""DB → 한 장짜리 HTML 화면.

output/<종목>.html          : 브라우저에서 바로 여는 완성 문서
output/<종목>.fragment.html : 같은 내용, 문서 껍데기 없이(Artifact 게시용)
"""
import json, os
from . import config

TEMPLATE = os.path.join(os.path.dirname(__file__), "template.html")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def collect(store, code, p, series, run_id):
    c = store.company(code)
    con = store.con
    metrics = []
    for key, name, unit in config.METRICS:
        s = series[key]
        metrics.append({"key": key, "name": name, "unit": unit,
                        **{slot: {str(y): v for y, v in s[slot].items()} for slot in ("actual", "partial", "consensus", "unverified")}})
    raw = {}
    for r in store.current(code, kind="ACTUAL", period="FY"):
        raw.setdefault(str(r["fiscal_year"]), {})[r["item"]] = {"v": r["value"], "status": r["status"], "ref": r["source_ref"]}
    counts = _rows(con.execute(
        "SELECT status, COUNT(*) n FROM observations WHERE stock_code=? AND is_current=1 GROUP BY status", (code,)))
    by_kind = _rows(con.execute(
        "SELECT kind, status, COUNT(*) n FROM observations WHERE stock_code=? AND is_current=1 GROUP BY kind, status", (code,)))
    last = con.execute("SELECT MAX(collected_at) c, MAX(verified_at) v FROM observations WHERE stock_code=?", (code,)).fetchone()
    runs = _rows(con.execute("SELECT * FROM runs WHERE stock_code=? ORDER BY started_at", (code,)))
    for r in runs:
        r["summary"] = json.loads(r["summary"]) if r["summary"] else None
        r["fetches"] = _rows(con.execute("SELECT source,target,channel,result,detail FROM fetch_log WHERE run_id=? ORDER BY id", (r["run_id"],)))
    checks = _rows(con.execute(
        "SELECT fiscal_year, item, check_name, result, detail FROM validation_results WHERE run_id=? ORDER BY "
        "CASE result WHEN 'FAIL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, fiscal_year", (run_id,)))
    changes = _rows(con.execute("SELECT * FROM change_log WHERE stock_code=? ORDER BY id DESC", (code,)))
    snaps = _rows(con.execute(
        "SELECT fiscal_year, item, value, source, as_of, collected_at, status, status_note, version, is_current, extra "
        "FROM observations WHERE stock_code=? AND kind='CONSENSUS' ORDER BY fiscal_year, item, collected_at", (code,)))
    gaps = []
    for y in [p["base"]] + p["future"]:
        for m in metrics:
            if str(y) not in m["consensus"] and not (m["key"] == "market_cap" and str(y) in m["actual"]):
                gaps.append({"year": y, "metric": m["name"]})
    return {"company": {"code": code, "name": c["name"], "market": c["market"]},
            "base": p["base"], "actualYears": p["actual"], "futureYears": p["future"],
            "metrics": metrics, "raw": raw, "counts": counts, "byKind": by_kind,
            "lastCollected": last["c"], "lastVerified": last["v"], "runs": runs, "checks": checks,
            "changes": changes, "snapshots": snaps, "gaps": gaps}


def write(store, code, p, series, run_id):
    data = collect(store, code, p, series, run_id)
    tpl = open(TEMPLATE, encoding="utf-8").read()
    frag = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
              .replace("__TITLE__", f"{data['company']['name']} 재무 추이")
    out_dir = os.path.join(config.ROOT, "output")
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, f"{code}.fragment.html")
    open(fpath, "w", encoding="utf-8").write(frag)
    # 로컬 파일은 인터넷 없이도 열리도록 Chart.js를 문서 안에 넣는다(Artifact용 조각은 cdnjs 사용).
    cdn = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'
    vend = os.path.join(config.ROOT, "vendor", "chart.umd.4.4.1.js")
    if os.path.exists(vend):
        frag = frag.replace(cdn, "<script>" + open(vend, encoding="utf-8").read().replace("</script", "<\\/script") + "</script>")
    full = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>\n'
            + frag + "\n</body></html>\n")
    path = os.path.join(out_dir, f"{code}.html")
    open(path, "w", encoding="utf-8").write(full)
    return path
