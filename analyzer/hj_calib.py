"""화면 T — 앱 참고 판정을 Claude 판정에 가깝게 맞추기: ① 차이 진단 ② Claude 판정으로 배우기 ③ 정직한 검증 (2026-11-24, 검증만 — 화면 판정은 바꾸지 않음)

- 정답: Claude 현재 판정(노드마다 우호·중립·역풍·씨앗·걸림). '유효한 정답만' 보기에서는 조사 뒤 실적 공시·바꿀 것 낱말 적중이 있는(낡았을 수 있는) 판정을 뺀다.
- 재료(앱이 스스로 모을 수 있는 것만): 노드 종류·주력 여부·매출 비중·세계 흐름 점수·회사 숫자(주력만: 분기 누적 매출 성장, 이익률 변화, 다음 해 컨센 성장, 3개월 수정)
  ·노드 관련 공시/뉴스 좋음·나쁨 건수·바꿀 것 낱말 적중·낱말 모델 확률(앱이 모은 제목 글에 적용 — Claude가 쓴 글은 쓰지 않음).
- 모델: 다항 로지스틱 회귀(표준 라이브러리, L2). 낱말 모델도 검증 때는 맞힐 회사를 뺀 나머지로만 다시 배움(답 보고 풀기 방지).
- 검증: 회사 단위로 5묶음 나눠 4묶음으로 배우고 1묶음 맞히기(같은 회사 노드가 배우기·맞히기에 함께 들어가지 않게). 비교: 지금 규칙 판정, 가장 흔한 판정만 찍기, 낱말 모델만.
- 결과는 데이터 폴더 hj_calib.json에 저장(화면 표시용). 적용은 사용자가 숫자를 보고 정한다.
"""
import json
import math
import re
import os
import random
from collections import Counter, defaultdict

from . import config, db

LABELS = ["우호", "중립", "역풍", "씨앗", "걸림"]
SEED_WORDS = re.compile(r"신사업|신규\s*사업|새\s*사업|개발|파이프라인|임상|샘플|시제품|진출|차세대|준비|인증|계획")
KINDS = ["사업부", "씨앗", "원료", "고객", "사람"]
FEAT_NAMES = {}      # 재료 이름(한글) — build_rows에서 채움


def result_path():
    return os.path.join(config.DATA_DIR, "hj_calib.json")


def _f(v, d=0.0):
    return d if v is None else float(v)


def seg_link(conn, code):
    """노드 이름 → DART 사업부(주요제품 매출구성) 비중·1년 변화. 반환: 함수(이름) → (비중%, 1년 변화 %p 또는 None) 또는 None"""
    import datetime as dt
    from . import segments
    hist = segments.mix_history(conn, code)
    if not hist:
        return lambda name: None
    days = sorted({h["as_of"] for h in hist})
    last = days[-1]
    snap = {h["name"]: h["pct"] for h in hist if h["as_of"] == last}

    def _d(a):
        try:
            return dt.date.fromisoformat(a + "-01" if len(a) == 7 else a[:10])
        except ValueError:
            return None
    ld = _d(last)
    old = next((d for d in reversed(days) if ld and _d(d) and (ld - _d(d)).days >= 300), None)
    snap_old = {h["name"]: h["pct"] for h in hist if h["as_of"] == old} if old else {}
    kw = {n: set(segments.auto_keywords(n)) | set(re.findall(r"[가-힣A-Za-z]{2,}", n)) for n in snap}

    def link(name):
        words = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"\(.*?\)", " ", name or ""))) - {"사업", "부문", "제품", "기타", "수요", "판매"}
        best = None
        for seg, ks in kw.items():
            hit = sum(len(w) for w in words for k in ks if len(k) >= 2 and (k in w or w in k))
            if hit and (best is None or hit > best[1]):
                best = (seg, hit)
        if not best:
            return None
        seg = best[0]
        ch = snap[seg] - snap_old[seg] if seg in snap_old and snap[seg] is not None and snap_old[seg] is not None else None
        return snap[seg], ch
    return link


def features(f, comp):
    """노드 판단 자료(node_facts 한 줄) + 회사 숫자 → {재료: 값}"""
    main = 1.0 if f.get("main") else 0.0
    x = {"주력 노드": main, "매출 비중": _f(f.get("share_pct")) / 100, "매출 비중 모름": 1.0 if f.get("share_pct") is None else 0.0,
         "세계 흐름 점수": _f(f.get("structural")), "세계 흐름 연결 없음": 1.0 if f.get("structural") is None else 0.0,
         "공시 좋음": math.log1p(_f(f.get("disc_pos"))), "공시 나쁨": math.log1p(_f(f.get("disc_neg"))),
         "뉴스 좋음": math.log1p(_f(f.get("news_pos"))), "뉴스 나쁨": math.log1p(_f(f.get("news_neg"))),
         "관련 뉴스 수": math.log1p(_f(f.get("n_news"))), "바꿀 것 낱말 적중": 1.0 if f.get("trigger_hit") else 0.0}
    for k in KINDS:                      # 종류는 노드 이름으로만 추정(Claude 판정에서 온 종류를 쓰면 답을 보고 푸는 셈)
        x[f"종류={k}"] = 1.0 if f.get("kind_i") == k else 0.0
    x["이름에 새 사업 낱말"] = 1.0 if SEED_WORDS.search(f.get("name") or "") else 0.0
    sg = f.get("seg")                     # DART 사업부와 연결(주력 아닌 노드에도 숫자)
    x["사업부 연결 없음"] = 0.0 if sg else 1.0
    x["연결된 사업부 비중"] = (_f(sg[0]) / 100) if sg else 0.0
    x["사업부 비중 1년 변화"] = max(-1.0, min(1.0, sg[1] / 10)) if sg and sg[1] is not None else 0.0
    x["비중 변화 모름"] = 0.0 if sg and sg[1] is not None else 1.0
    q = comp.get("q_opm")
    x["(주력) 분기 누적 매출 성장"] = main * max(-1.0, min(1.0, _f(comp.get("q_growth")) / 100))
    x["(주력) 이익률 변화"] = main * max(-1.0, min(1.0, ((q[0] - q[1]) / 10) if q else 0.0))
    x["(주력) 다음 해 컨센 성장"] = main * max(-1.0, min(1.0, _f(comp.get("cons_growth")) / 100))
    x["(주력) 3개월 수정"] = main * max(-1.0, min(1.0, _f(comp.get("rev3")) / 20))
    return x


def build_rows(conn, P, progress=None):
    """Claude 조사가 있고 앱 DB에 숫자가 있는 회사의 노드마다 (회사, 재료, 글, Claude 판정, 지금 규칙 판정, 유효한 정답?)"""
    from . import hanjang
    hanjang.ensure(conn)
    codes = [r["code"] for r in conn.execute("SELECT h.code FROM hj_company h WHERE h.origin='claude' AND EXISTS "
                                             "(SELECT 1 FROM facts f WHERE f.stock_code=h.code) ORDER BY h.code")]
    rows = []
    for i, code in enumerate(codes):
        if progress:
            progress(i, len(codes))
        cl = hanjang.load(conn, code, "claude")
        app = hanjang.load(conn, code, "app")
        try:
            F = hanjang.node_facts(conn, code, P, cl, app, cl["company"]["asof"], with_text=True)
        except Exception:      # noqa: BLE001  한 회사가 실패해도 나머지로
            continue
        trig = " ".join(n.get("triggers") or "" for n in cl["nodes"])
        fr = hanjang.claude_fresh(conn, code, cl["company"]["asof"], trig)
        link = seg_link(conn, code)
        segs = {n["node_id"]: link(n["name"]) for n in cl["nodes"]}
        share = lambda n: n.get("share_pct") if n.get("share_pct") is not None else (segs[n["node_id"]] or (None,))[0]
        biz = [n for n in cl["nodes"] if hanjang.guess_kind(n["name"]) == "사업부"]      # 주력도 이름 기준 종류로(Claude 종류 안 씀)
        main_id = max(biz, key=lambda n: (share(n) or 0, -n["node_id"]))["node_id"] if biz else None
        for n in cl["nodes"]:
            f = F["nodes"].get(n["node_id"])
            lab = n.get("rep")
            if not f or lab not in LABELS:
                continue
            f = dict(f, name=n["name"], kind_i=hanjang.guess_kind(n["name"]), main=n["node_id"] == main_id,
                     seg=segs[n["node_id"]], share_pct=share(n))
            rows.append({"code": code, "x": features(f, F["company"]), "text": f.get("text") or n["name"], "y": lab,
                         "rule": f.get("app_judgment"), "kind": f["kind_i"], "main": bool(f.get("main")),
                         "valid": fr["state"] != "old" and not f.get("trigger_hit"), "name": n["name"]})
    return rows


# ---------------------------------------------------------------- 다항 로지스틱 회귀(표준 라이브러리)
def _softmax(z):
    m = max(z)
    e = [math.exp(v - m) for v in z]
    s = sum(e)
    return [v / s for v in e]


def fit(X, Y, names, l2=0.05, iters=400, lr=0.3):
    """X: [{재료: 값}], Y: [라벨]. 반환: 모델 dict(재료 평균·표준편차로 맞춘 뒤 배움)"""
    labs = [l for l in LABELS if l in set(Y)]
    mu = {k: sum(x.get(k, 0.0) for x in X) / len(X) for k in names}
    sd = {k: (math.sqrt(sum((x.get(k, 0.0) - mu[k]) ** 2 for x in X) / len(X)) or 1.0) for k in names}
    Z = [[(x.get(k, 0.0) - mu[k]) / sd[k] for k in names] + [1.0] for x in X]
    W = [[0.0] * (len(names) + 1) for _ in labs]
    cnt = Counter(Y)
    cw = {l: len(Y) / (len(labs) * cnt[l]) for l in labs}          # 드문 판정(씨앗·걸림)도 묻히지 않게 무게
    yi = [labs.index(y) for y in Y]
    n = len(Z)
    for _ in range(iters):
        G = [[0.0] * len(Z[0]) for _ in labs]
        for z, t, y in zip(Z, yi, Y):
            p = _softmax([sum(a * b for a, b in zip(w, z)) for w in W])
            for c in range(len(labs)):
                g = (p[c] - (1.0 if c == t else 0.0)) * cw[y]
                if g:
                    Gc = G[c]
                    for j, v in enumerate(z):
                        Gc[j] += g * v
        for c in range(len(labs)):
            for j in range(len(Z[0])):
                reg = l2 * W[c][j] if j < len(names) else 0.0
                W[c][j] -= lr * (G[c][j] / n + reg)
    return {"labels": labs, "names": names, "mu": mu, "sd": sd, "W": W}


def predict(m, x):
    z = [(x.get(k, 0.0) - m["mu"][k]) / m["sd"][k] for k in m["names"]] + [1.0]
    p = _softmax([sum(a * b for a, b in zip(w, z)) for w in m["W"]])
    return dict(zip(m["labels"], p))


def _with_nb(train, test):
    """낱말 모델을 train 회사들의 Claude 판정으로만 배워 train·test 글에 적용 → 재료에 '낱말 모델: 판정' 확률 추가"""
    from . import hj_learn
    nb = hj_learn.train_rows([(r["y"], r["text"], r["code"]) for r in train])
    out = []
    for r in train + test:
        p, _ = hj_learn.predict(nb, r["text"])
        x = dict(r["x"])
        for l in LABELS:
            x[f"낱말 모델: {l}"] = (p or {}).get(l, 0.2)
        out.append(x)
    return out[:len(train)], out[len(train):], nb


def _folds(rows, k=5, seed=7):
    codes = sorted({r["code"] for r in rows})
    random.Random(seed).shuffle(codes)
    g = {c: i % k for i, c in enumerate(codes)}
    return [([r for r in rows if g[r["code"]] != f], [r for r in rows if g[r["code"]] == f]) for f in range(k)]


def evaluate(rows, k=5):
    """회사 단위 k묶음 검증. 반환: 판정 방법마다 일치율·혼동표·종류별 일치율·확신 상위 절반 일치율"""
    names = sorted({n for r in rows for n in r["x"]}) + [f"낱말 모델: {l}" for l in LABELS]
    pred_model, pred_nb, conf = {}, {}, {}
    for train, test in _folds(rows, k):
        if not train or not test:
            continue
        Xtr, Xte, _ = _with_nb(train, test)
        m = fit(Xtr, [r["y"] for r in train], names)
        for r, x in zip(test, Xte):
            p = predict(m, x)
            key = id(r)
            pred_model[key] = max(p, key=p.get)
            conf[key] = max(p.values())
            pred_nb[key] = max(LABELS, key=lambda l: x[f"낱말 모델: {l}"])
    major = Counter(r["y"] for r in rows).most_common(1)[0][0]

    def score(get, subset):
        sub = [r for r in subset if get(r) is not None]
        ok = sum(1 for r in sub if get(r) == r["y"])
        near = {("우호", "중립"), ("중립", "우호"), ("역풍", "중립"), ("중립", "역풍")}
        close = sum(1 for r in sub if get(r) == r["y"] or (r["y"], get(r)) in near)
        return {"n": len(sub), "same": ok, "rate": ok / len(sub) if sub else None, "close_rate": close / len(sub) if sub else None}

    methods = {"지금 규칙 판정": lambda r: r["rule"], "가장 흔한 판정만(" + major + ")": lambda r: major,
               "낱말 모델만": lambda r: pred_nb.get(id(r)), "새 모델(배운 무게)": lambda r: pred_model.get(id(r))}
    valid = [r for r in rows if r["valid"]]
    out = {"n_rows": len(rows), "n_valid": len(valid), "n_companies": len({r["code"] for r in rows}), "folds": k,
           "labels": dict(Counter(r["y"] for r in rows)), "methods": {}, "valid_methods": {}}
    for name, get in methods.items():
        out["methods"][name] = score(get, rows)
        out["valid_methods"][name] = score(get, valid)
    for name, get in (("지금 규칙 판정", methods["지금 규칙 판정"]), ("새 모델(배운 무게)", methods["새 모델(배운 무게)"])):
        cm = Counter((r["y"], get(r)) for r in rows if get(r) is not None)
        out.setdefault("confusion", {})[name] = {f"{a}→{b}": n for (a, b), n in cm.most_common(15) if a != b}
        out.setdefault("by_kind", {})[name] = {kd: score(get, [r for r in rows if r["kind"] == kd]) for kd in KINDS if any(r["kind"] == kd for r in rows)}
        out.setdefault("by_main", {})[name] = {lab: score(get, [r for r in rows if r["main"] == mv]) for lab, mv in (("주력 노드", True), ("나머지 노드", False))}
    ranked = sorted([r for r in rows if id(r) in conf], key=lambda r: -conf[id(r)])
    for frac in (0.25, 0.5):
        top = ranked[:max(1, int(len(ranked) * frac))]
        out.setdefault("confident", {})[f"확신 상위 {int(frac * 100)}%"] = score(methods["새 모델(배운 무게)"], top)
    return out


def importance(rows):
    """전체로 한 번 배워 판정마다 무게가 큰 재료(설명용)"""
    names = sorted({n for r in rows for n in r["x"]}) + [f"낱말 모델: {l}" for l in LABELS]
    X, _, _ = _with_nb(rows, [])
    m = fit(X, [r["y"] for r in rows], names)
    out = {}
    for c, lab in enumerate(m["labels"]):
        w = sorted(zip(names, m["W"][c][:len(names)]), key=lambda t: -abs(t[1]))[:6]
        out[lab] = [[k, round(v, 2)] for k, v in w]
    return out


def run(conn, P, progress=None):
    rows = build_rows(conn, P, progress)
    if len(rows) < 30 or len({r["code"] for r in rows}) < 10:
        res = {"error": f"자료가 부족합니다(노드 {len(rows)}개, 회사 {len({r['code'] for r in rows})}곳 — 회사 10곳·노드 30개 이상 필요)"}
    else:
        res = evaluate(rows)
        res["importance"] = importance(rows)
        mism = [r for r in rows if r["rule"] and r["rule"] != r["y"]]
        res["mismatch_sample"] = [{"code": r["code"], "node": r["name"][:40], "claude": r["y"], "rule": r["rule"], "kind": r["kind"],
                                   "main": r["main"], "valid": r["valid"]} for r in mism[:60]]
    res["at"] = db.now()
    with open(result_path(), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    return res


def load():
    try:
        with open(result_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
