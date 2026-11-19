"""화면 T「노드 4년 판정」 — 설계도 '기업 한 장 질문 1 · 무엇으로 버나 — 노드별 4년(2026~2029) 우호 판정'(2026-11-19)을 앱에 옮김

판정은 조사 결과(해석)이며 계산값이 아님 · 매수 조건 아님 · 리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님

두 가지 판정을 나란히 둔다(같은 노드·같은 해끼리 비교 → 일치율)
 · claude : Claude가 조사해 쓴 판정(작업 폴더 한장조사 82곳 초기값, 이후 'Claude 조사 요청 파일' → 결과 넣기)
 · app    : 앱이 스스로 모은 근거로 내는 규칙 판정(설계도 4~8절을 알고리즘으로)

앱 알고리즘(설계도 → 코드)
 ① 노드 뽑기: Claude 노드가 있으면 그 목록을 그대로 평가(비교용), 없으면 DART 사업보고서 주요 제품 표 > WiseReport 구성비를
    지식 사전 노드로 묶음 + 씨앗(기업개요·공시·뉴스에는 있으나 매출 구성에 없는 사업) + 걸림(사람·지배구조·자금 공시) → 3~6개
 ② 근거: DART 재무(연·분기 누적), 컨센서스(3개월 수정, ×0.83), DART 공시 제목(6개월, 종류→단계·방향), 뉴스 제목(말),
    지식 사전 구조 흐름(계획). 가격·목표가는 판정에 넘기지 않는다.
 ③ 판정: 2026 = 실적(숫자) · 2027·2028 = 컨센서스(보정)+확정 공시+구조 · 2029 = 구조 흐름만(단계 상한 '계획', 컨센 밖)
    판정 말 다섯: 우호·중립·역풍·씨앗·걸림. 대표 판정 = 네 칸 중 가장 많은 것(같으면 가까운 해)
    땅(몫): 좋은 땅·가시떨기·돌밭·길가 — 매출·이익률 추세, 반복 근거(공급계약·장기 공시), 구조 수요 대비 회사 성장으로
 ④ 기계 검사 10개(설계도 8절) — 통과 못 해도 저장하되 경고
 ⑤ 저장 hj_* 표 · ⑥ 화면 T · ⑦ 다시 조사 필요(조사 90일 넘음·조사 뒤 분기 보고서·'판정을 바꿀 것' 낱말 적중)
"""
import datetime as dt
import html
import io
import json
import re
import zipfile

from . import db

YEARS = [2026, 2027, 2028, 2029]
JUDGMENTS = ["우호", "중립", "역풍", "씨앗", "걸림"]
STAGES = ["말", "계획", "확정", "숫자"]
LANDS = ["좋은 땅", "가시떨기", "돌밭", "길가"]
KINDS = ["사업부", "씨앗", "원료", "고객", "사람"]
STALE_DAYS = 90
DISCLAIMER = "판정은 조사 결과(해석)이며 계산값이 아님 · 매수 조건 아님 · 리서치 참고자료이며 투자자문이 아님"
CUT = 0.83

# 원본 판정 말 → 다섯 말(설계도 6-1). 앞 낱말 우선, 없으면 꼬리표 색
WORD_MAP = [
    ("걸림", "걸림"), ("주의", "걸림"), ("숨은", "걸림"), ("내부거래", "걸림"),
    ("씨앗", "씨앗"), ("확인 필요", "씨앗"), ("확인", "씨앗"), ("미정", "씨앗"), ("기대", "씨앗"), ("다음", "씨앗"),
    ("역풍", "역풍"), ("둔화", "역풍"), ("부담", "역풍"), ("적자", "역풍"), ("나쁨", "역풍"), ("추격", "역풍"), ("경쟁", "역풍"),
    ("우호", "우호"), ("순풍", "우호"), ("회복", "우호"), ("성장", "우호"), ("개선", "우호"), ("초과 수요", "우호"), ("핵심", "우호"),
    ("중립", "중립"), ("보통", "중립"), ("성숙", "중립"), ("엇갈림", "중립"), ("양면", "중립"), ("관찰", "중립"), ("지켜볼", "중립"),
    ("전환", "중립"), ("진행", "걸림"),
]
CLASS_MAP = {"good": "우호", "warn": "걸림", "bad": "역풍", "mid": "중립"}


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def map_judgment(raw):
    """원본 꼬리표(HTML) → (다섯 말 중 하나, 원래 글자)"""
    text = strip_html(raw)
    head = text[:12]
    for w, j in WORD_MAP:
        if head.startswith(w) or (w in head and len(w) >= 2 and head.find(w) <= 3):
            return j, text
    cls = re.findall(r"t-(good|warn|bad|mid)", str(raw or ""))
    return (CLASS_MAP.get(cls[0], "중립") if cls else "중립"), text


# ---------------------------------------------------------------- 저장(설계도 7-1)
def ensure(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS hj_company (
      code TEXT NOT NULL, origin TEXT NOT NULL, name TEXT, asof TEXT, version INTEGER DEFAULT 1, status TEXT, memo TEXT, updated_at TEXT,
      PRIMARY KEY(code, origin));
    CREATE TABLE IF NOT EXISTS hj_node (
      code TEXT NOT NULL, origin TEXT NOT NULL, node_id INTEGER NOT NULL, name TEXT, share_pct REAL, kind TEXT, demand TEXT, share TEXT,
      numbers TEXT, triggers TEXT, land TEXT, rep TEXT, raw_judgment TEXT, kn_node TEXT, PRIMARY KEY(code, origin, node_id));
    CREATE TABLE IF NOT EXISTS hj_node_year (
      code TEXT NOT NULL, origin TEXT NOT NULL, node_id INTEGER NOT NULL, year INTEGER NOT NULL, judgment TEXT, stage TEXT, basis TEXT,
      n_sources INTEGER, PRIMARY KEY(code, origin, node_id, year));
    CREATE TABLE IF NOT EXISTS hj_evidence (
      id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, origin TEXT NOT NULL, node_id INTEGER, year INTEGER, date TEXT, type TEXT,
      stage TEXT, dir TEXT, title TEXT, ref TEXT, read INTEGER);
    CREATE INDEX IF NOT EXISTS ix_hj_ev ON hj_evidence(code, origin);
    CREATE TABLE IF NOT EXISTS hj_verdict (code TEXT NOT NULL, origin TEXT NOT NULL, asof TEXT, text TEXT, PRIMARY KEY(code, origin));
    CREATE TABLE IF NOT EXISTS hj_run_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, origin TEXT, at TEXT, step TEXT, result TEXT, warnings TEXT, model TEXT);
    """)


def save(conn, doc, origin, step, model=None):
    """설계도 7-2 모양의 doc을 저장(같은 회사·같은 출처는 새것으로 바꾸고 기록은 run_log에). 반환: 검사 경고 목록"""
    ensure(conn)
    code = doc["code"]
    warns = check(conn, doc, origin)
    prev = conn.execute("SELECT version FROM hj_company WHERE code=? AND origin=?", (code, origin)).fetchone()
    ver = (prev["version"] + 1) if prev else 1
    for t in ("hj_node", "hj_node_year", "hj_evidence", "hj_verdict"):
        conn.execute(f"DELETE FROM {t} WHERE code=? AND origin=?", (code, origin))
    status = "검사 경고" if warns else "정상"
    conn.execute("INSERT INTO hj_company(code, origin, name, asof, version, status, memo, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(code, origin) DO UPDATE SET name=excluded.name, asof=excluded.asof, version=excluded.version, "
                 "status=excluded.status, memo=excluded.memo, updated_at=excluded.updated_at",
                 (code, origin, doc.get("name"), doc.get("asof"), ver, status, doc.get("memo"), db.now()))
    for i, n in enumerate(doc.get("nodes") or [], 1):
        nid = int(n.get("node_id") or i)
        years = n.get("years") or {}
        conn.execute("INSERT INTO hj_node VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, origin, nid, n.get("name"), n.get("share_pct"), n.get("kind"), n.get("demand"), n.get("share"),
                      n.get("numbers"), n.get("triggers"), n.get("land"), rep_judgment(years), n.get("raw_judgment"), n.get("kn_node")))
        for y in YEARS:
            c = years.get(str(y)) or years.get(y) or {}
            conn.execute("INSERT INTO hj_node_year VALUES (?,?,?,?,?,?,?,?)",
                         (code, origin, nid, y, c.get("judgment"), c.get("stage"), c.get("basis"), c.get("n_sources")))
        for e in n.get("evidence") or []:
            conn.execute("INSERT INTO hj_evidence(code, origin, node_id, year, date, type, stage, dir, title, ref, read) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (code, origin, nid, e.get("year"), e.get("date"), e.get("type"), e.get("stage"), e.get("dir"), e.get("title"),
                          e.get("ref"), 1 if e.get("read") else 0))
    conn.execute("INSERT INTO hj_verdict VALUES (?,?,?,?)", (code, origin, doc.get("asof"), doc.get("verdict")))
    conn.execute("INSERT INTO hj_run_log(code, origin, at, step, result, warnings, model) VALUES (?,?,?,?,?,?,?)",
                 (code, origin, db.now(), step, status, json.dumps(warns, ensure_ascii=False), model))
    conn.commit()
    return warns


def rep_judgment(years):
    """대표 판정 = 네 칸 중 가장 많은 판정, 같으면 가까운 해(설계도 6-2)"""
    seq = [((years.get(str(y)) or years.get(y) or {}).get("judgment")) for y in YEARS]
    seq = [j for j in seq if j]
    if not seq:
        return None
    best = max(set(seq), key=lambda j: (seq.count(j), -seq.index(j)))
    return best


# ---------------------------------------------------------------- 기계 검사(설계도 8절)
PRICE_WORDS = re.compile(r"목표가|목표 ?주가|주가|시가총액|PER\s*\d|원\s*\(목표|매수|매도")


def check(conn, doc, origin):
    w = []
    nodes = doc.get("nodes") or []
    if not 3 <= len(nodes) <= 6:
        w.append(f"1. 노드 수 {len(nodes)}개(3~6개 권장)")
    today = dt.date.today()
    for i, n in enumerate(nodes, 1):
        tag = f"노드 {i}"
        for k, lab in (("name", "노드"), ("demand", "수요"), ("share", "몫"), ("numbers", "숫자"), ("triggers", "바꿀 것")):
            if not str(n.get(k) or "").strip():
                w.append(f"2. {tag} '{lab}' 빈칸(모르면 '모름' + 이유)")
        ys = n.get("years") or {}
        miss = [y for y in YEARS if not (ys.get(str(y)) or ys.get(y) or {}).get("judgment")]
        if miss:
            w.append(f"3. {tag} 연도 칸 빈칸: {', '.join(map(str, miss))}")
        for y in YEARS:
            c = ys.get(str(y)) or ys.get(y) or {}
            if c.get("judgment") and c["judgment"] not in JUDGMENTS:
                w.append(f"4. {tag} {y} 판정 말 '{c['judgment']}'(다섯 중 하나 아님)")
            if c.get("judgment") == "우호" and (c.get("n_sources") or 0) < 2 and "(원천 1)" not in str(c.get("basis") or ""):
                c["basis"] = (str(c.get("basis") or "") + " (원천 1)").strip()
                w.append(f"6. {tag} {y} 우호인데 확정 이상 원천 {c.get('n_sources') or 0}개 → '(원천 1)'")
        c29 = ys.get("2029") or ys.get(2029) or {}
        if c29.get("stage") in ("확정", "숫자"):
            w.append(f"7. {tag} 2029 단계 '{c29['stage']}'(컨센 밖이라 '계획' 이하)")
            c29["stage"] = "계획"
        for e in n.get("evidence") or []:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e.get("date") or "")) or not e.get("type"):
                w.append(f"5. {tag} 근거에 날짜(YYYY-MM-DD)·원천 종류 없음: {str(e.get('title'))[:30]}")
                break
        basis = " ".join(str((ys.get(str(y)) or ys.get(y) or {}).get("basis") or "") for y in YEARS)
        if PRICE_WORDS.search(basis):
            w.append(f"9. {tag} 판정 근거에 가격·목표가 낱말")
        for m in re.finditer(r"(20\d\d)-(\d\d)-(\d\d)", str(n.get("demand") or "") + str(n.get("share") or "")):
            try:
                d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if (today - d).days > 365 and not re.search(r"지난|과거|당시|끝난", str(n.get("demand")) + str(n.get("share"))):
                    w.append(f"10. {tag} 12개월 넘은 사건({m.group(0)})을 진행형으로 썼는지 확인")
                    break
            except ValueError:
                pass
    w += _check_numbers(conn, doc)
    return w


def _check_numbers(conn, doc):
    """8. 숫자 칸의 'YYYY 매출 N억' 꼴을 앱 재무와 대조(±1%). 맞출 값이 없으면 넘어감"""
    out = []
    code = doc["code"]
    for i, n in enumerate(doc.get("nodes") or [], 1):
        if (n.get("share_pct") or 0) < 70 and len(doc.get("nodes") or []) > 1:
            continue                                # 회사 전체 숫자와 대조할 수 있는 노드만(주력이 매출 대부분일 때)
        for m in re.finditer(r"(20\d\d)\s*(?:년)?\s*매출\s*([\d,]+)\s*억", str(n.get("numbers") or "")):
            y, v = int(m.group(1)), float(m.group(2).replace(",", ""))
            r = conn.execute("SELECT value FROM facts WHERE stock_code=? AND fiscal_year=? AND metric='revenue' AND period='FY' AND is_current=1",
                             (code, y)).fetchone()
            if r and r["value"] and abs(v / r["value"] - 1) > 0.01:
                out.append(f"8. 노드 {i} 숫자 '{m.group(0)}'가 앱 재무 {r['value']:,.0f}억과 1% 넘게 다름")
    return out


# ---------------------------------------------------------------- 초기값·Claude 결과 넣기
def guess_kind(name, j=None):
    """노드 이름으로 종류 추정(사람 = 사람·지배구조·소송 같은 위험 층)"""
    if re.search(r"사람|지배구조|경영진|최대주주|오너|소송|분쟁|횡령|배임|규제\s*리스크", name):
        return "사람"
    if j == "씨앗":
        return "씨앗"
    if re.search(r"원가|환율|원료|원재료|원자재|소재\s*값|가격\)|값\(|펄프|유가", name):
        return "원료"
    if re.search(r"고객|바이어|모회사|납품처|발주처|거래처", name):
        return "고객"
    return "사업부"


def from_hanjang_json(code, d):
    """작업 폴더 한장조사/{코드}.json(옛 모양) → 7-2 모양. 연도 칸은 '옛 판정(연도 없음)'으로 네 칸에 같은 판정"""
    nodes = []
    for i, row in enumerate(d.get("nodes") or [], 1):
        if not isinstance(row, (list, tuple)) or not row:
            continue
        row = list(row) + [""] * (6 - len(row))
        j, raw = map_judgment(row[1])
        name = strip_html(row[0])
        kind = guess_kind(name, j)
        m = re.search(r"(\d{1,3}(?:\.\d)?)\s*%", name)
        nodes.append({"node_id": i, "name": name, "share_pct": float(m.group(1)) if m else None, "kind": kind,
                      "demand": strip_html(row[2]), "share": strip_html(row[3]), "numbers": strip_html(row[4]), "triggers": strip_html(row[5]),
                      "land": None, "raw_judgment": raw,
                      "years": {str(y): {"judgment": j, "stage": "계획" if y == 2029 else None, "basis": "옛 판정에서 옮김(연도 없음)",
                                         "n_sources": 2 if j == "우호" else None} for y in YEARS},
                      "evidence": []})
    return {"code": code, "name": d.get("name"), "asof": str(d.get("asof") or "")[:10] or None, "nodes": nodes,
            "verdict": strip_html(d.get("nodes_verdict")), "memo": strip_html(d.get("sources"))[:4000]}


def import_zip(conn, data):
    """작업 폴더 한장조사 zip(또는 그 안의 json 여러 개) → origin='claude'로 저장. 반환: {회사 수, 경고 수, 건너뜀}"""
    z = zipfile.ZipFile(io.BytesIO(data))
    got, skipped, nwarn = [], [], 0
    for name in z.namelist():
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        m = re.fullmatch(r"([0-9A-Z]{6})\.json", base)
        if not m or ".." in name:
            continue
        try:
            d = json.loads(z.read(name).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            skipped.append(base)
            continue
        doc = from_hanjang_json(m.group(1), d)
        if not doc["nodes"]:
            skipped.append(base)
            continue
        nwarn += len(save(conn, doc, "claude", "초기값 옮기기(작업 폴더 zip)", "Claude(작업 폴더)"))
        got.append(m.group(1))
    return {"companies": len(got), "codes": got, "skipped": skipped, "warnings": nwarn}


def validate_claude(obj):
    """Claude가 돌려준 JSON(7-2 모양 하나 또는 목록) 검사·정리. 반환: ([doc], 메모)"""
    docs = obj if isinstance(obj, list) else (obj.get("companies") if isinstance(obj, dict) and isinstance(obj.get("companies"), list) else [obj])
    out, notes = [], []
    for d in docs:
        if not isinstance(d, dict) or not re.fullmatch(r"[0-9A-Z]{6}", str(d.get("code") or "")):
            notes.append(f"종목코드 없음·형식 오류로 뺌: {str(d)[:60]}")
            continue
        if not isinstance(d.get("nodes"), list) or not d["nodes"]:
            notes.append(f"{d['code']}: nodes 없음 — 뺌")
            continue
        nodes = []
        for i, n in enumerate(d["nodes"], 1):
            if not isinstance(n, dict) or not n.get("name"):
                notes.append(f"{d['code']} 노드 {i}: 이름 없음 — 뺌")
                continue
            ys = {}
            for y in YEARS:
                c = (n.get("years") or {}).get(str(y)) or {}
                j = c.get("judgment") if c.get("judgment") in JUDGMENTS else None
                if c.get("judgment") and not j:
                    j = map_judgment(c["judgment"])[0]
                    notes.append(f"{d['code']} 노드 {i} {y}: 판정 말 '{c.get('judgment')}' → '{j}'로 바꿈")
                ys[str(y)] = {"judgment": j, "stage": c.get("stage") if c.get("stage") in STAGES else None,
                              "basis": str(c.get("basis") or "")[:300]}
            ev = [e for e in (n.get("evidence") or []) if isinstance(e, dict)][:30]
            for y in YEARS:                          # 확정 이상 원천 수(서로 독립 — 제목이 같으면 1개)
                ys[str(y)]["n_sources"] = len({(e.get("title") or "")[:40] for e in ev if e.get("stage") in ("확정", "숫자")
                                                and (not e.get("year") or str(e.get("year")) == str(y))})
            nodes.append({"node_id": i, "name": str(n["name"])[:120], "share_pct": n.get("share_pct"),
                          "kind": n.get("kind") if n.get("kind") in KINDS else "사업부",
                          **{k: str(n.get(k) or "")[:600] for k in ("demand", "share", "numbers", "triggers")},
                          "land": n.get("land") if n.get("land") in LANDS else None, "years": ys,
                          "evidence": [{k: e.get(k) for k in ("year", "date", "type", "stage", "dir", "title", "ref", "read")} for e in ev]})
        out.append({"code": d["code"], "name": d.get("name"), "asof": str(d.get("asof") or db.today())[:10], "nodes": nodes,
                    "verdict": str(d.get("verdict") or "")[:1500], "memo": str(d.get("memo") or "")[:2000]})
    return out, notes


# ---------------------------------------------------------------- ② 근거 모으기
DISC_RULES = [   # (공시 제목 낱말, 종류, 단계, 방향, 걸림?)
    (r"횡령|배임", "공시", "확정", "-", True), (r"최대주주\s*변경|경영권", "공시", "확정", "-", True),
    (r"감사의견|감사범위\s*제한|의견거절|한정", "공시", "확정", "-", True), (r"관리종목|상장적격성|거래정지|불성실공시", "공시", "확정", "-", True),
    (r"소송|가처분|소장", "공시", "확정", "-", True), (r"회생|파산|부도", "공시", "확정", "-", True),
    (r"유상증자|전환사채|신주인수권|교환사채", "공시", "확정", "-", False),
    (r"공급계약|판매.?공급계약|수주", "공시", "확정", "+", False), (r"공급계약\s*해지|계약\s*해지", "공시", "확정", "-", False),
    (r"신규시설투자|시설투자|유형자산\s*취득|공장", "공시", "확정", "+", False), (r"타법인\s*주식.*취득|영업양수|인수", "공시", "확정", "+", False),
    (r"자기주식\s*취득|소각|현금.?배당", "공시", "확정", "+", False), (r"영업정지|생산중단", "공시", "확정", "-", False),
    (r"잠정|매출액또는손익구조", "공시", "숫자", None, False),
]
NEWS_POS = re.compile(r"증가|확대|수주|호조|상승|흑자|성장|최대|돌파|증설|신규|계약|수혜|회복|개선")
NEWS_NEG = re.compile(r"감소|축소|부진|적자|하락|소송|리콜|중단|철수|감산|둔화|우려|악화|손실|제재|과징금")


def classify_disclosure(title):
    for pat, typ, stage, d, risk in DISC_RULES:
        if re.search(pat, title or ""):
            return {"type": typ, "stage": stage, "dir": d, "risk": risk}
    return None


def evidence_pool(conn, code, today=None):
    """회사 단위 근거: 공시(6개월)·뉴스 제목(6개월). 반환: (공시 목록, 뉴스 목록)"""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=183))
    disc = []
    for r in conn.execute("SELECT rcept_dt, title, link FROM events WHERE stock_code=? AND rcept_dt>=? ORDER BY rcept_dt DESC",
                          (code, since.strftime("%Y%m%d"))):
        c = classify_disclosure(r["title"])
        if c:
            d = r["rcept_dt"]
            disc.append(dict(c, date=f"{d[:4]}-{d[4:6]}-{d[6:8]}", title=r["title"], ref=r["link"] or "DART", read=False))
    news = []
    for r in conn.execute("SELECT published, title, link, query FROM news WHERE stock_code=? AND published>=? ORDER BY published DESC LIMIT 500",
                          (code, since.isoformat())):
        t = r["title"] or ""
        d = "+" if NEWS_POS.search(t) and not NEWS_NEG.search(t) else "-" if NEWS_NEG.search(t) and not NEWS_POS.search(t) else None
        news.append({"date": r["published"], "title": t, "ref": r["link"], "type": "언론", "stage": "말", "dir": d, "read": False, "query": r["query"]})
    return disc, news


# ---------------------------------------------------------------- ①·③ 앱 판정
def _sgn(v, th):
    return None if v is None else (1 if v >= th else -1 if v <= -th else 0)


def company_numbers(conn, code, P):
    """회사 숫자 근거(가격 제외): 최근 분기 누적 성장, 연간 이익률 추세, 컨센(보정) 성장, 3개월 수정"""
    from .goodco import Data
    from .report import consensus_trend
    D = Data(conn, code, P)
    cy = P["current"]
    q = next((p for p in ("Q3", "H1", "Q1") if D.v(cy, "acc_revenue", p) is not None and D.v(cy - 1, "acc_revenue", p)), None)
    qg = qop = None
    if q:
        a, b = D.v(cy, "acc_revenue", q), D.v(cy - 1, "acc_revenue", q)
        qg = a / b - 1 if a and b else None
        oa, ob = D.v(cy, "acc_op", q), D.v(cy - 1, "acc_op", q)
        qop = (oa / a * 100, ob / b * 100) if oa is not None and ob is not None and a and b else None
    rev = {y: D.v(y, "revenue") for y in range(cy - 4, cy)}
    opm = {y: D.v(y, "op_margin") for y in range(cy - 4, cy)}
    cons = {y: {"rev": D.c(y, "revenue"), "op": D.c(y, "op_income")} for y in (cy, cy + 1, cy + 2)}
    rv3 = {}
    try:
        for it in consensus_trend(conn, code, P).get("summary") or []:
            if it["metric"] in ("op_income", "revenue"):
                rv3[(it["year"], it["metric"])] = (it.get("ch") or {}).get(3)
    except Exception:      # noqa: BLE001
        pass
    return {"q": q, "q_growth": qg, "q_opm": qop, "q_rev": D.eok(cy, "acc_revenue", q) if q else None, "rev": rev, "opm": opm,
            "cons": cons, "rev3": rv3, "cy": cy}


def _cons_growth(N, y):
    """y년 컨센 매출 성장(전년 실적 또는 전년 컨센 대비)과 보정 영업이익 성장"""
    c, p = N["cons"].get(y) or {}, N["cons"].get(y - 1) or {}
    prev_rev = p.get("rev") if y - 1 >= N["cy"] else N["rev"].get(y - 1)
    g = c["rev"] / prev_rev - 1 if c.get("rev") and prev_rev else None
    prev_op = (p.get("op") * CUT) if (p.get("op") is not None and y - 1 >= N["cy"]) else None
    og = (c["op"] * CUT) / prev_op - 1 if c.get("op") and prev_op and prev_op > 0 else None
    return g, og


def judge_node(node, kn, K, N, disc, news, share_pct, main):
    """노드 하나의 4년 칸·땅·6칸 글. node: {'name','kind'}, kn: 지식 사전 노드(없을 수 있음)"""
    from .nodeview import structural
    s1, parts = structural(kn, K) if kn else (0.0, [])
    kws = [k for k in (kn.get("keywords") if kn else []) or [] if len(k) >= 2] + \
          [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", node["name"]) if w not in ("사업", "부문", "제품", "기타", "사람", "지배구조")]
    hit = lambda t: any(k.lower() in (t or "").lower() for k in kws)
    nd = [e for e in disc if not e["risk"] and (hit(e["title"]) or main)]          # 주력 노드는 회사 공시를 받음
    nq = node_query(node.get("company") or "", node["name"])
    nn = [e for e in news if hit(e["title"]) or (e.get("query") and e["query"] == nq)]
    learned = learned_view(node, nd, nn)
    pos_conf = [e for e in nd if e["dir"] == "+" and e["stage"] in ("확정", "숫자")]
    neg_conf = [e for e in nd if e["dir"] == "-"]
    years, ev = {}, []
    kind = node.get("kind") or "사업부"
    cy = N["cy"]
    if kind == "사람":
        risk = [e for e in disc if e["risk"]]
        for y in YEARS:
            years[str(y)] = {"judgment": "걸림" if risk else "중립", "stage": "확정" if risk else "말",
                             "basis": (f"{risk[0]['date']} {risk[0]['title']} · 해결 공시 전까지" if risk else "최근 6개월 걸림 공시 없음(앱이 모르는 사건이 있을 수 있음)"),
                             "n_sources": len(risk)}
        ev = [dict(e, year=None) for e in risk[:8]]
    elif kind == "씨앗":
        for y in YEARS:
            years[str(y)] = {"judgment": "씨앗", "stage": "계획" if y >= cy + 1 else "말",
                             "basis": f"매출 미미·판정 전 · 구조 {s1:+.1f}" + (f" · 공시 {pos_conf[0]['title'][:30]}" if pos_conf else ""),
                             "n_sources": len(pos_conf)}
        ev = [dict(e, year=None) for e in (pos_conf + nn[:3])[:8]]
    elif kind == "원료":
        a_b = N["q_opm"]
        tone = _tone(nn)
        for y in YEARS:
            if y == cy and a_b:
                a, b = a_b
                j = "우호" if a > b + 0.5 and tone >= 0 else "역풍" if a < b - 0.5 or tone < -1 else "중립"
                years[str(y)] = {"judgment": j, "stage": "숫자", "basis": f"{cy} {N['q']} 누적 영업이익률 {b:.1f}→{a:.1f}% · 관련 뉴스 논조 {tone:+d}", "n_sources": 1}
            else:
                j = "역풍" if tone <= -2 else "우호" if tone >= 2 else "중립"
                years[str(y)] = {"judgment": j, "stage": "말" if y < 2029 else "계획", "basis": f"원가·가격 노드 — 관련 뉴스 논조 {tone:+d}" + (" · 컨센 밖·추정" if y == 2029 else ""), "n_sources": 0}
        ev = [dict(e, year=None) for e in nn[:6]]
    else:
        w = 1.0 if main else 0.0                    # 회사 전체 숫자(실적·컨센)는 주력 노드에만 — 보조 노드는 노드 근거로
        tone = _tone(nn)
        # 2026: 실적(분기 누적, 주력·큰 노드만 회사 숫자 사용)
        for y in YEARS:
            parts_txt, src, sc, stage = [], 0, 0.0, "계획"
            if y == cy and N["q_growth"] is None and main:
                g, _ = _cons_growth(N, cy)
                if g is not None:
                    sc += _sgn(g, 0.05) or 0
                    parts_txt.append(f"분기 실적 없음 → 컨센 {cy}E 매출 {g * 100:+.1f}%")
            if y == cy and N["q_growth"] is not None and (main or (share_pct or 0) >= 60):
                g = N["q_growth"]
                sc += _sgn(g, 0.05) or 0
                stage, src = "숫자", 1
                parts_txt.append(f"{cy} {N['q']} 누적 매출 {g * 100:+.1f}%")
                if N["q_opm"]:
                    a, b = N["q_opm"]
                    sc += 0.5 if a > b + 0.5 else -0.5 if a < b - 0.5 else 0
                    parts_txt.append(f"영업이익률 {b:.1f}→{a:.1f}%")
            elif cy < y <= cy + 2:
                g, og = _cons_growth(N, y)
                if g is not None:
                    sc += w * (_sgn(g, 0.05) or 0)
                    parts_txt.append(f"컨센 {y}E 매출 {g * 100:+.1f}%")
                if og is not None:
                    sc += w * (_sgn(og, 0.10) or 0) * 0.5
                    parts_txt.append(f"보정 영업이익 {og * 100:+.0f}%")
                r3 = N["rev3"].get((y, "op_income"))
                if r3 is not None and abs(r3) >= 3:
                    sc += w * (0.5 if r3 > 0 else -0.5)
                    parts_txt.append(f"3개월 수정 {r3:+.0f}%")
                stage = "확정" if pos_conf or neg_conf else "계획"
            # 확정 공시(계약·투자 +, 해지·증자 −) — 2026~2028에 반영
            if y <= cy + 2:
                if pos_conf:
                    sc += 0.5
                    parts_txt.append(f"확정 공시 +{len(pos_conf)}건")
                if neg_conf:
                    sc -= 0.5
                    parts_txt.append(f"부담 공시 {len(neg_conf)}건")
                src += len({e["title"][:40] for e in pos_conf})
            if not main and y <= cy + 2 and abs(tone) >= 2:     # 보조 노드: 관련 뉴스 논조(말 단계)
                sc += 0.5 if tone > 0 else -0.5
                parts_txt.append(f"관련 뉴스 논조 {tone:+d}")
            # 구조 흐름(2029는 이것만, 보조 노드는 해마다 온전히)
            sw = 1.0 if (y == 2029 or not main) else 0.5
            if s1:
                sc += sw * (1 if s1 >= 0.8 else -1 if s1 <= -0.8 else s1 / 0.8 * 0.5)
                parts_txt.append(f"구조 {parts[0][0]} {s1:+.1f}" if parts else f"구조 {s1:+.1f}")
            if y == 2029:
                stage = "계획"
                parts_txt.append("컨센 밖·추정")
            j = "우호" if sc >= 1.0 else "역풍" if sc <= -1.0 else "중립"
            if y > cy and not parts_txt:
                j = "중립"
            years[str(y)] = {"judgment": j, "stage": stage, "basis": " · ".join(parts_txt) or "근거 없음", "n_sources": src, "score": round(sc, 2)}
        ev = [dict(e, year=None) for e in (pos_conf + neg_conf)[:6]] + [dict(e, year=None) for e in nn[:4]]
    apply_learned(years, learned, kind, main, disc, cy)
    land, land_why = decide_land(kind, s1, N, pos_conf, main)
    texts = write_texts(node, kn, parts, s1, N, nd, nn, share_pct, main, land_why)
    return {"years": years, "land": land, "land_why": land_why, "evidence": ev, "structural": s1, "learned": learned, **texts}


def node_query(company, name):
    """노드별 뉴스 검색어: 회사 이름 + 노드 이름의 핵심 낱말 2개(번호·괄호 밖)"""
    core = re.sub(r"^[①-⑳0-9.\s]+", "", name or "")
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"\(.*?\)", " ", core)) if w not in ("사업", "부문", "제품", "기타")][:2]
    return " ".join([company] + words).strip()


def learned_view(node, nd, nn):
    """Claude 판정에서 배운 모델로 이 노드 근거 글(노드 이름 + 관련 공시·뉴스 제목)을 읽음"""
    from . import hj_learn
    m = hj_learn.load()
    text = " ".join([node["name"]] + [e["title"] for e in nd[:10]] + [e["title"] for e in nn[:30]])
    p, why = hj_learn.predict(m, text) if m else (None, [])
    cue = hj_learn.cues(" ".join(e["title"] for e in nn[:30]))
    up = sum(1 for e in nn[:30] if hj_learn.cues(e["title"]).count("#증가")) - sum(1 for e in nn[:30] if "#감소" in hj_learn.cues(e["title"]))
    return {"probs": p, "why": why, "n_titles": len(nn), "net_dir": up, "cues": cue}


def apply_learned(years, L, kind, main, disc, cy):
    """학습 모델·방향 낱말을 연도 칸에 반영(규칙 점수와 합침). 근거 글이 적으면 쓰지 않음"""
    p = L.get("probs")
    if not p or L["n_titles"] < 2 and kind not in ("사람",):
        return
    top = max(p, key=p.get)
    tag = f"학습 모델 {top} {p[top]:.0%}"
    for y in YEARS:
        c = years.get(str(y))
        if not c or y > cy + 2:
            continue
        j = c["judgment"]
        if kind == "사람" and j != "걸림" and p.get("걸림", 0) >= 0.6:
            c.update(judgment="걸림", stage="말", basis=c["basis"] + f" · {tag}(관련 뉴스 {L['n_titles']}건)")
            continue
        if kind in ("사업부", "고객") and not main and p.get("씨앗", 0) >= 0.65 and j == "중립":
            c.update(judgment="씨앗", basis=c["basis"] + f" · {tag}")
            continue
        if kind in ("사업부", "고객", "원료") and j in ("우호", "중립", "역풍"):
            sc = (c.get("score") or 0) + 0.8 * (p.get("우호", 0) - p.get("역풍", 0) - 0.5 * p.get("걸림", 0)) + 0.25 * max(-2, min(2, L["net_dir"]))
            nj = "우호" if sc >= 1.0 else "역풍" if sc <= -1.0 else "중립"
            if nj != j:
                c.update(judgment=nj, basis=c["basis"] + f" · {tag} · 뉴스 방향 {L['net_dir']:+d}")
            c["score"] = round(sc, 2)


def collect_node_news(conn, code, nodes, company, days=183):
    """노드마다 '회사 + 노드 낱말'로 뉴스를 찾아 저장(Claude의 웹 조사에 해당). 노드별 7일에 한 번."""
    from .collectors import news as _news
    since = dt.date.today() - dt.timedelta(days=days)
    got = 0
    for n in nodes:
        if n.get("kind") == "사람":
            q = f"{company} 소송"
        else:
            q = node_query(company, n["name"])
        key = f"{code}:{q}"
        if db.fetched_recently(conn, "hj_news", key, 7):
            continue
        try:
            if _news.naver_key():            # 네이버 API: 노드당 최대 2쪽(200건) — 하루 한도 25,000건에 비해 작게
                _, items = _news.naver(q, since, max_pages=2)
                src = "네이버 뉴스 API"
            else:
                src, _, items = _news.fetch(q, since)
            got += db.add_news(conn, code, items, f"{src}(노드 조사)", q)
            db.log_fetch(conn, "hj_news", key, code, True, len(items))
        except Exception as e:     # noqa: BLE001
            db.log_fetch(conn, "hj_news", key, code, False, 0, str(e)[:200])
    conn.commit()
    return got


def _tone(items):
    """뉴스 제목 논조 합(+ 좋은 낱말, − 나쁜 낱말)"""
    return sum(1 if e.get("dir") == "+" else -1 if e.get("dir") == "-" else 0 for e in items)


def decide_land(kind, s1, N, pos_conf, main):
    """네 가지 땅(설계도 6-3) — 수요가 있는 노드에만"""
    if kind in ("사람", "씨앗"):
        return None, "수요 판정 대상 아님"
    cy = N["cy"]
    r = N["rev"]
    g = (r.get(cy - 1) / r.get(cy - 3)) ** 0.5 - 1 if r.get(cy - 1) and r.get(cy - 3) and r[cy - 3] > 0 else None
    m1, m3 = N["opm"].get(cy - 1), N["opm"].get(cy - 3)
    demand = s1 > 0 or (_cons_growth(N, cy + 1)[0] or 0) > 0.03
    if not demand:
        return None, "수요가 뚜렷하지 않음"
    root = bool(pos_conf)
    if not main:
        return None, "회사 전체 숫자로는 이 노드의 몫을 가를 수 없음(사업부 숫자 필요)"
    if g is not None and g > 0.03 and m1 is not None and m3 is not None and m1 < m3 - 1:
        return "가시떨기", f"매출 연 {g * 100:+.0f}%인데 영업이익률 {m3:.1f}→{m1:.1f}%로 하락(이익을 막는 것이 있음)"
    if g is not None and g > 0.30 and not root:
        return "돌밭", f"매출 연 {g * 100:+.0f}% 급증, 반복 근거(공급계약·투자 공시) 없음"
    if g is not None and g < 0 and s1 > 0:
        return "길가", f"구조 수요는 우호인데 회사 매출 연 {g * 100:+.0f}%(몫이 남에게 감)"
    if g is not None and g > 0 and (m1 is None or m3 is None or m1 >= m3 - 1):
        return "좋은 땅", f"매출 연 {g * 100:+.0f}% · 이익률 유지·개선" + (" · 확정 공시" if root else "")
    return None, "판단할 숫자 부족"


def write_texts(node, kn, parts, s1, N, nd, nn, share_pct, main, land_why):
    """6칸 글(틀 문장) — 숫자는 DB 값만"""
    cy = N["cy"]
    dem = []
    if parts:
        dem.append(" · ".join(f"{nm} {'+' if c > 0 else ''}{c:.1f}" for nm, c, _ in parts[:2]))
    g1, _ = _cons_growth(N, cy + 1)
    if g1 is not None and main:
        dem.append(f"컨센 {cy + 1}E 매출 {g1 * 100:+.0f}%")
    if nn:
        dem.append(f"{nn[0]['title'][:40]}({nn[0]['date'][5:]})")
    sh = [f"매출 비중 {share_pct:.0f}%" if share_pct else "매출 비중 미확인"]
    if land_why and "아님" not in land_why:
        sh.append(land_why + "(해석)")
    if kn and kn.get("why"):
        sh.append(kn["why"][:60])
    nums = []
    if N["q"] and N["q_growth"] is not None and main:
        nums.append(f"{cy} {N['q']} 누적 매출 {N['q_rev']:,.0f}억({N['q_growth'] * 100:+.0f}%)" if N["q_rev"] else f"{cy} {N['q']} 누적 매출 {N['q_growth'] * 100:+.0f}%")
    if N["opm"].get(cy - 1) is not None and main:
        nums.append(f"{cy - 1} 영업이익률 {N['opm'][cy - 1]:.1f}%")
    c1 = (N["cons"].get(cy + 1) or {}).get("op")
    if c1 is not None and main:
        nums.append(f"컨센 {cy + 1}E 영업이익 {c1:,.0f}억(×0.83 → {c1 * CUT:,.0f}억)")
    trig = []
    if kn and kn.get("watch"):
        trig.append(kn["watch"][:60])
    if nd:
        trig.append(f"'{nd[0]['title'][:24]}' 후속 공시")
    return {"demand": " · ".join(dem) or "모름(구조 흐름·컨센서스·관련 뉴스 없음)",
            "share": " · ".join(sh), "numbers": " · ".join(nums) or ("모름(이 노드만의 숫자 없음 — 사업부 실적 미공개)" if not main else "모름(실적·컨센서스 없음)"),
            "triggers": " / ".join(trig) or "모름"}


def app_nodes(conn, code, P, K):
    """① 노드 뽑기: Claude 노드가 있으면 그 목록, 없으면 사업부(지식 사전 노드로 묶음) + 씨앗 + 걸림"""
    from . import nodeview
    rows = conn.execute("SELECT node_id, name, kind, share_pct FROM hj_node WHERE code=? AND origin='claude' ORDER BY node_id", (code,)).fetchall()
    out = []
    if rows:
        for r in rows:
            kn, _ = _link_name(r["name"], K)
            out.append({"node_id": r["node_id"], "name": r["name"], "kind": r["kind"] or "사업부", "share_pct": r["share_pct"], "kn": kn,
                        "from": "Claude 노드를 앱이 다시 판정"})
        return out, "claude"
    res = nodeview.company(conn, code, P, K)
    groups = {}
    for s in res["segments"]:
        if s.get("excluded") or not s.get("node_id"):
            continue
        g = groups.setdefault(s["node_id"], {"names": [], "pct": 0.0, "hidden": s.get("hidden")})
        g["names"].append(s["segment"])
        g["pct"] += s.get("pct") or 0
    for nid, g in sorted(groups.items(), key=lambda x: -x[1]["pct"]):
        kn = K["nodes"][nid]
        out.append({"name": f"{kn['name']}({', '.join(g['names'][:2])})" if not g["hidden"] else f"{kn['name']}(기업개요)",
                    "kind": "씨앗" if g["hidden"] else "사업부", "share_pct": g["pct"] or None, "kn": nid, "from": "앱이 뽑음"})
    disc, _ = evidence_pool(conn, code)
    if any(e["risk"] for e in disc):
        out.append({"name": "사람·지배구조·자금", "kind": "사람", "share_pct": None, "kn": None, "from": "걸림 공시"})
    out = out[:6]
    for i, n in enumerate(out, 1):
        n["node_id"] = i
    return out, "app"


def _link_name(name, K):
    from .nodeview import _token_hits
    best = None
    for nid, n in K["nodes"].items():
        for kw in n.get("keywords") or []:
            if len(kw) >= 2 and (_token_hits(name, kw) or kw in name):
                if best is None or len(kw) > best[1]:
                    best = (nid, len(kw))
    return (best[0] if best else None), None


def run_app(conn, code, P, collect_news=False):
    """앱 판정 한 회사 전체 → origin='app'으로 저장. collect_news=True면 노드별 뉴스부터 찾음. 반환: (doc, 경고)"""
    from . import nodeview
    ensure(conn)
    K = nodeview.knowledge()
    nodes, basis = app_nodes(conn, code, P, K)
    cname = (conn.execute("SELECT name FROM companies WHERE stock_code=?", (code,)).fetchone() or {"name": code})["name"] or code
    for n in nodes:
        n["company"] = cname
    if collect_news:
        collect_node_news(conn, code, nodes, cname)
    N = company_numbers(conn, code, P)
    disc, news = evidence_pool(conn, code)
    name = (conn.execute("SELECT name FROM companies WHERE stock_code=?", (code,)).fetchone() or {"name": code})["name"]
    main_id = max(nodes, key=lambda n: (n.get("share_pct") or 0, -n["node_id"]))["node_id"] if nodes else None
    doc_nodes = []
    for n in nodes:
        kn = K["nodes"].get(n["kn"]) if n.get("kn") else None
        is_main = n["node_id"] == main_id and n["kind"] == "사업부"
        r = judge_node(n, kn, K, N, disc, news, n.get("share_pct"), is_main)
        doc_nodes.append({"node_id": n["node_id"], "name": n["name"], "share_pct": n.get("share_pct"), "kind": n["kind"], "kn_node": n.get("kn"),
                          "demand": r["demand"], "share": r["share"], "numbers": r["numbers"], "triggers": r["triggers"], "land": r["land"],
                          "raw_judgment": f"{n['from']} · 구조 {r['structural']:+.1f} · 땅 근거: {r['land_why']}" + learned_note(r.get("learned")), "years": r["years"],
                          "evidence": r["evidence"]})
    doc = {"code": code, "name": name, "asof": db.today(), "nodes": doc_nodes, "verdict": write_verdict(doc_nodes, N),
           "memo": f"노드 출처: {'Claude 노드 목록' if basis == 'claude' else '앱이 사업부·씨앗·걸림으로 뽑음'} · 공시 {len(disc)}건 · 뉴스 {len(news)}건(6개월)"}
    return doc, save(conn, doc, "app", "앱 판정(규칙)", "앱 규칙")


def learned_note(L):
    if not L or not L.get("probs"):
        return " · 학습 모델: 근거 글 부족" if L else ""
    p = L["probs"]
    top = sorted(p.items(), key=lambda x: -x[1])[:2]
    return f" · 학습 모델(Claude 판정에서 배움): {', '.join(f'{k} {v:.0%}' for k, v in top)} · 관련 뉴스 {L['n_titles']}건 · 근거 낱말 {', '.join(w for w, _ in L['why'][:4])}"


def write_verdict(nodes, N):
    """결론 2~3문장: 방향(대표 판정의 매출 비중 가중) · 때(연도) · 걸림"""
    if not nodes:
        return "판정할 노드가 없습니다."
    biz = [n for n in nodes if n["kind"] == "사업부"]
    main = max(biz, key=lambda n: n.get("share_pct") or 0) if biz else nodes[0]
    rep = rep_judgment(main["years"])
    seq = [main["years"][str(y)]["judgment"] for y in YEARS]
    when = ""
    if len(set(seq)) > 1:
        turn = next((YEARS[i] for i in range(1, 4) if seq[i] != seq[i - 1]), None)
        when = f" {turn}년부터 '{seq[YEARS.index(turn)]}'로 바뀌는 흐름입니다." if turn else ""
    s = f"주력 '{main['name'][:30]}'은(는) 대표 판정 '{rep}'입니다.{when}"
    seeds = [n for n in nodes if n["kind"] == "씨앗"]
    if seeds:
        s += f" 씨앗 {len(seeds)}개({', '.join(n['name'][:15] for n in seeds[:2])})는 아직 판정 전입니다."
    if any(n["kind"] == "사람" and rep_judgment(n["years"]) == "걸림" for n in nodes):
        s += " 걸리는 것은 방향이 아니라 사람·지배구조·자금 쪽입니다."
    return s + " (앱 규칙 판정 — 해석)"


# ---------------------------------------------------------------- 비교·상태·화면
def stale_reasons(conn, code, asof, triggers_text):
    """다시 조사 필요: 조사 90일 넘음 · 조사 뒤 분기·반기·사업보고서 · '판정을 바꿀 것' 낱말이 조사 뒤 공시·뉴스에"""
    out = []
    try:
        a = dt.date.fromisoformat(str(asof)[:10])
    except (TypeError, ValueError):
        return ["조사일 없음"]
    if (dt.date.today() - a).days > STALE_DAYS:
        out.append(f"조사 {(dt.date.today() - a).days}일 지남")
    r = conn.execute("SELECT report_nm, rcept_dt FROM disclosures WHERE stock_code=? AND rcept_dt>? AND reprt_code IN ('11011','11012','11013','11014') "
                     "ORDER BY rcept_dt DESC LIMIT 1", (code, a.strftime("%Y%m%d"))).fetchone()
    if r:
        out.append(f"조사 뒤 {r['report_nm']} 공시({r['rcept_dt'][:4]}-{r['rcept_dt'][4:6]}-{r['rcept_dt'][6:]})")
    words = {w for w in re.findall(r"[가-힣A-Za-z]{2,}", triggers_text or "") if w not in ("공시", "확정", "여부", "지연", "회수", "금액")}
    if words:
        for t in conn.execute("SELECT title, rcept_dt d FROM events WHERE stock_code=? AND rcept_dt>? UNION ALL "
                              "SELECT title, replace(published,'-','') d FROM news WHERE stock_code=? AND published>?",
                              (code, a.strftime("%Y%m%d"), code, a.isoformat())):
            hitw = [w for w in words if w in (t["title"] or "")]
            if len(hitw) >= 1 and len(max(hitw, key=len)) >= 3:
                out.append(f"'판정을 바꿀 것' 낱말 '{max(hitw, key=len)}' 적중: {t['title'][:30]}")
                break
    return out


def load(conn, code, origin):
    ensure(conn)
    c = conn.execute("SELECT * FROM hj_company WHERE code=? AND origin=?", (code, origin)).fetchone()
    if not c:
        return None
    nodes = []
    for n in conn.execute("SELECT * FROM hj_node WHERE code=? AND origin=? ORDER BY node_id", (code, origin)):
        d = dict(n)
        d["years"] = {str(r["year"]): dict(r) for r in conn.execute("SELECT year, judgment, stage, basis, n_sources FROM hj_node_year "
                                                                      "WHERE code=? AND origin=? AND node_id=?", (code, origin, n["node_id"]))}
        d["evidence"] = [dict(e) for e in conn.execute("SELECT date, type, stage, dir, title, ref, read, year FROM hj_evidence WHERE code=? AND origin=? "
                                                       "AND node_id=? ORDER BY date DESC", (code, origin, n["node_id"]))]
        nodes.append(d)
    v = conn.execute("SELECT * FROM hj_verdict WHERE code=? AND origin=?", (code, origin)).fetchone()
    log = conn.execute("SELECT * FROM hj_run_log WHERE code=? AND origin=? ORDER BY id DESC LIMIT 1", (code, origin)).fetchone()
    return {"company": dict(c), "nodes": nodes, "verdict": v["text"] if v else None,
            "warnings": json.loads(log["warnings"]) if log and log["warnings"] else [], "log": dict(log) if log else None}


def agreement(conn, code=None):
    """같은 회사·같은 노드 번호·같은 해의 claude 판정과 app 판정 일치율(옛 판정 = 연도 없음은 대표 판정끼리만)"""
    ensure(conn)
    q = ("SELECT a.code, a.node_id, a.year, a.judgment ja, b.judgment jb FROM hj_node_year a JOIN hj_node_year b "
         "ON a.code=b.code AND a.node_id=b.node_id AND a.year=b.year AND a.origin='claude' AND b.origin='app'")
    args = ()
    if code:
        q += " WHERE a.code=?"
        args = (code,)
    rows = conn.execute(q, args).fetchall()
    reps = conn.execute("SELECT a.code, a.rep ra, b.rep rb FROM hj_node a JOIN hj_node b ON a.code=b.code AND a.node_id=b.node_id "
                        "AND a.origin='claude' AND b.origin='app'" + (" WHERE a.code=?" if code else ""), args).fetchall()
    n = len(reps)
    same = sum(1 for r in reps if r["ra"] == r["rb"])
    near = {("우호", "중립"), ("중립", "우호"), ("역풍", "중립"), ("중립", "역풍")}
    close = sum(1 for r in reps if r["ra"] == r["rb"] or (r["ra"], r["rb"]) in near)
    return {"nodes": n, "same": same, "rate": (same / n) if n else None, "close_rate": (close / n) if n else None,
            "year_cells": len(rows), "companies": len({r["code"] for r in reps})}


def payload(conn, code, P):
    ensure(conn)
    out = {"claude": load(conn, code, "claude"), "app": load(conn, code, "app"), "years": YEARS, "judgments": JUDGMENTS,
           "disclaimer": DISCLAIMER, "agree_company": agreement(conn, code), "agree_all": agreement(conn),
           "n_claude": conn.execute("SELECT COUNT(*) FROM hj_company WHERE origin='claude'").fetchone()[0],
           "n_app": conn.execute("SELECT COUNT(*) FROM hj_company WHERE origin='app'").fetchone()[0]}
    cl = out["claude"]
    if cl:
        trig = " ".join(n.get("triggers") or "" for n in cl["nodes"])
        out["stale"] = stale_reasons(conn, code, cl["company"]["asof"], trig)
    missing = [r["code"] for r in conn.execute("SELECT h.code FROM hj_company h WHERE h.origin='claude' AND NOT EXISTS "
                                               "(SELECT 1 FROM facts f WHERE f.stock_code=h.code)")]
    out["claude_not_in_db"] = missing
    return out


# ---------------------------------------------------------------- Claude 조사 요청 파일(설계도 6·7-2절 규격)
INSTRUCTIONS = """당신은 한국 상장기업 한 장의 '질문 1 · 무엇으로 버나 — 노드별 4년(2026~2029) 우호 판정'을 조사해 쓰는 애널리스트입니다.
아래 evidence(앱이 모은 숫자·공시·뉴스·이전 판정)를 먼저 읽고, 필요하면 공시·뉴스·웹을 더 찾아 판단하세요. 가격·목표가는 판정에 쓰지 마세요.
규격(반드시 지킴)
1. 노드 3~6개: 이 회사가 돈을 버는 고리(사업부·제품·고객·원료·사람 층). kind는 사업부|씨앗|원료|고객|사람.
2. 노드마다 6칸: name, demand(수요 — 위에서 아래: 산업·고객 쪽 힘), share(몫 — 아래에서 위: 그 힘이 이 회사에 오는가), numbers(이미 숫자로 찍힌 것), triggers(판정을 바꿀 사건). 모르면 '모름' + 이유.
3. years: 2026·2027·2028·2029 네 칸에 judgment(우호|중립|역풍|씨앗|걸림) · stage(말|계획|확정|숫자) · basis(근거 한 줄).
   2026 = 확정 실적(분기 누적, stage 상한 숫자) / 2027·2028 = 컨센서스(×0.83)·확정 공시(상한 확정) / 2029 = 산업 전망·회사 계획(상한 계획, '컨센 밖·추정').
4. land(수요가 있는 노드만): 좋은 땅(방향+뿌리+몫) | 가시떨기(매출↑ 이익 정체) | 돌밭(빨리 오르나 반복 근거 없음) | 길가(몫이 남에게 감).
5. 두 증인: '우호'는 그 해에 확정 이상 원천 2개(서로 독립). 1개면 basis에 '(원천 1)'. '역풍'·'걸림'은 1개로 충분. 회사 가이던스·IR·AI 요약은 증인 아님.
6. evidence: 각 근거에 date(YYYY-MM-DD) · type(공시|숫자|언론|기관|회사 주장|AI 주장) · stage · dir(+|−) · title · ref(주소 또는 공시번호) · read(본문 확인 true/false). 날짜 없는 사실 금지, 12개월 넘은 사건은 '지난 일'.
7. verdict: 2~3문장, 방향과 때를 나눠 적음(예: "방향은 맞고, 걸리는 것은 때"). 해석은 (해석)으로 표시.
돌려줄 것: 아래 모양의 JSON만(설명 글 없이). 여러 회사면 {"companies": [ ... ]}.
{"code":"6자리","asof":"YYYY-MM-DD","nodes":[{"name":"...","share_pct":숫자|null,"kind":"사업부","demand":"...","share":"...","numbers":"...","triggers":"...","land":"좋은 땅",
 "years":{"2026":{"judgment":"우호","stage":"숫자","basis":"..."},"2027":{...},"2028":{...},"2029":{...}},
 "evidence":[{"year":2026,"date":"2026-08-07","type":"공시","stage":"숫자","dir":"+","title":"잠정실적","ref":"DART 접수번호","read":true}]}],
 "verdict":"..."}"""


def export(conn, codes, P):
    comps = []
    for c in codes:
        N = company_numbers(conn, c, P)
        disc, news = evidence_pool(conn, c)
        prev = load(conn, c, "claude")
        app = load(conn, c, "app")
        name = (conn.execute("SELECT name FROM companies WHERE stock_code=?", (c,)).fetchone() or {"name": None})["name"]
        from . import profile
        pf = profile.get(conn, c) or {}
        comps.append({"code": c, "name": name, "company_overview": pf.get("wr_lines"), "business_overview_dart": (pf.get("dart_overview") or "")[:1200],
                      "products_dart": pf.get("dart_products"),
                      "numbers": {"latest_quarter": N["q"], "quarter_cum_revenue_growth": N["q_growth"], "quarter_op_margin_now_prev": N["q_opm"],
                                  "revenue_by_year_eok": N["rev"], "op_margin_by_year": N["opm"],
                                  "consensus_eok": {str(k): v for k, v in N["cons"].items()}, "consensus_3m_revision_pct": {f"{k[0]}_{k[1]}": v for k, v in N["rev3"].items()},
                                  "consensus_cut": CUT},
                      "disclosures_6m": [{k: e[k] for k in ("date", "title", "stage", "dir")} for e in disc[:40]],
                      "news_titles_6m": [f"{e['date']} {e['title']}" for e in news[:60]],
                      "previous_claude": {"asof": prev["company"]["asof"], "nodes": [{k: n[k] for k in ("name", "kind", "demand", "share", "numbers", "triggers", "rep", "raw_judgment")} for n in prev["nodes"]],
                                          "verdict": prev["verdict"]} if prev else None,
                      "app_rule_judgment": {"nodes": [{"name": n["name"], "years": {y: v["judgment"] for y, v in n["years"].items()}, "land": n["land"]} for n in app["nodes"]]} if app else None})
    return {"instructions": INSTRUCTIONS, "made_at": db.now(), "companies": comps}
