"""사업부 분해 · 사업부별 뉴스 흐름 · 사업부별 경쟁사 비교 (화면 L)

- 사업부 구성: WiseReport '주요제품 매출구성'(사업보고서 주요제품 매출 비중을 옮긴 것). 시점별로 쌓는다.
- 사업부 키워드: 사용자 지정(DATA_DIR/segment_keywords.json) > 기본값(analyzer/seed/<코드>/segment_keywords.json)
  > 자동(사업부 이름에서 '부문·제품·기타' 같은 일반어를 뺀 낱말).
- 뉴스 흐름: 제목에 키워드가 들어간 기사를 그 사업부로 묶고, 달마다 비슷한 제목을 한 묶음으로 모아
  많이 보도된 묶음부터 대표 제목 3개를 보여 준다(문장을 새로 지어내지 않는 '제목 기반 요약').
- 경쟁사: DB에 자료가 있는 기업 가운데 사업부 키워드가 겹치는 기업(사업부 일치) → 같은 DART 업종코드(회사 전체 기준) 순.
  DB에 기업이 많아질수록 후보가 정확해진다.
"""
import json
import os
import re

from . import config, db

GENERIC = {"제품", "상품", "기타", "용역", "수출", "내수", "매출", "부문", "사업", "서비스", "판매", "등", "외", "및",
           "기타부문", "제품매출", "상품매출", "용역매출", "임대", "수수료"}
# 경쟁사 찾기에서 쓰지 않는 낱말(지역·판매 방식은 사업 내용이 아님)
PEER_STOP = {"수출", "내수", "해외", "국내", "글로벌", "중국", "中", "美", "미국", "유럽", "중동", "중남미", "일본", "태국",
             "파나마", "판매", "품목허가", "국내 허가", "국내 품목허가", "공급계약", "공급 계약", "약국", "IP", "AI", "OTT"}
SEED = os.path.join(os.path.dirname(__file__), "seed")


def user_file():
    return os.path.join(config.DATA_DIR, "segment_keywords.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def auto_keywords(name):
    """'인쇄용지, 특수지' → ['인쇄용지', '특수지'], '건설부문' → ['건설'], '제품(수출)' → []"""
    name = re.sub(r"\(.*?\)", " ", name)
    out = []
    for tok in re.split(r"[,·/\s]+|및", name):
        tok = re.sub(r"(부문|사업부|사업|등)$", "", tok.strip())
        if len(tok) >= 2 and tok not in GENERIC and tok not in out:
            out.append(tok)
    return out


def keyword_config(code):
    """반환: {'segments': {사업부명: [키워드]}, 'topics': {주제명: [키워드]}, 'origin': {...}, 'note': str}"""
    seed = _load(os.path.join(SEED, code, "segment_keywords.json"))
    user = _load(user_file()).get(code) or {}
    out = {"segments": {}, "topics": {}, "origin": {}, "note": seed.get("_근거")}
    for src, d in (("기본값", seed), ("사용자", user)):
        for k in ("segments", "topics"):
            for name, kws in (d.get(k) or {}).items():
                if k == "topics" and src == "사용자" and not kws:     # 사용자가 주제 키워드를 비우면 주제를 지운다
                    out[k].pop(name, None)
                    continue
                out[k][name] = [x for x in kws if x]
                out["origin"][f"{k}:{name}"] = src
    return out


def save_user_keywords(code, segs, topics):
    d = _load(user_file())
    d[code] = {"segments": segs or {}, "topics": topics or {}}
    os.makedirs(os.path.dirname(user_file()), exist_ok=True)
    with open(user_file(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def latest_mix(conn, code):
    r = conn.execute("SELECT MAX(as_of) d FROM segments WHERE stock_code=? AND kind='mix'", (code,)).fetchone()
    if not r or not r["d"]:
        return None, []
    rows = conn.execute("SELECT name, pct, source, source_ref, collected_at FROM segments WHERE stock_code=? AND kind='mix' "
                        "AND as_of=? ORDER BY pct DESC", (code, r["d"])).fetchall()
    return r["d"], [dict(x) for x in rows]


def mix_history(conn, code):
    return [dict(x) for x in conn.execute("SELECT as_of, name, pct FROM segments WHERE stock_code=? AND kind='mix' "
                                          "ORDER BY as_of, pct DESC", (code,))]


def groups(conn, code):
    """사업부·주제별 키워드 목록. [{name, kind: '사업부'|'주제', pct, keywords, origin}]"""
    as_of, mix = latest_mix(conn, code)
    kc = keyword_config(code)
    out = []
    for m in mix:
        kws = kc["segments"].get(m["name"])
        origin = kc["origin"].get(f"segments:{m['name']}") or "자동"
        out.append({"name": m["name"], "kind": "사업부", "pct": m["pct"],
                    "keywords": kws if kws is not None else auto_keywords(m["name"]), "origin": origin})
    for name, kws in kc["segments"].items():        # 매출구성에 없는 사업부를 직접 지정한 경우
        if not any(g["name"] == name for g in out):
            out.append({"name": name, "kind": "사업부", "pct": None, "keywords": kws, "origin": kc["origin"][f"segments:{name}"]})
    for name, kws in kc["topics"].items():
        out.append({"name": name, "kind": "주제", "pct": None, "keywords": kws, "origin": kc["origin"][f"topics:{name}"]})
    return as_of, out, kc.get("note")


# ---- 뉴스 묶기 ----
def _tokens(title, drop):
    t = re.sub(r"\[[^\]]*\]|【[^】]*】|<[^>]*>", " ", title)
    toks = {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t)}
    return {w for w in toks if w not in drop}


def cluster(items, drop=()):
    """비슷한 제목을 한 묶음으로. items는 날짜 오름차순. 반환: [{title, link, published, press, n, items}]"""
    out = []
    for it in items:
        tk = _tokens(it["title"], set(drop))
        best = None
        for c in out:
            inter = len(tk & c["_tk0"])
            small = min(len(tk), len(c["_tk0"]))
            if small and inter >= 2 and inter / small >= 0.5:
                best = c
                break
        if best:
            best["n"] += 1
            best["items"].append(it)
            best["_tk"] |= tk
        else:
            out.append({"title": it["title"], "link": it["link"], "published": it["published"], "press": it.get("press"),
                        "n": 1, "items": [it], "_tk": set(tk), "_tk0": set(tk)})
    for c in out:
        c.pop("_tk"); c.pop("_tk0")
    return out


def _match(text, kws):
    return [k for k in kws if k and k.lower() in text.lower()]


def news_flow(conn, code, name, since, top=3):
    """사업부·주제별 월별 기사 수와 대표 제목. 키워드에 걸리지 않는 기사는 '회사 전반'."""
    from .collectors import news as _news
    as_of, gs, _ = groups(conn, code)
    rows = [dict(r) for r in conn.execute("SELECT published, title, link, press, description, source FROM news "
                                          "WHERE stock_code=? AND published>=? ORDER BY published", (code, since))]
    for r in rows:
        if not r["link"]:
            r["link"] = _news.search_link(r["title"])
    drop = {name} | {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", name or "")}
    buckets = {g["name"]: [] for g in gs}
    buckets["회사 전반"] = []
    for r in rows:
        hit = False
        for g in gs:
            m = _match(r["title"], g["keywords"])
            if m:
                buckets[g["name"]].append(dict(r, matched=m))
                hit = True
        if not hit:
            buckets["회사 전반"].append(r)
    flow = {}
    for gname, items in buckets.items():
        months = {}
        for it in items:
            months.setdefault(it["published"][:7], []).append(it)
        flow[gname] = {"count": len(items), "months": []}
        for mon in sorted(months, reverse=True):
            cl = sorted(cluster(months[mon], drop), key=lambda c: (-c["n"], c["published"]))
            flow[gname]["months"].append({"month": mon, "count": len(months[mon]), "top": cl[:top], "more": cl[top:]})
    srcs = sorted({r["source"] for r in rows if r["source"]})
    return {"groups": gs, "flow": flow, "total": len(rows), "sources": srcs,
            "first": rows[0]["published"] if rows else None, "last": rows[-1]["published"] if rows else None}


def events(conn, code, since):
    return [dict(r) for r in conn.execute("SELECT rcept_dt, title, kind, link, rcept_no FROM events WHERE stock_code=? "
                                          "AND rcept_dt>=? ORDER BY rcept_dt DESC", (code, since.replace("-", "")))]


def summaries(code):
    """Claude가 정리한 제목 기반 요약(샘플 기업만, analyzer/seed/<코드>/news_summary.json)"""
    return _load(os.path.join(SEED, code, "news_summary.json")) or None


# ---- 경쟁사 ----
def _fact(conn, code, y, k):
    r = db.current_fact(conn, code, y, k)
    return None if r is None else r["value"]


def company_metrics(conn, code, y):
    rev, rev0 = _fact(conn, code, y, "revenue"), _fact(conn, code, y - 1, "revenue")
    return {"year": y, "revenue": rev, "rev_growth": (rev / rev0 - 1) * 100 if rev and rev0 and rev0 > 0 else None,
            "op_margin": _fact(conn, code, y, "op_margin"), "roe": _fact(conn, code, y, "roe"),
            "debt_ratio": _fact(conn, code, y, "debt_ratio"), "market_cap": _fact(conn, code, y, "market_cap"),
            "pbr": (lambda p, b: p / b if p and b and b > 0 else None)(_fact(conn, code, y, "price_close"), _fact(conn, code, y, "bps")),
            "per": (lambda p, e: p / e if p and e and e > 0 else None)(_fact(conn, code, y, "price_close"), _fact(conn, code, y, "eps"))}


def peers(conn, code, P, gs):
    """사업부마다 경쟁사 후보와 비교 지표. 반환: {'by_segment': {사업부: [후보]}, 'pool': n, 'self': 지표}"""
    y = P["current"] - 1
    me = conn.execute("SELECT induty_code FROM companies WHERE stock_code=?", (code,)).fetchone()
    my_ind = me["induty_code"] if me else None
    others = [dict(r) for r in conn.execute("SELECT c.stock_code, c.name, c.induty_code FROM companies c WHERE c.stock_code!=? "
                                            "AND EXISTS (SELECT 1 FROM facts f WHERE f.stock_code=c.stock_code)", (code,))]
    info = {}
    for o in others:
        _, ogs, _ = groups(conn, o["stock_code"])
        info[o["stock_code"]] = (o, ogs)
    out = {}
    for g in gs:
        if g["kind"] != "사업부":
            continue
        cands = []
        for oc, (o, ogs) in info.items():
            hit = []
            for og in ogs:
                if og["kind"] != "사업부":
                    continue
                mine = [k for k in g["keywords"] if len(k) >= 2 and k not in PEER_STOP]
                theirs = [x for x in og["keywords"] + auto_keywords(og["name"]) if len(x) >= 2 and x not in PEER_STOP]
                common = [k for k in mine if any(k == x or k in x or x in k for x in theirs)]
                if common:
                    hit.append((og, common))
            if hit:
                og, common = max(hit, key=lambda h: h[0]["pct"] or 0)
                pct = "" if og["pct"] is None else "(%.1f%%)" % og["pct"]
                cands.append({"code": oc, "name": o["name"], "match": "사업부 일치",
                              "why": "상대 사업부 '%s'%s · 겹치는 낱말 %s" % (og["name"], pct, ", ".join(common)),
                              "peer_seg_pct": og["pct"], **company_metrics(conn, oc, y)})
            elif my_ind and o["induty_code"] and o["induty_code"][:3] == my_ind[:3]:
                cands.append({"code": oc, "name": o["name"], "match": "같은 업종",
                              "why": f"DART 업종코드 앞 3자리 {my_ind[:3]} 같음(회사 전체 기준이라 사업부와 다를 수 있음)",
                              "peer_seg_pct": None, **company_metrics(conn, oc, y)})
        cands.sort(key=lambda c: (c["match"] != "사업부 일치", -(c["revenue"] or 0)))
        out[g["name"]] = cands
    return {"by_segment": out, "pool": len(others) + 1, "self": dict(company_metrics(conn, code, y), code=code), "year": y}


def payload(conn, code, name, P):
    import datetime as dt
    since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    as_of, gs, note = groups(conn, code)
    _, mix = latest_mix(conn, code)
    return {"as_of": as_of, "mix": mix, "mix_history": mix_history(conn, code), "groups": gs, "keyword_note": note,
            "news": news_flow(conn, code, name, since), "events": events(conn, code, since), "since": since,
            "summary": summaries(code), "peers": peers(conn, code, P, gs),
            "naver_key": bool(__import__("analyzer.collectors.news", fromlist=["naver_key"]).naver_key())}
