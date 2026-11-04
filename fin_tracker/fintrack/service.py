"""화면 서버와 명령줄이 함께 쓰는 작업 단위."""
import json, os, shutil
from datetime import datetime

from . import config, dashboard, paths
from .db import Store, now
from .metrics import build
from .pipeline import Collector
from .validate import Validator


def store(w):
    return Store(w.db)


def analyze(w, code, base_year=None, refresh=False, offline=False, reverify=False, progress=None, settings=None):
    """한 종목 수집·검증·화면 파일 생성. 반환: 요약 dict"""
    say = progress or (lambda m: None)
    s = store(w)
    try:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        p = config.periods(base_year)
        existed = bool(s.con.execute("SELECT 1 FROM runs WHERE stock_code=? LIMIT 1", (code,)).fetchone())
        s.con.execute("INSERT INTO runs(run_id,stock_code,base_year,started_at) VALUES(?,?,?,?)", (run_id, code, p["base"], now()))
        say(f"{code} · 기준연도 {p['base']} · {'갱신' if refresh else ('기존 자료 확인' if existed else '첫 분석')}")
        c = Collector(s, w, code, p["base"], run_id, offline=offline, refresh=refresh, progress=say, settings=settings)
        st = c.run_all()
        say("검증 중")
        v = Validator(s, code, p, run_id, reverify=reverify).run()
        series = build(s, code, p)
        summary = {"외부·원본 조회": st["fetched"], "재사용(조회 생략)": st["reused"], "실패": st["failed"], "신규 저장": st["inserted"],
                   "변경 없음": st["same"], "새 버전": st["new_version"], "미확보 계정": len(st["missing"]), "검증": v}
        s.con.execute("UPDATE runs SET finished_at=?, summary=? WHERE run_id=?", (now(), json.dumps(summary, ensure_ascii=False), run_id))
        s.commit()
        out = dashboard.export(s, w, code, p, series, run_id)
        say(f"끝 · 조회 {st['fetched']} · 재사용 {st['reused']} · 실패 {st['failed']}")
        return {"run_id": run_id, "summary": summary, "export": out, "name": c.info["name"]}
    finally:
        s.con.close()


def company_data(w, code, base_year=None):
    s = store(w)
    try:
        if not s.company(code):
            return None
        last = s.con.execute("SELECT run_id, base_year FROM runs WHERE stock_code=? AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                             (code,)).fetchone()
        if not last:
            return None
        p = config.periods(base_year or last["base_year"])
        return dashboard.collect(s, code, p, build(s, code, p), last["run_id"], w)
    finally:
        s.con.close()


def companies(w):
    s = store(w)
    try:
        rows = s.con.execute(
            "SELECT c.stock_code code, c.name, MAX(r.finished_at) last_run FROM companies c JOIN runs r ON r.stock_code=c.stock_code "
            "WHERE r.finished_at IS NOT NULL GROUP BY c.stock_code ORDER BY MIN(r.started_at)").fetchall()
        return [dict(r) for r in rows]
    finally:
        s.con.close()


def import_sample(w):
    """저장소에 든 한솔제지 예시 원본을 작업 폴더로 복사한다(인증키 없이 화면을 보는 용도)."""
    src = os.path.join(paths.CODE_DIR, "sample")
    for sub in ("raw/dart", "raw/wisereport", "manual"):
        a = os.path.join(src, sub)
        if os.path.isdir(a):
            for f in os.listdir(a):
                dst = os.path.join(w.root, sub, f)
                if not os.path.exists(dst):
                    shutil.copy2(os.path.join(a, f), dst)
    with open(os.path.join(src, "companies.json"), encoding="utf-8") as f:
        meta = json.load(f)
    s = store(w)
    for code, c in meta.items():
        if code.startswith("_"):
            continue
        s.upsert_company(code, c["corp_code"], c["name"], c.get("market"), c.get("face_value"))
    s.commit(); s.con.close()
    return [k for k in meta if not k.startswith("_")]
