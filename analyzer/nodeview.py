"""화면 R「사업부 우호 판정」 — 앱 안에서 판정하는 지식·규칙 엔진 (사용자 지시 2026-11-18)

구조(같은 자료면 언제나 같은 답, 이유를 문장으로 보여 줌)
 ① 지식 사전: knowledge/themes.json(세계 경제 구조 흐름) · knowledge/nodes.json(산업·제품 노드와 흐름별 영향 −2~+2)
    + 데이터 폴더 knowledge_review.json(Claude 검토 결과를 넣으면 사전 위에 덮어씀 — 사용자 아이디어: 가끔 Claude 수준 판단으로 갱신)
 ② 숫자 신호(앱 DB): 사업부 매출 흐름(WiseReport 구성비 × 회사 매출), 컨센서스 3개월 수정률, 매출 전망(올해→내후년)
 ③ 판정: 구조 점수 + 숫자 신호 → 우호 / 약한 우호 / 중립 / 약한 비우호 / 비우호 + 확신 정도 + 반대 근거 + 판정을 바꿀 지표
뉴스 기사 수는 점수에 넣지 않고 '관심도'로만 보여 준다. 숫자는 앱 DB 값만 쓴다.
리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님
"""
import datetime as dt
import json
import os
import re
import shutil

from . import config, db

KDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
VERDICTS = ["우호", "약한 우호", "중립", "약한 비우호", "비우호"]
GENERIC = {"기타", "용역", "상품", "제품", "매출", "서비스", "임대", "기타매출", "기타부문", "제품(내수)", "제품(수출)",
           "상품(내수)", "상품(수출)", "내수", "수출", "용역부문 등", "기타 등", "제품 등", "상품 등"}
DISCLAIMER = "리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님"
REVIEW_STALE_DAYS = 90


# ---------------------------------------------------------------- 지식 사전
def review_path():
    return os.path.join(config.DATA_DIR, "knowledge_review.json")


def _load(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def knowledge():
    """기본 사전 + Claude 검토(있으면 덮어씀). 반환: {themes:{id:..}, nodes:{id:..}, review:{...} or None}"""
    T = {t["id"]: dict(t, origin="기본") for t in _load(os.path.join(KDIR, "themes.json"))["themes"]}
    N = {n["id"]: dict(n, origin="기본") for n in _load(os.path.join(KDIR, "nodes.json"))["nodes"]}
    rv = _load(review_path())
    if rv:
        for t in rv.get("themes") or []:
            T[t["id"]] = dict(T.get(t["id"], {}), **t, origin=f"Claude 검토 {rv.get('as_of', '')}")
        for n in rv.get("nodes") or []:
            base = N.get(n["id"], {"keywords": [], "ksic": [], "themes": {}, "why": "", "watch": "", "group": "기타"})
            m = dict(base, **{k: v for k, v in n.items() if k != "themes"}, origin=f"Claude 검토 {rv.get('as_of', '')}")
            m["themes"] = dict(base.get("themes") or {}, **(n.get("themes") or {}))
            m["keywords"] = list(dict.fromkeys((base.get("keywords") or []) + (n.get("keywords") or [])))
            N[n["id"]] = m
    return {"themes": T, "nodes": N, "review": rv}


# ---------------------------------------------------------------- DB(연결·판정 기록)
def ensure(conn):
    db.ensure_tables(conn, """
    CREATE TABLE IF NOT EXISTS node_links (
      stock_code TEXT NOT NULL, segment TEXT NOT NULL, node_id TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT,
      UNIQUE(stock_code, segment));
    CREATE TABLE IF NOT EXISTS node_verdict_log (
      day TEXT NOT NULL, stock_code TEXT NOT NULL, segment TEXT NOT NULL, node_id TEXT, verdict TEXT, score REAL,
      confidence TEXT, signals TEXT, UNIQUE(day, stock_code, segment));
    CREATE INDEX IF NOT EXISTS ix_nvl_code ON node_verdict_log(stock_code, day);
    """)


def set_link(conn, code, segment, node_id, source="user"):
    ensure(conn)
    if node_id:
        conn.execute("INSERT INTO node_links(stock_code, segment, node_id, source, updated_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(stock_code, segment) DO UPDATE SET node_id=excluded.node_id, source=excluded.source, "
                     "updated_at=excluded.updated_at", (code, segment, node_id, source, db.now()))
    else:
        conn.execute("DELETE FROM node_links WHERE stock_code=? AND segment=?", (code, segment))
    conn.commit()


# ---------------------------------------------------------------- 사업부 → 노드 연결
def is_generic(name):
    s = re.sub(r"\s+", "", name or "")
    return name in GENERIC or s in {re.sub(r"\s+", "", g) for g in GENERIC} or bool(re.fullmatch(r"(기타|용역|상품|제품)(\(.*\))?(등|부문)?", s))


def link_segment(name, ksic, K, user_links, review_links, main=None):
    """반환: (노드 id 또는 None, 연결 방법 설명). 순서: 사용자 > Claude 검토 > 이름 낱말 > 기업개요 주력(막연한 이름일 때) > 업종코드"""
    if name in user_links:
        return user_links[name], "사용자 지정"
    if name in review_links:
        return review_links[name], "Claude 검토"
    best = None
    for nid, n in K["nodes"].items():
        for kw in n.get("keywords") or []:
            if kw and kw.lower() in (name or "").lower():
                if best is None or len(kw) > best[1]:
                    best = (nid, len(kw), kw)
    if best:
        return best[0], f"이름 낱말 '{best[2]}'"
    if main:
        return main[0], f"기업개요 낱말 '{main[2]}'(주력 사업으로 연결)"
    if ksic:
        bk = None
        for nid, n in K["nodes"].items():
            for k in n.get("ksic") or []:
                if ksic.startswith(k) and (bk is None or len(k) > bk[1]):
                    bk = (nid, len(k), k)
        if bk:
            return bk[0], f"회사 업종코드 {ksic}(앞자리 {bk[2]})로 추정"
    return None, "연결할 노드를 찾지 못함"


# ---------------------------------------------------------------- 회사 정의(기업개요·사업보고서)
BIZ_NEAR = re.compile(r"^.{0,12}?(사업|제조|생산|판매|제작|개발|영위|연구)")
_PRE_OK = set(" ,·/()[]\"'“”‘’·-")
_POST_PARTICLES = ("으로", "과", "와", "및", "을", "를", "의", "이", "가", "은", "는", "에", "로", "등", "와의", "과의")


def _token_hits(text, kw):
    """낱말 단위로만 찾기: '방송'이 '방송영상물' 안에서 걸리지 않게. 뒤에는 조사·문장부호·숫자·영문만 허용"""
    out, low, k = [], text.lower(), kw.lower()
    st = 0
    while True:
        i = low.find(k, st)
        if i < 0:
            return out
        st = i + 1
        pre = text[i - 1] if i > 0 else " "
        post = text[i + len(kw):]
        if pre not in _PRE_OK and not ("A" <= pre.upper() <= "Z" and False):
            if not (pre.isspace()):
                continue
        if post and not (post[0] in _PRE_OK or post[0].isspace() or post[0].isdigit() or post[0].isascii() and post[0].isalpha()
                         or post.startswith(_POST_PARTICLES)):
            continue
        out.append(i)


def profile_nodes(prof, K):
    """기업개요·사업의 개요 문장에서 노드 찾기. 반환: [(노드 id, 점수, 대표 낱말, 사업으로 쓰였나)] 점수 큰 순.
    낱말 단위로만 세고(방송영상물 ≠ 방송), 괄호 안은 빼고, '주력'이 있는 문장은 2배.
    '사업으로 쓰였나' = 낱말 뒤 12자 안에 사업·제조·생산·판매·제작·개발·영위가 있음(고객 이름 '방송국·플랫폼에 공급'과 구별)."""
    if not prof:
        return []
    sents = []
    for line in (prof.get("wr_lines") or []) + [prof.get("dart_overview") or ""]:
        sents += [x for x in re.split(r"(?<=[.다음함])\s+|\n", line or "") if x.strip()]
    sents += [f"{p.get('name', '')} {p.get('desc', '')} 제조" for p in prof.get("dart_products") or []]
    hits = {}
    for snt in sents:
        clean = re.sub(r"\([^)]*\)", " ", snt)
        w = 2 if "주력" in clean else 1
        for nid, n in K["nodes"].items():
            for kw in n.get("keywords") or []:
                if len(kw) < 2:
                    continue
                for pos in _token_hits(clean, kw):
                    biz = bool(BIZ_NEAR.search(clean[pos + len(kw):]))
                    sc, ex, b = hits.get(nid, (0, kw, False))
                    hits[nid] = (sc + w * len(kw) / 2, ex if len(ex) >= len(kw) else kw, b or biz)
    return sorted([(nid, round(sc, 1), kw, b) for nid, (sc, kw, b) in hits.items()], key=lambda x: -x[1])


def definition(prof):
    """한 줄 정의: '주력'이 있는 줄 > 첫 줄. 설립 연혁·상장 문구는 걷어 낸다(원문은 화면에 따로 보여 줌)."""
    lines = (prof or {}).get("wr_lines") or []
    if not lines:
        ov = (prof or {}).get("dart_overview")
        return (ov[:160] + "…") if ov and len(ov) > 160 else ov
    pick = next((l for l in lines if "주력" in l), lines[0])
    x = re.sub(r"^.*(?:신설된|설립된|출범한|설립되었으며|설립되었고|설립되어)\s*", "", pick).lstrip(" ,")
    x = re.split(r"(?:으로|로),\s", x)[0]
    x = re.sub(r"(임|함|음)?\.\s*$", "", x).strip()
    return x if len(x) >= 6 else pick


ROLE_ORDER = {"주력": 0, "최대 사업": 0, "부수": 1, "부수(비중 미확인)": 2, "기타": 3}


# ---------------------------------------------------------------- 신호
def structural(node, K):
    """구조 점수 = Σ(영향 × 흐름 강도), −2~+2로 자름. 반환: (점수, [(흐름 이름, 기여, 흐름 id)])"""
    parts = []
    for tid, imp in (node.get("themes") or {}).items():
        t = K["themes"].get(tid)
        if t is None or not imp:
            continue
        parts.append((t["name"], round(imp * float(t.get("strength", 1.0)), 2), tid))
    parts.sort(key=lambda x: -abs(x[1]))
    return max(-2.0, min(2.0, round(sum(p[1] for p in parts), 2))), parts


def _sig(v, th):
    return None if v is None else (1 if v >= th else -1 if v <= -th else 0)


def company_signals(conn, code, P):
    """회사 전체 숫자: 매출 2년 연평균, 내년 영업이익 컨센서스 3개월 수정률, 매출 전망(올해→내후년 연평균)"""
    from .goodco import Data
    from .report import consensus_trend
    D = Data(conn, code, P)
    cy = P["current"]
    r1, r3 = D.v(cy - 1, "revenue"), D.v(cy - 3, "revenue")
    g2 = (r1 / r3) ** 0.5 - 1 if r1 and r3 and r1 > 0 and r3 > 0 else None
    last_down = None
    if D.v(cy - 2, "revenue") and r1 and r3:
        last_down = r1 < D.v(cy - 2, "revenue") < r3
    rev = None
    try:
        for it in consensus_trend(conn, code, P).get("summary") or []:
            if it["metric"] == "op_income" and it["year"] in (cy + 1, cy):
                ch = (it.get("ch") or {}).get(3)
                if ch is not None and (rev is None or it["year"] == cy + 1):
                    rev = (ch, it["year"])
    except Exception:      # noqa: BLE001
        rev = None
    c0, c2 = D.c(cy, "revenue"), D.c(cy + 2, "revenue")
    fg = (c2 / c0) ** 0.5 - 1 if c0 and c2 and c0 > 0 and c2 > 0 else None
    return {"rev_g2": g2, "two_down": last_down, "op_rev3": rev, "fwd_g": fg, "n_est": D.n_est(cy + 1)}


def segment_flow(conn, code, seg, P, cs):
    """사업부 매출 흐름: 구성비 두 시점(9개월 이상 차이)이 있으면 구성비 × 회사 매출로 추정, 없으면 이 사업부가 매출 60% 이상일 때만
    회사 전체 매출로 대신. 반환: (값 −1/0/1 또는 None, 설명)"""
    hist = [r for r in conn.execute("SELECT as_of, pct FROM segments WHERE stock_code=? AND kind='mix' AND name=? ORDER BY as_of",
                                    (code, seg["name"]))]
    if len(hist) >= 2:
        a, b = hist[0], hist[-1]
        try:
            ya, yb = int(a["as_of"][:4]), int(b["as_of"][:4])
        except (TypeError, ValueError):
            ya = yb = None
        if ya and yb and yb > ya and a["pct"] and b["pct"]:
            from .goodco import Data
            D = Data(conn, code, P)
            ra, rb = D.v(ya - 1, "revenue") or D.v(ya, "revenue"), D.v(yb - 1, "revenue") or D.v(yb, "revenue")
            if ra and rb:
                g = ((b["pct"] * rb) / (a["pct"] * ra)) ** (1 / (yb - ya)) - 1
                return _sig(g, 0.05), f"구성비 {a['as_of']} {a['pct']:.1f}% → {b['as_of']} {b['pct']:.1f}% × 회사 매출 → 사업부 매출 연 {g * 100:+.1f}%(추정)"
    if (seg.get("pct") or 0) >= 60 and cs["rev_g2"] is not None:
        s = _sig(cs["rev_g2"], 0.05)
        if cs.get("two_down"):
            s = -1
        return s, f"회사 매출 최근 2년 연 {cs['rev_g2'] * 100:+.1f}%{' · 2년 연속 감소' if cs.get('two_down') else ''}(이 사업부가 매출 {seg['pct']:.0f}%라 회사 전체로 대신)"
    return None, "사업부 숫자 없음(구성비 시점이 하나뿐 — 매달 쌓이면 계산)"


def judge(node, K, seg, cs, flow):
    """반환: 판정 dict(같은 입력이면 같은 답)"""
    s1, parts = structural(node, K)
    w = 1.0 if (seg.get("pct") or 0) >= 60 else 0.5          # 회사 전체 숫자를 이 사업부에 얼마나 반영할지
    sig = {"flow": flow[0], "rev3": _sig(cs["op_rev3"][0], 3.0) if cs.get("op_rev3") else None, "fwd": _sig(cs["fwd_g"], 0.05)}
    total = s1 + (sig["flow"] or 0) + w * (sig["rev3"] or 0) + w * (sig["fwd"] or 0)
    v = "우호" if total >= 1.5 else "약한 우호" if total >= 0.5 else "중립" if total > -0.5 else "약한 비우호" if total > -1.5 else "비우호"
    side = 1 if total >= 0.5 else -1 if total <= -0.5 else 0
    data = [x for x in sig.values() if x is not None]
    agree = [x for x in data if x == side and side != 0]
    oppose = [x for x in data if x == -side and side != 0]
    if not data:
        conf = "낮음"
    elif len(data) >= 2 and not oppose and len(agree) >= 2 and (s1 > 0) == (side > 0) and abs(s1) >= 0.8:
        conf = "높음"
    else:
        conf = "중간"
    reasons = [f"구조: {nm} {c:+.1f}" for nm, c, _ in parts[:4]]
    nums = []
    if flow[0] is not None:
        nums.append(("사업부 매출 흐름", flow[0], flow[1]))
    else:
        nums.append(("사업부 매출 흐름", None, flow[1]))
    if cs.get("op_rev3"):
        nums.append(("시장 기대(영업이익 컨센 3개월)", sig["rev3"], f"{cs['op_rev3'][1]}년 영업이익 컨센서스 3개월 {cs['op_rev3'][0]:+.1f}%" + ("" if w == 1 else " — 회사 전체 숫자라 절반만 반영")))
    else:
        nums.append(("시장 기대(영업이익 컨센 3개월)", None, "컨센서스 추이 없음"))
    if cs.get("fwd_g") is not None:
        nums.append(("매출 전망(올해→내후년)", sig["fwd"], f"컨센 매출 연 {cs['fwd_g'] * 100:+.1f}%" + ("" if w == 1 else " — 회사 전체 숫자라 절반만 반영")))
    else:
        nums.append(("매출 전망(올해→내후년)", None, "매출 컨센 없음"))
    counter = []
    if side:
        counter += [f"{d} ({lab})" for lab, x, d in nums if x == -side]
        counter += [f"{nm}: {K['themes'][tid].get('counter', '')}" for nm, c, tid in parts if (c > 0) == (side > 0) and c != 0][:2]
        counter += [f"{nm} {c:+.1f}" for nm, c, _ in parts if (c > 0) != (side > 0) and c != 0][:2]
    return {"verdict": v, "score": round(total, 2), "structural": s1, "parts": parts, "signals": sig, "nums": nums,
            "confidence": conf, "counter": counter, "weight_company": w, "reasons": reasons}


# ---------------------------------------------------------------- 뉴스 관심도(점수 아님)
def news_interest(conn, code, seg_name, node, K):
    kws = [k for k in (node.get("keywords") if node else []) or [] if len(k) >= 2][:8] + \
          [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", seg_name or "") if w not in ("기타", "부문", "제품", "상품")]
    if not kws:
        return None
    today = dt.date.today()
    since = (today - dt.timedelta(days=365)).isoformat()
    rows = [r for r in conn.execute("SELECT published, title, link FROM news WHERE stock_code=? AND published>=? ORDER BY published DESC",
                                    (code, since)) if any(k.lower() in (r["title"] or "").lower() for k in kws)]
    cut = (today - dt.timedelta(days=90)).isoformat()
    recent = sum(1 for r in rows if r["published"] >= cut)
    before = (len(rows) - recent) / 3.0
    lab = "기사 없음" if not rows else "관심 증가" if recent > before * 1.5 + 1 else "관심 감소" if recent < before * 0.5 else "평소 수준"
    from .collectors import news as _news
    return {"label": lab, "recent": recent, "before_avg": round(before, 1), "n": len(rows),
            "top": [{"date": r["published"], "title": r["title"], "link": r["link"] or _news.search_link(r["title"])} for r in rows[:3]]}


# ---------------------------------------------------------------- 회사 한 곳
def company(conn, code, P, K=None):
    ensure(conn)
    K = K or knowledge()
    row = conn.execute("SELECT name, induty_code FROM companies WHERE stock_code=?", (code,)).fetchone()
    name, ksic = (row["name"], row["induty_code"]) if row else (code, None)
    as_of = (conn.execute("SELECT MAX(as_of) d FROM segments WHERE stock_code=? AND kind='mix'", (code,)).fetchone() or {"d": None})["d"]
    segs = [dict(r) for r in conn.execute("SELECT name, pct FROM segments WHERE stock_code=? AND kind='mix' AND as_of=? ORDER BY pct DESC",
                                          (code, as_of))] if as_of else []
    from . import profile as _profile
    prof = _profile.get(conn, code)
    seg_src = f"WiseReport 주요제품 매출구성({as_of})" if segs else None
    dp = (prof or {}).get("dart_products") or []
    if len(dp) >= 2 and 80 <= sum(x["pct"] for x in dp) <= 120:      # 사업보고서 주요 제품 표가 있으면 그것(품목·용도가 더 구체적)
        segs = [{"name": x["name"] + (f" — {x['desc']}" if x.get("desc") and x["desc"] != x["name"] else ""), "pct": x["pct"],
                 "link_text": f"{x['name']} {x.get('desc') or ''}"} for x in sorted(dp, key=lambda x: -x["pct"])]
        seg_src = f"DART 사업보고서 {prof.get('dart_year')} 주요 제품"
    pnodes = profile_nodes(prof, K)
    main = next(((nid, sc, kw) for nid, sc, kw, _ in pnodes if not K["nodes"][nid].get("broad")),
                (pnodes[0][:3] if pnodes else None))
    user = {r["segment"]: r["node_id"] for r in conn.execute("SELECT segment, node_id FROM node_links WHERE stock_code=?", (code,))}
    rv = K.get("review") or {}
    rlinks = {x["segment"]: x["node"] for x in rv.get("links") or [] if x.get("stock_code") == code}
    rverd = {x["segment"]: x for x in rv.get("verdicts") or [] if x.get("stock_code") == code}
    for s in list(user) + list(rlinks):                     # 구성비에 없는 사업부를 직접 연결한 경우
        if s not in {x["name"] for x in segs}:
            segs.append({"name": s, "pct": None})
    if not segs and ksic:                                   # 구성비가 없으면 회사 전체를 한 사업부로
        segs = [{"name": "회사 전체", "pct": 100.0}]
    cs = company_signals(conn, code, P)
    out = []
    today = dt.date.today()
    for sg in segs:
        nid, how = link_segment(sg["name"], ksic, K, user, rlinks) if sg["name"] in user or sg["name"] in rlinks else \
            link_segment(sg.get("link_text") or sg["name"], ksic, K, {}, {}, main if is_generic(sg["name"]) or sg["name"] == "회사 전체" else None)
        if nid is None and main:
            nid, how = main[0], f"기업개요 낱말 '{main[2]}'(주력 사업으로 연결)"
        small_generic = is_generic(sg["name"]) and (sg.get("pct") or 0) < 10 and sg["name"] not in user and sg["name"] not in rlinks
        r = {"segment": sg["name"], "pct": sg.get("pct"), "node_id": nid, "how": how}
        if small_generic or nid is None:
            r.update(excluded=True, why="비중 10% 미만의 '기타·용역' 항목이라 판정 제외" if small_generic else how)
        else:
            node = K["nodes"][nid]
            r.update(node_name=node["name"], node_why=node.get("why"), watch=node.get("watch"), node_origin=node.get("origin"),
                     **judge(node, K, sg, cs, segment_flow(conn, code, sg, P, cs)))
            r["news"] = news_interest(conn, code, sg["name"], node, K)
        cv = rverd.get(sg["name"])
        if cv:
            age = None
            try:
                age = (today - dt.date.fromisoformat(str(cv.get("as_of") or rv.get("as_of"))[:10])).days
            except ValueError:
                pass
            r["review"] = dict(cv, as_of=cv.get("as_of") or rv.get("as_of"),
                               stale=age is not None and age > int(cv.get("valid_days") or REVIEW_STALE_DAYS))
        out.append(r)
    pk = {nid: kw for nid, sc, kw, _ in pnodes}
    for x in out:
        if x.get("node_id") in pk:
            x["profile_kw"] = pk[x["node_id"]]
    # 숨은 부수 사업: 기업개요에는 있지만 매출 구성에 없는 사업(비중 미확인 — 회사 가중 점수에는 넣지 않음)
    linked = {x.get("node_id") for x in out}
    seg_text = " ".join(x["segment"] for x in out)
    for nid, sc, kw, biz in pnodes:
        n = K["nodes"][nid]
        covered = any(k and k in seg_text for k in n.get("keywords") or [])     # 이미 매출 구성 이름에 들어 있는 사업(예: '인쇄용지, 특수지')
        if nid in linked or covered or n.get("broad") or not biz:
            continue
        r = {"segment": f"{n['name']}(기업개요)", "pct": None, "node_id": nid, "how": f"기업개요 낱말 '{kw}'", "hidden": True,
             "node_name": n["name"], "node_why": n.get("why"), "watch": n.get("watch"), "node_origin": n.get("origin")}
        r.update(judge(n, K, {"pct": 0}, cs, (None, "비중 미확인 사업이라 사업부 숫자 없음")))
        r["news"] = news_interest(conn, code, n["name"], n, K)
        out.append(r)
        linked.add(nid)
    # 역할: 가장 큰 사업 = 주력(40% 이상) 또는 최대 사업, 5% 이상 부수, 5% 미만 기타
    ranked = sorted([x for x in out if not x.get("excluded") and not x.get("hidden") and x.get("pct") is not None], key=lambda x: -x["pct"])
    for i, x in enumerate(ranked):
        x["role"] = ("주력" if x["pct"] >= 40 else "최대 사업") if i == 0 else "부수" if x["pct"] >= 5 else "기타"
    for x in out:
        if x.get("hidden"):
            x["role"] = "부수(비중 미확인)"
        elif x.get("excluded"):
            x["role"] = "기타"
        x.setdefault("role", "부수")
    out.sort(key=lambda x: (ROLE_ORDER.get(x["role"], 9), -(x.get("pct") or 0)))
    judged = [x for x in out if not x.get("excluded") and x.get("pct")]
    tot = sum(x["pct"] for x in judged)
    share = {k: sum(x["pct"] for x in judged if (x["score"] >= 0.5 if k == "good" else x["score"] <= -0.5 if k == "bad" else -0.5 < x["score"] < 0.5))
             for k in ("good", "mid", "bad")}
    wscore = sum(x["pct"] * x["score"] for x in judged) / tot if tot else None
    prof_out = None
    if prof:
        prof_out = {"definition": definition(prof), "wr_lines": prof.get("wr_lines") or [], "wics": prof.get("wics"),
                    "wr_at": prof.get("wr_at"), "wr_url": prof.get("wr_url"), "dart_year": prof.get("dart_year"),
                    "dart_overview": (prof.get("dart_overview") or "")[:600] or None, "dart_products": dp, "dart_note": prof.get("dart_note"),
                    "dart_at": prof.get("dart_at"), "dart_rcept_no": prof.get("dart_rcept_no"),
                    "main_node": next((x.get("node_name") for x in out if x.get("role") in ("주력", "최대 사업")), None)
                    or (K["nodes"][main[0]]["name"] if main else None)}
    return {"code": code, "name": name, "ksic": ksic, "mix_as_of": as_of, "seg_source": seg_src, "segments": out, "company_signals": cs,
            "share": share, "share_total": tot, "wscore": wscore, "profile": prof_out, "summary": summary(out, wscore, prof_out)}


def summary(rows, wscore, prof=None):
    """회사 방향 한 줄: 주력 사업의 판정 → 부수 사업 → 가중 결과"""
    judged = [x for x in rows if not x.get("excluded")]
    if not judged:
        return "판정할 사업부가 없습니다(사업부 구성비·기업개요·업종코드 없음)."
    main = next((x for x in judged if x.get("role") in ("주력", "최대 사업")), judged[0])
    pc = lambda x: f"{x['pct']:.0f}%" if x.get("pct") is not None else "비중 미확인"
    s = f"{main.get('role', '주력')} '{main['segment']}'({pc(main)})는 {main['verdict']}"
    if main.get("parts"):
        s += f"({main['parts'][0][0]} {main['parts'][0][1]:+.1f})"
    subs = [x for x in judged if x is not main and x.get("role") != "기타"]
    if subs:
        s += ", 부수 사업은 " + ", ".join(f"'{x['segment']}'({pc(x)}) {x['verdict']}" for x in subs[:3])
    lab = "우호" if wscore is not None and wscore >= 0.5 else "비우호" if wscore is not None and wscore <= -0.5 else "중립"
    s += "." + (f" 매출 비중으로 가중하면 {lab} 쪽({wscore:+.2f})." if wscore is not None else "")
    return s


def log_today(conn, code, P):
    """판정을 하루 한 번 기록(나중에 실제 실적과 맞춰 보는 검증용)"""
    ensure(conn)
    res = company(conn, code, P)
    day = db.today()
    for r in res["segments"]:
        if r.get("excluded"):
            continue
        conn.execute("INSERT OR IGNORE INTO node_verdict_log(day, stock_code, segment, node_id, verdict, score, confidence, signals) "
                     "VALUES (?,?,?,?,?,?,?,?)", (day, code, r["segment"], r["node_id"], r["verdict"], r["score"], r["confidence"],
                                                   json.dumps(r["signals"], ensure_ascii=False)))
    conn.commit()


# ---------------------------------------------------------------- Claude 검토(내보내기·넣기)
INSTRUCTIONS = """당신은 한국 상장기업의 사업부가 세계 경제 구조에 우호적인지 판단하는 애널리스트입니다.
아래 'companies'는 앱이 지식 사전과 숫자 규칙으로 낸 판정과 근거입니다. 'knowledge'는 지금 앱의 지식 사전입니다.
할 일: 최신 정보와 당신의 판단으로 (1) 바뀐 세계 경제 흐름(themes) (2) 노드의 흐름별 영향(nodes) (3) 잘못 연결된 사업부(links)
(4) 사업부 판정에 대한 당신의 의견(verdicts)을 고쳐서, 아래 형식의 JSON 하나만 답하세요(설명 글 없이).
{
 "as_of": "YYYY-MM-DD",
 "themes": [{"id": "T_AI", "strength": 0.9, "confidence": "높음|중간|낮음", "desc": "...", "counter": "반대 근거"}],
 "nodes": [{"id": "기존 또는 새 id", "name": "...", "keywords": ["..."], "themes": {"T_AI": 2}, "why": "...", "watch": "판정을 바꿀 지표"}],
 "links": [{"stock_code": "6자리", "segment": "사업부 이름 그대로", "node": "노드 id"}],
 "verdicts": [{"stock_code": "6자리", "segment": "사업부 이름 그대로", "verdict": "우호|약한 우호|중립|약한 비우호|비우호",
               "confidence": "높음|중간|낮음", "reason": "근거(출처·날짜)", "valid_days": 90}]
}
규칙: 바꿀 것만 넣으세요(빈 목록 가능). themes 영향은 −2~+2 정수. 새 흐름 id는 'T_'로 시작. 숫자를 지어내지 말고 근거에 출처와 날짜를 쓰세요.
사업부 이름·종목코드는 받은 그대로 쓰세요."""


def export(conn, codes, P):
    K = knowledge()
    comps = []
    for c in codes:
        try:
            r = company(conn, c, P, K)
        except Exception as e:     # noqa: BLE001
            comps.append({"code": c, "error": f"{type(e).__name__}: {e}"})
            continue
        pf = r.get("profile") or {}
        comps.append({"stock_code": c, "name": r["name"], "industry_code": r["ksic"], "segments_as_of": r["mix_as_of"],
                      "segments_source": r.get("seg_source"), "company_overview": pf.get("wr_lines"), "wics": pf.get("wics"),
                      "business_overview_dart": pf.get("dart_overview"), "summary": r["summary"],
                      "segments": [{"segment": s["segment"], "role": s.get("role"), "pct": s["pct"], "node": s.get("node_id"), "link_how": s.get("how"),
                                    **({"excluded": s.get("why")} if s.get("excluded") else
                                       {"app_verdict": s["verdict"], "score": s["score"], "confidence": s["confidence"],
                                        "structural": [f"{n} {v:+.1f}" for n, v, _ in s["parts"]],
                                        "numbers": [f"{lab}: {d}" for lab, x, d in s["nums"]],
                                        "news_titles": [f"{t['date']} {t['title']}" for t in ((s.get("news") or {}).get("top") or [])]})}
                                   for s in r["segments"]]})
    return {"instructions": INSTRUCTIONS, "made_at": db.now(), "app": "corp-analyzer 화면 R",
            "knowledge": {"themes": list(K["themes"].values()),
                          "nodes": [{k: n[k] for k in ("id", "name", "group", "keywords", "themes", "why", "watch") if k in n}
                                    for n in K["nodes"].values()]},
            "companies": comps}


def validate_review(d, K=None):
    """Claude 검토 결과 검사. 반환: (정리된 dict, 메모 목록). 형식이 틀린 항목은 빼고 알려 준다."""
    K = K or knowledge()
    if not isinstance(d, dict):
        raise ValueError("JSON 객체가 아닙니다")
    notes, out = [], {"as_of": str(d.get("as_of") or db.today())[:10], "themes": [], "nodes": [], "links": [], "verdicts": []}
    conf = {"높음", "중간", "낮음"}
    for t in d.get("themes") or []:
        if not isinstance(t, dict) or not re.fullmatch(r"T_[A-Z0-9_]{1,30}", str(t.get("id") or "")):
            notes.append(f"흐름 형식 오류로 뺌: {str(t)[:60]}")
            continue
        x = {k: t[k] for k in ("id", "name", "desc", "counter", "confidence") if k in t}
        if "strength" in t:
            try:
                x["strength"] = min(max(float(t["strength"]), 0.0), 1.0)
            except (TypeError, ValueError):
                notes.append(f"{t['id']} 강도 숫자 아님")
        if x.get("confidence") not in (None, *conf):
            x.pop("confidence")
        if t["id"] not in K["themes"] and not x.get("name"):
            notes.append(f"새 흐름 {t['id']}에 이름이 없어 뺌")
            continue
        out["themes"].append(x)
    tids = set(K["themes"]) | {t["id"] for t in out["themes"]}
    for n in d.get("nodes") or []:
        if not isinstance(n, dict) or not re.fullmatch(r"[a-z0-9_]{2,40}", str(n.get("id") or "")):
            notes.append(f"노드 형식 오류로 뺌: {str(n)[:60]}")
            continue
        th = {}
        for k, v in (n.get("themes") or {}).items():
            try:
                v = int(round(float(v)))
            except (TypeError, ValueError):
                continue
            if k in tids and -2 <= v <= 2:
                th[k] = v
            else:
                notes.append(f"노드 {n['id']}의 흐름 영향 {k}:{v} 뺌(없는 흐름이거나 −2~+2 밖)")
        x = {k: n[k] for k in ("id", "name", "group", "why", "watch") if k in n}
        x["themes"] = th
        x["keywords"] = [str(k) for k in (n.get("keywords") or []) if str(k).strip()][:30]
        if n["id"] not in K["nodes"] and not x.get("name"):
            notes.append(f"새 노드 {n['id']}에 이름이 없어 뺌")
            continue
        out["nodes"].append(x)
    nids = set(K["nodes"]) | {n["id"] for n in out["nodes"]}
    for l in d.get("links") or []:
        if isinstance(l, dict) and re.fullmatch(r"[0-9A-Z]{6}", str(l.get("stock_code") or "")) and l.get("segment") and l.get("node") in nids:
            out["links"].append({"stock_code": l["stock_code"], "segment": str(l["segment"]), "node": l["node"]})
        else:
            notes.append(f"연결 형식 오류로 뺌: {str(l)[:60]}")
    for v in d.get("verdicts") or []:
        if isinstance(v, dict) and re.fullmatch(r"[0-9A-Z]{6}", str(v.get("stock_code") or "")) and v.get("segment") and v.get("verdict") in VERDICTS:
            x = {k: v[k] for k in ("stock_code", "segment", "verdict", "reason", "as_of") if k in v}
            x["confidence"] = v.get("confidence") if v.get("confidence") in conf else "중간"
            try:
                x["valid_days"] = max(7, min(int(v.get("valid_days") or REVIEW_STALE_DAYS), 365))
            except (TypeError, ValueError):
                x["valid_days"] = REVIEW_STALE_DAYS
            out["verdicts"].append(x)
        else:
            notes.append(f"판정 형식 오류로 뺌: {str(v)[:60]}")
    return out, notes


def save_review(d):
    """검토 결과를 이전 검토와 합쳐 저장(같은 id·같은 사업부는 새 것으로). 이전 파일은 날짜를 붙여 보관."""
    old = _load(review_path()) or {}
    p = review_path()
    if os.path.exists(p):
        os.makedirs(os.path.join(config.DATA_DIR, "backup"), exist_ok=True)
        shutil.copy2(p, os.path.join(config.DATA_DIR, "backup", f"knowledge_review_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"))

    def merge(a, b, key):
        m = {key(x): x for x in a or []}
        for x in b or []:
            m[key(x)] = x
        return list(m.values())
    new = {"as_of": d["as_of"],
           "themes": merge(old.get("themes"), d["themes"], lambda x: x["id"]),
           "nodes": merge(old.get("nodes"), d["nodes"], lambda x: x["id"]),
           "links": merge(old.get("links"), d["links"], lambda x: (x["stock_code"], x["segment"])),
           "verdicts": merge(old.get("verdicts"), [dict(v, as_of=v.get("as_of") or d["as_of"]) for v in d["verdicts"]],
                             lambda x: (x["stock_code"], x["segment"]))}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    return {k: len(new[k]) for k in ("themes", "nodes", "links", "verdicts")}


# ---------------------------------------------------------------- 자료 기준일·신선도(2026-11-18 사용자 요청)
# (이름, 판정에서의 역할, 주의 기준(일), 오래됨 기준(일), 근거) — 각 자료가 원래 갱신되는 주기의 약 2배를 '주의', 4배 안팎을 '오래됨'으로
FRESH_RULES = {
    "consensus": ("컨센서스", "시장 기대·매출 전망", 14, 30, "7일마다 받음 → 2주 넘으면 주의, 한 달 넘으면 오래됨"),
    "cons_hist": ("컨센서스 추이(3개월 수정)", "시장 기대", 45, 75, "월말 기준으로 갱신 → 한 달 반 넘으면 주의"),
    "mix": ("사업부 구성비", "사업부 목록·비중", 240, 420, "반기 보고서마다 바뀜 → 8개월 넘으면 주의, 14개월 넘으면 오래됨"),
    "profile": ("기업개요(회사 정의)", "주력 연결·숨은 사업", 60, 120, "30일마다 받음 → 두 달 넘으면 주의"),
    "news": ("뉴스", "관심도(점수 아님)", 14, 45, "3일마다 받음 → 2주 넘게 새 기사가 없으면 주의"),
    "knowledge": ("지식 사전(세계 경제 흐름)", "구조 점수", 120, 240, "저절로 바뀌지 않음 → 4개월 넘으면 Claude 검토 권함"),
    "review": ("Claude 검토", "흐름 갱신·판정 의견", 90, 180, "검토 결과는 90일 유효 → 지나면 다시 검토 권함"),
    "flows": ("수급(키움)", "참고 표시(점수 아님)", 5, 14, "거래일마다 받음 → 5일 넘으면 주의, 2주 넘으면 오래됨"),
}


def _age(d, today):
    if not d:
        return None
    s = str(d)[:10]
    try:
        if len(s) == 7:                                  # 'YYYY-MM' → 그달 말일로 봄
            y, m = int(s[:4]), int(s[5:7])
            last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            return (today - last).days
        return (today - dt.date.fromisoformat(s)).days
    except ValueError:
        return None


def freshness(conn, code, P, res, K, today=None):
    """자료 기준일과 상태(ok·warn·old·none). 사업보고서·연간 실적은 '나와 있어야 할 해'와 비교한다."""
    today = today or dt.date.today()
    from .goodco import Data
    D = Data(conn, code, P)
    pf = res.get("profile") or {}
    hist = conn.execute("SELECT MAX(as_of) d FROM consensus_history WHERE stock_code=?", (code,)).fetchone()
    news = conn.execute("SELECT MAX(published) d FROM news WHERE stock_code=?", (code,)).fetchone()
    rv = K.get("review") or {}
    fl = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='kiwoom_flows'").fetchone():
        r = conn.execute("SELECT MAX(dt) d FROM kiwoom_flows WHERE stock_code=?", (code,)).fetchone()
        fl = f"{r['d'][:4]}-{r['d'][4:6]}-{r['d'][6:]}" if r and r["d"] else None
    dates = {"flows": fl, "consensus": D.cons_asof(), "cons_hist": hist["d"] if hist else None, "mix": res.get("mix_as_of"),
             "profile": pf.get("wr_at"), "news": news["d"] if news else None,
             "knowledge": _load(os.path.join(KDIR, "nodes.json")).get("as_of"), "review": rv.get("as_of")}
    out = []
    for k, (nm, role, warn, old, rule) in FRESH_RULES.items():
        if k == "flows" and dates.get(k) is None:          # 키움을 쓰지 않는 PC에서는 칸을 만들지 않음
            continue
        d, age = dates.get(k), _age(dates.get(k), today)
        lvl = "none" if d is None else "old" if age is not None and age > old else "warn" if age is not None and age > warn else "ok"
        hint = {"none": "자료 없음" + (" — 이 기업을 갱신하세요" if k not in ("knowledge", "review") else " — R 탭 아래 Claude 검토로 넣을 수 있음"),
                "old": "오래됨 — 갱신 권함", "warn": "주의 — 곧 갱신 필요", "ok": "최신"}[lvl]
        out.append({"key": k, "name": nm, "role": role, "date": str(d)[:10] if d else None, "age": age, "level": lvl, "rule": rule, "hint": hint})
    # 연간 자료: 기준 연도의 전년 사업보고서가 나와 있어야 함(4월 1일 기준 연도 전환과 같은 규칙)
    want = P["current"] - 1
    fy = max([y for (y, per, m) in D.f if per == "FY" and m == "revenue" and D.f[(y, per, m)][0] is not None and y < P["current"]], default=None)
    for k, nm, role, have in (("annual", "연간 실적(DART)", "사업부 매출 흐름", fy),
                              ("bizreport", "사업보고서 「사업의 내용」", "사업부·주요 제품", pf.get("dart_year"))):
        lvl = "none" if have is None else "ok" if have >= want else "warn" if have == want - 1 else "old"
        out.append({"key": k, "name": nm, "role": role, "date": f"{have}년" if have else None, "age": None, "level": lvl,
                    "rule": f"기준 연도 {P['current']} → {want}년 사업보고서가 있어야 함(3월 말 공시, 4월 1일에 기준 연도가 바뀜)",
                    "hint": {"none": "자료 없음" + (" — DART 키가 있는 PC에서 갱신" if k == "bizreport" else ""), "ok": "최신",
                             "warn": f"{want}년 자료가 아직 없음 — 갱신 권함", "old": "2년 이상 지난 자료"}[lvl]})
    return out


# ---------------------------------------------------------------- 화면 S「노드 사전」(2026-11-18)
def catalog(conn, K=None):
    """노드·흐름 전체 목록 + 노드마다 연결된 회사(매일 남기는 판정 기록의 회사별 마지막 날 기준)"""
    ensure(conn)
    K = K or knowledge()
    links = {}
    last = conn.execute("SELECT MAX(day) d FROM node_verdict_log").fetchone()["d"]
    rows = conn.execute("""SELECT l.stock_code, c.name, l.segment, l.node_id, l.verdict, l.score, l.day FROM node_verdict_log l
                           JOIN (SELECT stock_code, MAX(day) d FROM node_verdict_log GROUP BY stock_code) m
                             ON m.stock_code=l.stock_code AND m.d=l.day
                           LEFT JOIN companies c ON c.stock_code=l.stock_code ORDER BY c.name""").fetchall()
    for r in rows:
        links.setdefault(r["node_id"], []).append({"code": r["stock_code"], "name": r["name"], "segment": r["segment"],
                                                   "verdict": r["verdict"], "score": r["score"], "day": r["day"]})
    themes = []
    for t in K["themes"].values():
        imp = [(nid, n["themes"].get(t["id"])) for nid, n in K["nodes"].items() if n.get("themes", {}).get(t["id"])]
        themes.append(dict(t, n_pos=sum(1 for _, v in imp if v > 0), n_neg=sum(1 for _, v in imp if v < 0)))
    nodes = []
    for n in K["nodes"].values():
        sc, parts = structural(n, K)
        nodes.append({"id": n["id"], "name": n["name"], "group": n.get("group", "기타"), "keywords": n.get("keywords") or [],
                      "ksic": n.get("ksic") or [], "broad": bool(n.get("broad")), "why": n.get("why"), "watch": n.get("watch"),
                      "origin": n.get("origin"), "score": sc,
                      "themes": [{"id": tid, "name": K["themes"][tid]["name"], "impact": v,
                                  "contrib": round(v * float(K["themes"][tid].get("strength", 1.0)), 2)}
                                 for tid, v in sorted((n.get("themes") or {}).items(), key=lambda x: -abs(x[1])) if v and tid in K["themes"]],
                      "links": links.get(n["id"], [])})
    return {"themes": sorted(themes, key=lambda t: -float(t.get("strength", 0))), "nodes": nodes, "log_day": last,
            "n_companies": len({r["stock_code"] for r in rows}),
            "knowledge_as_of": _load(os.path.join(KDIR, "nodes.json")).get("as_of"),
            "review_as_of": (K.get("review") or {}).get("as_of")}


def payload(conn, code, P):
    K = knowledge()
    res = company(conn, code, P, K)
    used = {r["node_id"] for r in res["segments"] if r.get("node_id")}
    tids = {tid for nid in used for tid, v in (K["nodes"][nid].get("themes") or {}).items() if v and tid in K["themes"]}
    rv = K.get("review")
    return dict(res, themes=[K["themes"][t] for t in sorted(tids, key=lambda t: -float(K["themes"][t].get("strength", 0)))],
                node_choices=[{"id": n["id"], "name": n["name"], "group": n.get("group", "기타")} for n in K["nodes"].values()],
                knowledge_as_of=_load(os.path.join(KDIR, "nodes.json")).get("as_of"),
                review={"as_of": rv.get("as_of"), "n": {k: len(rv.get(k) or []) for k in ("themes", "nodes", "links", "verdicts")}} if rv else None,
                freshness=freshness(conn, code, P, res, K), disclaimer=DISCLAIMER)
