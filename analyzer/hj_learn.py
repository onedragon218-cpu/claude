"""화면 T — Claude 판정에서 배우는 모델(앱 안에 내재화, 표준 라이브러리만)

자료: hj_node(origin='claude')의 노드 이름·수요·몫·숫자·바꿀 것 글과 Claude 대표 판정(우호·중립·역풍·씨앗·걸림)
모델: 다항 나이브 베이즈(낱말 단위, 라플라스 평활) — 'Claude가 어떤 낱말을 보고 어느 판정을 주는가'를 셈으로 익힘
쓰임: 앱이 모은 노드 근거 글(노드 이름 + 관련 공시·뉴스 제목)에 적용해 판정 확률을 냄 → hanjang.judge_node의 점수에 더함
검증: 회사 하나씩 빼고 학습해 그 회사 노드를 맞혀 보는 교차 검증(일치율을 화면에 표시)
저장: 데이터 폴더 hj_model.json (작업 폴더 원본을 옮기거나 Claude 조사 결과를 넣으면 다시 학습)
"""
import json
import math
import os
import re
from collections import Counter, defaultdict

from . import config

LABELS = ["우호", "중립", "역풍", "씨앗", "걸림"]
STOP = {"있음", "없음", "이후", "이전", "관련", "확인", "가능", "경우", "때문", "정도", "부분", "모름", "해석", "기준", "이상", "이하",
        "대비", "전년", "올해", "내년", "회사", "사업", "부문", "제품", "매출", "억", "원", "등", "및", "중", "또는", "그리고"}
JOSA = re.compile(r"(으로|에서|에게|까지|부터|이며|이고|으며|하고|하여|해서|에는|에도|과의|와의|이다|입니다|했다|한다|은|는|이|가|을|를|의|에|도|로|과|와)$")


CUES = {   # 방향 낱말 묶음 — 회사가 달라도 같은 뜻으로 쓰이는 말(특정 회사 이름에 끌려가지 않게)
    "#증가": r"증가|늘|확대|증설|상향|성장|호조|회복|개선|흑자|최대|돌파|수혜|강세|램프업|가동률 상승|점유율 상승|1위|독점|국산화",
    "#감소": r"감소|줄|축소|하향|부진|둔화|적자|하락|약세|감산|침체|악화|손실|역성장|감익|공급과잉|가격 인하|단가 인하",
    "#지연": r"지연|늦|미뤄|연기|보류|불확실|불투명|미정",
    "#경쟁": r"경쟁|추격|대체|중국산|저가|점유율 하락|이탈",
    "#씨앗": r"신사업|개발|샘플|시제품|출시 예정|진출|계획|파이프라인|임상|인증 대기|초기|아직|미미|0원|매출 없음",
    "#걸림": r"횡령|배임|소송|분쟁|고소|송치|CB|전환사채|유상증자|증자|희석|감사의견|담보|관리종목|제재|과징금|사망|사고|오너|최대주주",
    "#확정": r"계약|수주|공시|확정|체결|납품 시작|양산",
}
_CUE_RE = {k: re.compile(v) for k, v in CUES.items()}


def cues(text):
    return [k for k, r in _CUE_RE.items() if r.search(text or "")]


def tokens(text):
    out = cues(text)
    for w in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}[A-Za-z0-9]*", text or ""):
        w = JOSA.sub("", w) if re.match(r"[가-힣]", w) else w.upper()
        if len(w) >= 2 and w not in STOP:
            out.append(w)
    return out


def model_path():
    return os.path.join(config.DATA_DIR, "hj_model.json")


def node_text(n):
    return " ".join(str(n.get(k) or "") for k in ("name", "demand", "share", "numbers", "triggers"))


def train_rows(rows):
    """rows: [(라벨, 글) 또는 (라벨, 글, 회사)] → 모델 dict. 3개 회사 이상에 나온 낱말만 씀(회사 이름·고유명사에 끌려가지 않게)"""
    cnt = {l: Counter() for l in LABELS}
    prior = Counter()
    comp = defaultdict(set)
    for row in rows:
        lab, txt = row[0], row[1]
        if lab not in LABELS:
            continue
        prior[lab] += 1
        tk = set(tokens(txt))
        cnt[lab].update(tk)                        # 글마다 한 번(문서 빈도) — 긴 글이 과하게 세지지 않게
        for t in tk:
            comp[t].add(row[2] if len(row) > 2 else id(row))
    vocab = {w for w in comp if w.startswith("#") or len(comp[w]) >= 3}
    return {"labels": LABELS, "prior": dict(prior), "counts": {l: {w: c for w, c in cnt[l].items() if w in vocab} for l in LABELS},
            "n": sum(prior.values()), "vocab": len(vocab)}


def predict(model, text):
    """반환: {라벨: 확률}, 근거 낱말 [(낱말, 가장 기운 라벨)]"""
    if not model or not model.get("n"):
        return None, []
    toks = [t for t in set(tokens(text)) if any(t in model["counts"][l] for l in LABELS)]
    V = max(model["vocab"], 1)
    logp = {}
    for l in LABELS:
        nl = model["prior"].get(l, 0)
        if not nl:
            continue
        lp = math.log(nl / model["n"])
        for t in toks:
            lp += math.log((model["counts"][l].get(t, 0) + 1) / (nl + 2))      # 베르누이식(문서에 낱말이 있나)
        logp[l] = lp
    if not logp:
        return None, []
    m = max(logp.values())
    z = sum(math.exp(v - m) for v in logp.values())
    probs = {l: math.exp(v - m) / z for l, v in logp.items()}
    why = sorted(((t, max(LABELS, key=lambda l: (model["counts"][l].get(t, 0) + 1) / (model["prior"].get(l, 0) + 2)))
                  for t in toks), key=lambda x: -sum(model["counts"][l].get(x[0], 0) for l in LABELS))[:6]
    return probs, why


def lexicon(model, top=12):
    """판정마다 Claude 글에서 유난히 많이 나온 낱말(로그 오즈) — 화면에 '앱이 배운 낱말'로 보여 줌"""
    if not model or not model.get("n"):
        return {}
    out = {}
    for l in LABELS:
        nl = model["prior"].get(l, 0)
        if not nl:
            continue
        no = model["n"] - nl
        sc = []
        for w, c in model["counts"][l].items():
            co = sum(model["counts"][k].get(w, 0) for k in LABELS if k != l)
            if c >= 2:
                sc.append((w, math.log((c + 1) / (nl + 2)) - math.log((co + 1) / (no + 2))))
        out[l] = [w for w, s in sorted(sc, key=lambda x: -x[1])[:top]]
    return out


def train(conn):
    """DB의 Claude 노드로 학습해 저장 + 회사 빼고 맞혀 보기 교차 검증. 반환: 요약"""
    rows = conn.execute("SELECT code, rep, name, demand, share, numbers, triggers FROM hj_node WHERE origin='claude' AND rep IS NOT NULL").fetchall()
    data = [(r["code"], r["rep"], node_text(dict(r))) for r in rows]
    model = train_rows([(l, t, c) for c, l, t in data])
    by = defaultdict(list)
    for c, l, t in data:
        by[c].append((l, t))
    ok = tot = 0
    conf = Counter()
    for c in by:                                   # 회사 하나씩 빼고 학습 → 그 회사 노드 맞히기
        m = train_rows([(l, t, cc) for cc, l, t in data if cc != c])
        for l, t in by[c]:
            p, _ = predict(m, t)
            if p:
                g = max(p, key=p.get)
                ok += g == l
                tot += 1
                conf[(l, g)] += 1
    model["cv"] = {"accuracy": ok / tot if tot else None, "n": tot, "companies": len(by),
                   "confusion": {f"{a}→{b}": n for (a, b), n in conf.most_common(12)}}
    model["lexicon"] = lexicon(model)
    with open(model_path(), "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False)
    return {"nodes": model["n"], "vocab": model["vocab"], "cv": model["cv"]}


_CACHE = {}


def load():
    p = model_path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    if _CACHE.get("mt") != mt:
        with open(p, encoding="utf-8") as f:
            _CACHE.update(mt=mt, model=json.load(f))
    return _CACHE["model"]
