"""화면 R「노드 우호 판정」· S「사업부 피어 비교」 — 자료 모으기·고르기 (지시서 2026-11-15, 1단계)

우선순위: ① 앱이 스스로 받은 값(source='self') ② 사용자 글(user) ③ AI 초안(ai) ④ 작업 폴더(doca_seed, 빈칸 채우기)
- 작업 폴더 값은 최신이 아닐 수 있어 날짜·출처를 늘 보여 주고, 앱이 받은 값이 있으면 그 값이 이긴다.
- 값은 덮어쓰지 않고 버전으로 쌓는다(같은 출처·같은 칸에서 내용이 바뀌면 새 버전, 이전은 is_current=0).
작업 폴더(사용자 PC): {doca_dir}\\엔진\\핵심\\ — 한장조사\\{종목코드}.json, 피어그룹.csv, 피어그룹_해외.csv, 피어_부문.csv,
  가지키팩터.csv, 키팩터원장.csv, 병목지도.csv, 인과개별화.csv (형식은 지시서 0절. 개발 환경에서는 실제 파일을 열지 못해
  지시서의 열 이름대로 읽고, 열 이름이 다르면 그 파일은 건너뛰고 화면 B에 알린다)
리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님
"""
import csv
import datetime as dt
import glob
import html
import io
import json
import os
import re

from . import config, db

PRIORITY = {"self": 0, "user": 1, "ai": 2, "doca_seed": 3}
SOURCE_NAME = {"self": "자체 수집", "user": "사용자", "ai": "AI 초안", "doca_seed": "작업 폴더"}
STALE_DAYS = {"doca_seed": 90, "ai": 90, "signal": 45}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
VERDICTS = ["순풍", "순풍(약)", "확인 필요", "약한 역풍", "역풍"]
DISCLAIMER = "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"


# ---------------------------------------------------------------- 설정(작업 폴더 경로)
def _settings_file():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "settings.json")


def default_doca_dir():
    """사용자가 알려 준 작업 폴더 위치(2026-11-15): C:\\Users\\{사용자}\\claude 작업"""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "claude 작업")


def get_doca_dir():
    try:
        with open(_settings_file(), encoding="utf-8-sig") as f:
            d = json.load(f).get("doca_dir")
            if d:
                return d
    except (OSError, ValueError):
        pass
    return default_doca_dir() if os.path.isdir(default_doca_dir()) else None


def set_doca_dir(path):
    f = _settings_file()
    try:
        with open(f, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    d["doca_dir"] = path
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)


FILES = ["가지키팩터.csv", "피어그룹.csv", "피어그룹_해외.csv", "피어_부문.csv", "키팩터원장.csv", "병목지도.csv", "인과개별화.csv"]


def _is_core(c):
    return os.path.isdir(c) and (os.path.exists(os.path.join(c, "피어그룹.csv")) or os.path.isdir(os.path.join(c, "한장조사")))


def core_dir(root):
    """작업 폴더에서 자료 폴더 찾기. 지정한 폴더 자체 → 엔진\\핵심 → 핵심 → 하위 5단계까지(이름이 '핵심'이 아니어도
    피어그룹.csv 또는 한장조사 폴더가 있으면 그 폴더). 여러 곳이면 '핵심'이라는 이름을 먼저."""
    if not root:
        return None
    root = os.path.expandvars(os.path.expanduser(str(root).strip().strip('"')))
    for c in (root, os.path.join(root, "엔진", "핵심"), os.path.join(root, "핵심")):
        if _is_core(c):
            return c
    if not os.path.isdir(root):
        return None
    base, found = root.rstrip("\\/").count(os.sep), []
    for dp, dns, _ in os.walk(root):
        if dp.count(os.sep) - base >= 5:
            dns[:] = []
            continue
        dns[:] = sorted(d for d in dns if not d.startswith((".", "__")) and d not in ("node_modules", "venv", ".venv"))
        if _is_core(dp):
            found.append(dp)
            dns[:] = []
    found.sort(key=lambda d: (os.path.basename(d) != "핵심", d.count(os.sep)))
    return found[0] if found else None


def inspect(root, code=None):
    """작업 폴더 진단: 찾은 자료 폴더, 파일별 있음/줄 수, 이 기업 자료가 있는지"""
    core = core_dir(root)
    out = {"root": root, "exists": bool(root and os.path.isdir(os.path.expandvars(os.path.expanduser(str(root).strip().strip('"'))))),
           "core": core, "files": [], "json_n": 0, "code_json": None, "code_peer": None}
    if not core:
        return out
    js = glob.glob(os.path.join(core, "한장조사", "*.json"))
    out["json_n"] = len(js)
    if code:
        out["code_json"] = os.path.exists(os.path.join(core, "한장조사", f"{code}.json"))
    for n in FILES:
        f = os.path.join(core, n)
        if os.path.exists(f):
            try:
                rows, cols = read_csv(f)
                item = {"name": n, "rows": len(rows), "cols": cols[:12]}
                if n == "피어그룹.csv" and code:
                    out["code_peer"] = sum(1 for r in rows if code6(r.get("대상")) == code)
            except Exception as e:     # noqa: BLE001
                item = {"name": n, "error": f"{type(e).__name__}: {e}"[:120]}
        else:
            item = {"name": n, "missing": True}
        out["files"].append(item)
    return out


# ---------------------------------------------------------------- 버전으로 쌓기
KEYS = {"nodes": ("node_id",), "company_nodes": ("stock_code", "seq"), "node_signal_defs": ("node_id", "signal_key"),
        "node_signals": ("node_id", "signal_key", "period"), "node_judgments": ("stock_code", "seq"),
        "company_node_verdict": ("stock_code",), "segment_fin": ("stock_code", "fy", "period", "segment"),
        "peer_groups": ("target", "segment", "peer_code", "peer_name"), "peer_foreign": ("target", "segment", "name"),
        "doca_refs": ("kind", "row_key")}


def put(conn, table, keys, vals, source, source_ref=None, as_of=None):
    """같은 출처·같은 칸의 현재 값과 다르면 새 버전. 반환: 'new'|'changed'|'same'"""
    kcols = KEYS[table]
    where = " AND ".join(f"{k} IS ?" for k in kcols)
    cur = conn.execute(f"SELECT * FROM {table} WHERE {where} AND source=? AND is_current=1 ORDER BY id DESC LIMIT 1",
                       [keys[k] for k in kcols] + [source]).fetchone()
    if cur is not None and all(cur[k] == v for k, v in vals.items()):
        return "same"
    ver = 1
    if cur is not None:
        conn.execute(f"UPDATE {table} SET is_current=0 WHERE id=?", (cur["id"],))
        ver = cur["version"] + 1
    cols = list(kcols) + list(vals) + ["source", "source_ref", "as_of", "collected_at", "is_current", "version"]
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [keys[k] for k in kcols] + list(vals.values()) + [source, source_ref, as_of, db.now(), 1, ver])
    return "changed" if cur is not None else "new"


def pick(rows):
    """같은 칸의 여러 출처 중 우선순위가 가장 높은 것(self > user > ai > doca_seed)"""
    rows = [dict(r) for r in rows]
    return min(rows, key=lambda r: (PRIORITY.get(r["source"], 9), -(r.get("id") or 0))) if rows else None


def stale(row, kind=None, today=None):
    today = today or dt.date.today()
    lim = STALE_DAYS.get(kind or row.get("source"))
    if not lim or not row.get("as_of"):
        return False
    try:
        return (today - dt.date.fromisoformat(str(row["as_of"])[:10])).days > lim
    except ValueError:
        return False


# ---------------------------------------------------------------- 작업 폴더 읽기
def strip_tag(s):
    """<span class='tag t-good'>순풍(약)</span> → 순풍(약)"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(s or "")))).strip()


def code6(s):
    s = str(s or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(6) if s.isdigit() and len(s) <= 6 else s


def _num(s):
    try:
        s = str(s).replace(",", "").replace("%", "").strip()
        return float(s) if s not in ("", "-", "nan", "None") else None
    except ValueError:
        return None


def read_csv(path):
    """utf-8-sig, 안 되면 cp949(엑셀 저장본). 반환: (행 목록, 열 이름)"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    rd = csv.DictReader(io.StringIO(text))
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rd]
    return rows, [c.strip() for c in (rd.fieldnames or [])]


def _mtime(path):
    return dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def split_seq(name, i):
    """'③ 인쇄용지' → (3, '인쇄용지'), 번호가 없으면 (i, 이름)"""
    name = str(name or "").strip()
    if name and name[0] in CIRCLED:
        return CIRCLED.index(name[0]) + 1, name[1:].strip()
    m = re.match(r"^\(?(\d{1,2})[\).]\s*(.+)$", name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return i, name


def import_doca(conn, root, today=None):
    """작업 폴더를 읽어 새 표에 넣는다(source='doca_seed'). 반환: {파일: {'rows','new','changed','same','skip'}}, 메모"""
    core = core_dir(root) if root else None
    if not core:
        return {}, [f"작업 폴더를 찾지 못함: {root}"]
    rep, notes = {}, []
    year_from = (today or dt.date.today()).year

    def tally(name, r):
        t = rep.setdefault(name, {"rows": 0, "new": 0, "changed": 0, "same": 0})
        t["rows"] += 1
        t[r] = t.get(r, 0) + 1

    def need(path, cols):
        rows, have = read_csv(path)
        miss = [c for c in cols if c not in have]
        if miss:
            notes.append(f"{os.path.basename(path)}: 열 이름이 다름(없는 열 {', '.join(miss)}) — 건너뜀")
            return None
        return rows

    # 1) 가지키팩터 → 노드 사전의 뼈대
    f = os.path.join(core, "가지키팩터.csv")
    if os.path.exists(f):
        rows = need(f, ["가지", "가지이름"])
        for r in rows or []:
            nid = "가지:" + r["가지"]
            tally("가지키팩터.csv", put(conn, "nodes", {"node_id": nid},
                                       {"name": r.get("가지이름"), "parent": None, "direction": r.get("방향"),
                                        "key_factor": r.get("공통키팩터"), "description": r.get("비고")},
                                       "doca_seed", "가지키팩터.csv", _mtime(f)))
            ch = r.get("자료통로", "")
            chan = "kcs" if "관세" in ch else "ecos" if ("ECOS" in ch.upper() or "한국은행" in ch) else "kosis" if "KOSIS" in ch.upper() else None
            if chan:
                put(conn, "node_signal_defs", {"node_id": nid, "signal_key": r.get("공통키팩터") or "기본"},
                    {"channel": chan, "code": None, "direction": r.get("방향"), "unit": None,
                     "description": f"{ch} · 주기 {r.get('주기', '')} · 기계수집 {r.get('기계수집', '')}"}, "doca_seed", "가지키팩터.csv", _mtime(f))
    # 2) 한장조사 json → 회사 노드·판정·종합 문장
    for f in sorted(glob.glob(os.path.join(core, "한장조사", "*.json"))):
        code = code6(os.path.splitext(os.path.basename(f))[0])
        if not re.fullmatch(r"[0-9A-Z]{6}", code):
            continue
        try:
            with open(f, encoding="utf-8-sig") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            notes.append(f"{os.path.basename(f)} 읽기 실패: {type(e).__name__}")
            continue
        asof = str(d.get("asof") or d.get("as_of") or _mtime(f))[:10]
        ref = "한장조사/" + os.path.basename(f)
        for i, row in enumerate(d.get("nodes") or [], 1):
            if not isinstance(row, (list, tuple)) or not row:
                continue
            row = list(row) + [""] * (6 - len(row))
            seq, seg = split_seq(row[0], i)
            nid = "노드:" + seg
            put(conn, "nodes", {"node_id": nid}, {"name": seg, "parent": None, "direction": None, "key_factor": None,
                                                   "description": None}, "doca_seed", ref, asof)
            put(conn, "company_nodes", {"stock_code": code, "seq": seq}, {"segment": seg, "node_id": nid, "weight": None,
                                                                          "weight_year": None}, "doca_seed", ref, asof)
            tally("한장조사(노드 줄)", put(conn, "node_judgments", {"stock_code": code, "seq": seq},
                                       {"node_name": seg, "year_from": year_from, "year_to": year_from + 3,
                                        "verdict": strip_tag(row[1]), "mech_verdict": None, "demand": strip_tag(row[2]),
                                        "share": strip_tag(row[3]), "numbers": strip_tag(row[4]), "changers": strip_tag(row[5]),
                                        "evidence": None}, "doca_seed", ref, asof))
        nv = d.get("nodes_verdict")
        if nv:
            nv = " ".join(map(str, nv)) if isinstance(nv, list) else str(nv)
            tally("한장조사(종합 문장)", put(conn, "company_node_verdict", {"stock_code": code},
                                         {"text": strip_tag(nv), "weighted": None}, "doca_seed", ref, asof))
        if d.get("name"):
            db.ensure_company(conn, code, d.get("name")) if not conn.execute(
                "SELECT 1 FROM companies WHERE stock_code=?", (code,)).fetchone() else None
    # 3) 피어그룹(국내)
    f = os.path.join(core, "피어그룹.csv")
    if os.path.exists(f):
        for r in need(f, ["대상", "사업부", "code", "name"]) or []:
            tally("피어그룹.csv", put(conn, "peer_groups",
                                     {"target": code6(r["대상"]), "segment": r["사업부"], "peer_code": code6(r["code"]) or None,
                                      "peer_name": r["name"]},
                                     {"segment_weight": _num(r.get("사업부_비중")), "peer_business": r.get("피어의_해당사업"),
                                      "reason": r.get("근거")}, "doca_seed", "피어그룹.csv", _mtime(f)))
    # 4) 해외 경쟁사(이름만)
    f = os.path.join(core, "피어그룹_해외.csv")
    if os.path.exists(f):
        for r in need(f, ["대상", "사업부", "name"]) or []:
            tally("피어그룹_해외.csv", put(conn, "peer_foreign", {"target": code6(r["대상"]), "segment": r["사업부"], "name": r["name"]},
                                         {"ticker": r.get("ticker"), "role": r.get("역할"), "reason": r.get("근거")},
                                         "doca_seed", "피어그룹_해외.csv", _mtime(f)))
    # 5) 사업부 실적(사업보고서 주석 영업부문)
    f = os.path.join(core, "피어_부문.csv")
    if os.path.exists(f):
        for r in need(f, ["code", "부문명", "fy"]) or []:
            fy = _num(r["fy"])
            if fy is None:
                continue
            tally("피어_부문.csv", put(conn, "segment_fin", {"stock_code": code6(r["code"]), "fy": int(fy), "period": "FY", "segment": r["부문명"]},
                                      {"revenue": _num(r.get("매출억")), "profit": _num(r.get("이익억")), "profit_kind": r.get("이익종류"),
                                       "status": "SEED"}, "doca_seed", "피어_부문.csv · " + (r.get("원천") or ""), _mtime(f)))
    # 6) 참고 표(근거 칸 참고 줄)
    for name in ("키팩터원장.csv", "병목지도.csv", "인과개별화.csv"):
        f = os.path.join(core, name)
        if not os.path.exists(f):
            continue
        rows, have = read_csv(f)
        ccol = next((c for c in ("종목코드", "code", "대상", "코드") if c in have), None)
        for i, r in enumerate(rows):
            code = code6(r.get(ccol)) if ccol else None
            key = json.dumps(r, ensure_ascii=False, sort_keys=True)[:400]
            tally(name, put(conn, "doca_refs", {"kind": name, "row_key": f"{code}|{key}"},
                            {"stock_code": code if code and re.fullmatch(r"[0-9A-Z]{6}", code) else None,
                             "row_json": json.dumps(r, ensure_ascii=False)}, "doca_seed", name, _mtime(f)))
    conn.commit()
    db.log_fetch(conn, "doca_import", core, None, True, sum(t["rows"] for t in rep.values()), json.dumps(rep, ensure_ascii=False)[:900])
    return rep, notes


def status(conn, code=None):
    root = get_doca_dir()
    r = conn.execute("SELECT * FROM fetch_log WHERE source='doca_import' ORDER BY id DESC LIMIT 1").fetchone()
    last = dict(r) if r else None
    if last and last.get("note"):
        try:
            last["report"] = json.loads(last["note"])
        except ValueError:
            pass
    have = {t: conn.execute(f"SELECT COUNT(DISTINCT {c}) FROM {t} WHERE is_current=1 AND source='doca_seed'").fetchone()[0]
            for t, c in (("node_judgments", "stock_code"), ("peer_groups", "target"))}
    return {"dir": root, "default": default_doca_dir(), "found": bool(core_dir(root)), "inspect": inspect(root, code) if root else None,
            "last": last, "companies": have,
            "counts": {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE is_current=1").fetchone()[0]
                       for t in ("node_judgments", "peer_groups", "peer_foreign", "segment_fin", "nodes")}}


# ---------------------------------------------------------------- 기계 판정(2단계에서 신호를 채움)
def mech_verdict(signals):
    """signals: [①사업부 매출, ②노드 수요, ③컨센 방향] 각각 −1·0·+1 또는 None. 반환: (판정, 꼬리표 목록)"""
    have = [s for s in signals if s is not None]
    if len(have) < 2:
        return "판정 못 함(자료 부족)", []
    t = sum(have)
    v = "순풍" if t >= 2 else "순풍(약)" if t == 1 else "확인 필요" if t == 0 else "약한 역풍" if t == -1 else "역풍"
    tags = []
    s1, s2 = signals[0], signals[1] if len(signals) > 1 else None
    if s1 == 1 and s2 == -1:
        tags.append("몫 확인")
    elif s1 == -1 and s2 == 1:
        tags.append("노드 순풍 · 몫 확인")
    return v, tags


def _verdict_side(v):
    v = v or ""
    if v.startswith("순풍"):
        return "good"
    if "역풍" in v:
        return "bad"
    return "mid"


# ---------------------------------------------------------------- 화면 R 자료
def nodes_payload(conn, code, P, today=None):
    today = today or dt.date.today()
    cy = P["current"]
    judg = {}
    for r in conn.execute("SELECT * FROM node_judgments WHERE stock_code=? AND is_current=1", (code,)):
        judg.setdefault(r["seq"], []).append(r)
    links = {}
    for r in conn.execute("SELECT * FROM company_nodes WHERE stock_code=? AND is_current=1", (code,)):
        links.setdefault(r["seq"], []).append(r)
    # 사업부 비중: WiseReport(자체 수집) → 피어그룹 사업부 비중(작업 폴더)
    mix = {r["name"]: r["pct"] for r in conn.execute(
        "SELECT name, pct FROM segments WHERE stock_code=? AND kind='mix' AND as_of=(SELECT MAX(as_of) FROM segments WHERE stock_code=? AND kind='mix')",
        (code, code))}
    mix_asof = (conn.execute("SELECT MAX(as_of) d FROM segments WHERE stock_code=? AND kind='mix'", (code,)).fetchone() or {"d": None})["d"]
    pw = {}
    for r in conn.execute("SELECT segment, segment_weight, as_of FROM peer_groups WHERE target=? AND is_current=1", (code,)):
        if r["segment_weight"] is not None:
            pw[r["segment"]] = (r["segment_weight"], r["as_of"])

    def match(seg):
        """(항목 이름, 비중, 설명, 출처) — 자체 수집(WiseReport) 같은 이름 > 부분 일치 > 작업 폴더 같은 이름 > 부분 일치"""
        if not seg:
            return None
        for nm, pct in mix.items():
            if nm == seg:
                return nm, pct, f"WiseReport 주요제품({mix_asof}) '{nm}'", "self"
        for nm, pct in mix.items():
            if nm and (nm in seg or seg in nm or _overlap(nm, seg)):
                return nm, pct, f"WiseReport 주요제품({mix_asof}) '{nm}'", "self"
        for nm, (w, a) in pw.items():
            if nm == seg:
                return nm, (w * 100 if w is not None and w <= 1 else w), f"작업 폴더 피어그룹 사업부 비중({a})", "doca_seed"
        for nm, (w, a) in pw.items():
            if nm and (nm in seg or seg in nm or _overlap(nm, seg)):
                return nm, (w * 100 if w is not None and w <= 1 else w), f"작업 폴더 피어그룹 사업부 비중({a}) '{nm}'", "doca_seed"
        return None

    rows = []
    for seq in sorted(set(judg) | set(links)):
        j = pick(judg.get(seq, []))
        lk = pick(links.get(seq, []))
        seg = (lk or {}).get("segment") or (j or {}).get("node_name")
        mt = match(seg)
        w, wnote, wsrc = (mt[1], mt[2], mt[3]) if mt else (None, "비중 미확인", None)
        alts = sorted([dict(x) for x in judg.get(seq, [])], key=lambda x: str(x.get("as_of") or ""), reverse=True)
        newer = bool(j and alts and alts[0]["id"] != j["id"] and str(alts[0].get("as_of") or "") > str(j.get("as_of") or ""))
        rows.append({"seq": seq, "segment": seg, "node_id": (lk or {}).get("node_id"), "weight": w, "weight_note": wnote, "weight_src": wsrc,
                     "verdict": (j or {}).get("verdict"), "mech": (j or {}).get("mech_verdict"), "side": _verdict_side((j or {}).get("verdict")),
                     "demand": (j or {}).get("demand"), "share": (j or {}).get("share"), "numbers": (j or {}).get("numbers"),
                     "changers": (j or {}).get("changers"), "source": (j or {}).get("source"),
                     "source_name": SOURCE_NAME.get((j or {}).get("source"), "–"), "as_of": (j or {}).get("as_of"),
                     "source_ref": (j or {}).get("source_ref"), "stale": bool(j and stale(j, today=today)), "newer": newer,
                     "signals": [None, None, None]})
    # 한 항목(예: '인쇄용지, 특수지')에 노드 여러 개가 걸리면 비중을 나눠 두 번 세지 않게(추정 표시)
    by_item = {}
    for r in rows:
        mt = match(r["segment"])
        if mt and r["weight"] is not None:
            by_item.setdefault((mt[3], mt[0]), []).append(r)
    for (_, nm), rs in by_item.items():
        if len(rs) > 1:
            for r in rs:
                r["weight"] = r["weight"] / len(rs)
                r["weight_note"] += f" — 노드 {len(rs)}개가 한 항목이라 {len(rs)}등분(추정)"
                r["weight_est"] = True
    cv = pick(conn.execute("SELECT * FROM company_node_verdict WHERE stock_code=? AND is_current=1", (code,)).fetchall())
    tot = sum(r["weight"] for r in rows if r["weight"] is not None)
    share = {k: sum(r["weight"] for r in rows if r["weight"] is not None and r["side"] == k) for k in ("good", "mid", "bad")}
    refs = [json.loads(r["row_json"]) | {"_kind": r["kind"]} for r in conn.execute(
        "SELECT kind, row_json FROM doca_refs WHERE stock_code=? AND is_current=1 ORDER BY kind", (code,))]
    return {"rows": rows, "period": f"{cy}~{cy + 3}", "verdict": dict(cv, source_name=SOURCE_NAME.get(cv["source"]),
                                                                       stale=stale(cv, today=today)) if cv else None,
            "weights": {"total": tot, **share}, "refs": refs, "disclaimer": DISCLAIMER}


def _overlap(a, b):
    ta = {t for t in re.split(r"[\s,·/()]+", a) if len(t) >= 2}
    tb = {t for t in re.split(r"[\s,·/()]+", b) if len(t) >= 2}
    return bool(ta & tb)


# ---------------------------------------------------------------- 화면 S 자료
def front_growth(q3_cum, cons_cy, cons_cy2):
    """앞 성장 = 올해 기준점 → 내후년 컨센 매출 연평균. 3분기 누적이 있으면 기준점 = 누적 × 4/3. 반환: (성장률, 기준점, 기준 설명)"""
    if q3_cum is not None and q3_cum > 0:
        base, how = q3_cum * 4 / 3, "3Q 누적×4/3"
    elif cons_cy is not None and cons_cy > 0:
        base, how = cons_cy, "올해 컨센"
    else:
        return None, None, "올해 기준점 없음"
    if cons_cy2 is None or cons_cy2 <= 0:
        return None, base, how + " · 내후년 컨센 없음"
    return (cons_cy2 / base) ** 0.5 - 1, base, how


def company_row(conn, code, P):
    cy = P["current"]
    from .goodco import Data
    D = Data(conn, code, P)
    if not D.f:
        return None
    rev = lambda y: D.v(y, "revenue")
    q3 = D.eok(cy, "acc_revenue", "Q3")
    fg, base, how = front_growth(q3, D.c(cy, "revenue"), D.c(cy + 2, "revenue"))
    r5a, r5b = rev(cy - 5), rev(cy - 1)
    past = (r5b / r5a) ** (1 / 4) - 1 if r5a and r5b and r5a > 0 and r5b > 0 else None
    opc = D.c(cy + 2, "op_income")
    rv2 = D.c(cy + 2, "revenue")
    opm2 = D.c(cy + 2, "op_margin") if D.c(cy + 2, "op_margin") is not None else (opc / rv2 * 100 if opc is not None and rv2 else None)
    price = D.v(cy, "price_close", "ASOF")
    eps_c, bps_c = D.c(cy, "eps"), D.c(cy, "bps")
    years = list(range(cy - 5, cy + 3))
    hist = []
    for y in years:
        if y < cy and rev(y) is not None:
            hist.append({"y": y, "rev": rev(y), "opm": D.v(y, "op_margin"), "est": False})
        else:
            rv, op = D.c(y, "revenue"), D.c(y, "op_income")
            hist.append({"y": y, "rev": rv, "opm": (op / rv * 100) if op is not None and rv else D.c(y, "op_margin"),
                         "opm_adj": (op * 0.83 / rv * 100) if op is not None and rv else None, "est": True})
    return {"code": code, "front": fg, "front_how": how, "past5": past, "opm_last": D.v(cy - 1, "op_margin"),
            "opm_2e": opm2, "opm_2e_adj": (opm2 * 0.83) if opm2 is not None else None, "roe": D.v(cy - 1, "roe"),
            "debt": D.v(cy - 1, "debt_ratio"), "per": (price / eps_c) if price and eps_c and eps_c > 0 else None,
            "pbr": (price / bps_c) if price and bps_c and bps_c > 0 else None, "hist": hist, "n_est": D.n_est(cy + 2)}


def peers_payload(conn, code, P):
    groups = {}
    for r in conn.execute("SELECT * FROM peer_groups WHERE target=? AND is_current=1 ORDER BY id", (code,)):
        k = (r["segment"], r["peer_code"], r["peer_name"])
        groups.setdefault(r["segment"], {}).setdefault(k, []).append(r)
    name_of = {r["stock_code"]: r["name"] for r in conn.execute("SELECT stock_code, name FROM companies")}
    cache = {}

    def crow(c):
        if c not in cache:
            try:
                cache[c] = company_row(conn, c, P) if c else None
            except Exception:      # noqa: BLE001
                cache[c] = None
        return cache[c]

    segs = []
    for seg, items in groups.items():
        lines, weight, src_asof = [], None, None
        for (s, pc, pn), rs in items.items():
            r = pick(rs)
            weight = weight if weight is not None else r["segment_weight"]
            src_asof = r["as_of"]
            m = crow(pc)
            lines.append({"code": pc, "name": pn or name_of.get(pc), "self": pc == code, "business": r["peer_business"],
                          "reason": r["reason"], "source_name": SOURCE_NAME.get(r["source"]), "as_of": r["as_of"],
                          "collected": m is not None, "m": m})
        if not any(x["self"] for x in lines):
            lines.insert(0, {"code": code, "name": name_of.get(code), "self": True, "business": seg, "reason": "본인",
                             "source_name": "–", "as_of": None, "collected": crow(code) is not None, "m": crow(code)})
        for key in ("front", "opm_last"):              # 사업부 안 순위(값 있는 회사끼리)
            vals = sorted([x for x in lines if x["m"] and x["m"].get(key) is not None], key=lambda x: -x["m"][key])
            for i, x in enumerate(vals, 1):
                x.setdefault("rank", {})[key] = (i, len(vals))
        me = next((x for x in lines if x["self"]), None)
        rk = (me or {}).get("rank", {})
        concl = f"{seg}: " + " · ".join(f"{nm} {rk[k][0]}위/{rk[k][1]}" for k, nm in (("front", "앞 성장"), ("opm_last", "이익률")) if k in rk) \
            if rk else f"{seg}: 비교할 수집 자료 없음(피어 미수집)"
        foreign = [dict(r) for r in conn.execute("SELECT name, ticker, role, reason, as_of FROM peer_foreign WHERE target=? AND segment=? "
                                                 "AND is_current=1 ORDER BY id", (code, seg))]
        segs.append({"segment": seg, "weight": weight, "lines": lines, "conclusion": concl, "foreign": foreign, "as_of": src_asof,
                     "missing": sum(1 for x in lines if not x["collected"])})
    segfin = [dict(r) for r in conn.execute("SELECT stock_code, fy, period, segment, revenue, profit, profit_kind, status, source, as_of "
                                            "FROM segment_fin WHERE stock_code=? AND is_current=1 ORDER BY fy, segment", (code,))]
    return {"segments": segs, "segment_fin": segfin, "years": list(range(P["current"] - 5, P["current"] + 3)),
            "cy": P["current"], "disclaimer": DISCLAIMER}


def payload(conn, code, P):
    return {"status": status(conn, code), "nodes": nodes_payload(conn, code, P), "peers": peers_payload(conn, code, P)}
