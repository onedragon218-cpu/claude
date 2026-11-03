"""LG전자 간극 그래프 샘플과 같은 형식의 회사별 HTML을 만든다.

샘플과 다른 점: 컨센서스(증권사 전망)가 없어 미래 구간은 본질가치 모델의 기본 시나리오로 채운다.
매출·영업이익·시가총액·영업이익률·부채비율은 미래 값을 그리지 않는다.

실행: python3 scripts/sample_page.py hansol meditox  ->  output/<회사>_gap_with_value_20261103.html
"""
import json, os, sys, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model

ROOT = model.ROOT
YEARS = list(range(2016, 2030))
NOW_I = YEARS.index(2026)
LISTED = {"lge": 162884456, "hansol": 23797000, "meditox": 7299000}  # 시가총액 계산용 상장 보통주 수(시세 시총 ÷ 주가)
NOTES = {
    "hansol": [
        "2015년 1월 한솔홀딩스에서 분할 신설된 회사라 2015년부터 자료가 있습니다. 2016년 값의 3년 가중평균에는 2015년만 들어갑니다.",
        "2024년 지배순이익이 적자(−304억)라 순이익·EPS 지수선이 그해에 끊깁니다.",
        "2022·2026년(최근 12개월)은 영업현금흐름이 약해 주주 몫 현금흐름이 음수가 되어 DCF를 계산하지 않았습니다.",
        "2023년 이후 ROE가 요구수익률(8~10%)보다 크게 낮습니다. 그래서 S-RIM 가치가 장부가치보다 낮게 나옵니다. 낮은 주가가 '싸서'인지 '수익성이 낮아서'인지 함께 봐야 합니다.",
    ],
    "meditox": [
        "2018~2021년 주식배당이 있어 주가는 수정주가, 주식 수는 현재(약 700만 주) 기준으로 맞췄습니다.",
        "2020년 영업이익·지배순이익이 적자라 해당 지수선이 그해에 끊깁니다. 2021년 이익에는 소송 관련 일회성 이익이 섞였을 수 있습니다(미확인).",
        "2026년 상반기 전환상환우선주 상환(약 767억 원)으로 지배자본이 4,518억에서 3,744억으로 줄었습니다. 2026년 가치 하락의 큰 원인입니다.",
        "DDM은 배당이 없던 2021·2022년에 0원으로 나와 선이 끊깁니다.",
    ],
    "lge": [],
}


def quant(xs, q):
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def build(key):
    c = model.INP["companies"][key]
    r = model.run(key)
    fin = model.load_fin(key)
    h1 = json.load(open(os.path.join(ROOT, "raw", f"{key}_2026_H1.json")))
    cur, prv = h1["h1_2026"], h1["h1_2025"]
    ttm = {x: fin[2025][x] + cur[x] - prv[x] for x in ("ni_owner", "ocf", "capex_ppe", "capex_int", "revenue", "op")}
    ttm.update(eq_owner=cur["eq_owner"], eq=cur["eq"], liab=cur["liab"])
    fy = dict(fin); fy[2026] = ttm
    sh_v, sh_l = c["shares"], LISTED[key]
    price = {int(k): v for k, v in c["price_close"].items()}
    hist = {row["year"]: row for row in r["rows"]}
    fut = {s: {row["year"]: row for row in rows} for s, rows in r["scenarios"].items()}
    E = 1e8  # 억원

    def roe(y):
        a, b = fy.get(y), fy.get(y - 1)
        if not a: return None
        base = (a["eq_owner"] + b["eq_owner"]) / 2 if b else a["eq_owner"]
        return a["ni_owner"] / base * 100

    S = {k: [] for k in ["매출", "영업이익", "순이익(지배)", "EPS", "시가총액", "영업이익률", "ROE", "부채비율", "주가", "종합", "S-RIM", "DCF", "DDM", "상대가치"]}
    mkey = {"종합": "종합(중앙값)", "S-RIM": "S-RIM", "DCF": "DCF", "DDM": "DDM", "상대가치": "상대가치"}
    for y in YEARS:
        a = fy.get(y)
        if y <= 2026:
            S["매출"].append(round(a["revenue"] / E, 1)); S["영업이익"].append(round(a["op"] / E, 1))
            S["순이익(지배)"].append(round(a["ni_owner"] / E, 1)); S["EPS"].append(round(a["ni_owner"] / sh_v))
            S["시가총액"].append(round(price[y] * sh_l / E)); S["영업이익률"].append(round(a["op"] / a["revenue"] * 100, 2))
            S["ROE"].append(round(roe(y), 2)); S["부채비율"].append(round(a["liab"] / a["eq"] * 100, 1))
            S["주가"].append(price[y])
            for m, k in mkey.items():
                v = hist[y]["value"][k]
                S[m].append(None if v is None else round(v / sh_v))
        else:
            b = fut["기본"][y]
            for k in ("매출", "영업이익", "시가총액", "영업이익률", "부채비율", "주가"): S[k].append(None)
            S["순이익(지배)"].append(round(b["ni"] / E, 1)); S["EPS"].append(round(b["ni"] / sh_v)); S["ROE"].append(round(b["roe"] * 100, 2))
            for m, k in mkey.items():
                v = b["value"][k]
                S[m].append(None if v is None else round(v / sh_v))
    vband = {str(y): {"lo": round(fut["보수"][y]["value"]["종합(중앙값)"] / sh_v), "hi": round(fut["낙관"][y]["value"]["종합(중앙값)"] / sh_v)} for y in (2027, 2028, 2029)}

    gaps = {y: price[y] * sh_l / fy[y]["op"] for y in range(2016, 2026) if fy[y]["op"] > 0}
    gv = list(gaps.values())
    G = {"p25": quant(gv, .25), "med": quant(gv, .5), "p75": quant(gv, .75), "lo": min(gv), "hi": max(gv)}
    gap_now = price[2026] * sh_l / ttm["op"] if ttm["op"] > 0 else None
    return dict(key=key, name=c["name"], code=c["stock_code"], S=S, vband=vband, gaps={str(k): v for k, v in gaps.items()}, G=G,
                gap_now=gap_now, sh_v=sh_v, sh_l=sh_l, k_now=hist[2026]["k"], roe_now=hist[2026]["가중ROE"],
                B_ps=round(ttm["eq_owner"] / sh_v), excl=[y for y in range(2016, 2026) if fy[y]["op"] <= 0])


def won(v): return "—" if v is None else f"{round(v):,}원"


def page(d):
    S, i = d["S"], NOW_I
    px, val = S["주가"][i], S["종합"][i]
    ratio = px / val
    below = [YEARS[j] for j in range(0, NOW_I) if S["종합"][j] and S["주가"][j] < S["종합"][j]]
    b29, lo29, hi29 = S["종합"][-1], d["vband"]["2029"]["lo"], d["vband"]["2029"]["hi"]
    G, gn = d["G"], d["gap_now"]
    gpos = "—" if gn is None else ("25% 아래" if gn < G["p25"] else "25%~가운데" if gn < G["med"] else "가운데~75%" if gn < G["p75"] else "75% 위")
    below_txt = f"{len(below)}번({', '.join(str(y) for y in below)})" if below else "한 번도 없었습니다"
    tag = "t-good" if ratio < 1 else "t-warn"
    meth = [("S-RIM", "자본 + 초과이익의 현재가치"), ("DCF", "주주 몫 현금흐름 할인"), ("DDM", "배당 할인"), ("상대가치", "PER 10배·PBR 1.0배"), ("종합", "네 방법의 중앙값")]
    rows_now = "".join(f"<tr><td>{m}</td><td class='note'>{n}</td><td>{won(S[m][i])}</td><td>{'—' if not S[m][i] else f'{px / S[m][i]:.2f}배'}</td><td>{won(S[m][-1])}</td></tr>" for m, n in meth)
    notes = "".join(f"<li>{t}</li>" for t in NOTES[d["key"]])
    excl = f" 영업이익이 적자였던 {', '.join(map(str, d['excl']))}년은 간극을 계산하지 않았습니다." if d["excl"] else ""
    gap_now_txt = "영업이익이 적자라 계산할 수 없습니다" if gn is None else f"{gn:.1f}배({gpos})"
    data = json.dumps({"series": {"year": YEARS, **S}, "vband": d["vband"], "gap": d["gaps"], "G": G, "gap_now": gn}, ensure_ascii=False)
    css = open(os.path.join(ROOT, "scripts", "sample_style.css"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "scripts", "sample_chart.js"), encoding="utf-8").read()
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{d['name']} 간극 그래프 (본질가치 포함)</title>
<style>{css}</style></head><body><div class="wrap">
<div class="disc">리서치 참고자료이며 투자자문이 아님 · 주가 예측이 아님 · {d['name']}({d['code']}) · LG전자 샘플과 같은 형식</div>
<h1>간극 그래프 — {d['name']}</h1><div class="sub">2026-11-03 · 과거 10년 실적(DART 연결재무제표) + 2026년 최근 12개월 + 2027~2029년 기본 시나리오(컨센서스 아님) · 주가 = 11-03 종가</div>
<div class="card hero"><p class="big">지금 주가 <b>{won(px)}</b>은 종합 본질가치 <b>{won(val)}</b>의 <span class="tag {tag}">{ratio:.2f}배</span>입니다.</p>
<p>2016~2025년 중 주가가 종합가치보다 낮았던 해는 <b>{below_txt}</b>. 2029년 종합가치는 기본 시나리오 {won(b29)}, 범위 {won(lo29)}~{won(hi29)}입니다. 시가총액 ÷ 최근 12개월 영업이익(간극)은 지금 <b>{gap_now_txt}</b>.</p></div>

<div class="card"><h2>한 그래프 — 10년 + 3년</h2>
<div class="note">규모 지표(매출·영업이익·순이익·시총·EPS)는 기준 연도 = 100인 지수(로그 눈금, 왼쪽 축)로, 비율 지표(영업이익률·ROE·부채비율)는 %(오른쪽 축)로 그렸습니다. 주가와 주당 본질가치(종합·S-RIM·DCF·DDM·상대가치)는 모두 <b>기준 연도의 주가 = 100</b>으로 나눠 같은 눈금에 올렸습니다. 그래서 가치선이 주가선보다 아래면 그해 주가가 가치보다 비쌌다는 뜻입니다. 버튼으로 켜고 끌 수 있습니다.</div>
<div class="ctl" id="ctl"></div>
<div class="ctl" id="base"><span class="note" style="align-self:center">기준 연도</span></div>
<div class="chartbox" id="c1"></div>
<p class="note">파란 칸 = 추정 구간(2027~2029 기본 시나리오, 점선) · 청록 띠 = 종합 본질가치의 보수~낙관 범위 · 매출·영업이익·시총·영업이익률·부채비율은 추정하지 않았습니다. 적자인 해는 로그 눈금에 그릴 수 없어 지수선이 끊깁니다. 기준 연도 값이 적자면 그 지표는 사라집니다.</p></div>

<div class="card"><h2>간극 — 시가총액 ÷ 그해 영업이익(배)</h2><div class="chartbox" id="c2"></div>
<p class="note">2016~2025년 간극의 25% {G['p25']:.1f}배 · 가운데 {G['med']:.1f}배 · 75% {G['p75']:.1f}배 (최저 {G['lo']:.1f} · 최고 {G['hi']:.1f}). 지금 시총 ÷ 최근 12개월 영업이익 = {gap_now_txt}.{excl} 시가총액 = 연말 수정주가 × 현재 상장주식 수({d['sh_l']:,}주).</p></div>

<div class="card"><h2>같은 잣대로 본 지금 — 방법별 주당 가치</h2>
<table><tr><th>방법</th><th>뜻</th><th>2026 (최근 12개월)</th><th>주가 ÷ 가치</th><th>2029 기본</th></tr>{rows_now}</table>
<p class="so">요구수익률 {d['k_now'] * 100:.1f}%(국고채 10년 10월 평균 4.21% + 5.5%p) · 가중 ROE {d['roe_now'] * 100:.1f}% · 주당 지배자본 {won(d['B_ps'])}. ROE가 요구수익률보다 낮으면 S-RIM 가치는 주당 자본보다 낮아집니다.</p></div>

<div class="card"><h2>이 회사에서 읽을 때 주의할 점</h2><ul class="flat">{notes}
<li>가정(세 회사 공통): 위험프리미엄 5.5%p, 영구성장률 1.5%, S-RIM 지속계수 0.9, 상대가치 PER 10배·PBR 1.0배. 시나리오는 ROE ±3%p, 요구수익률 ±0.5%p, 배당 ×0.7/×1.2.</li>
<li>계산 코드와 원자료: 저장소 <code>valuation/</code> 폴더(scripts/model.py, scripts/sample_page.py).</li></ul></div>
</div>
<div class="tip" id="tip"></div>
<script>const D={data};const UNIT='억';
{js}</script></body></html>
"""


def main():
    for key in sys.argv[1:] or ["hansol", "meditox"]:
        d = build(key)
        out = os.path.join(ROOT, "output", f"{key}_gap_with_value_20261103.html")
        open(out, "w", encoding="utf-8").write(page(d))
        print(out)


if __name__ == "__main__":
    main()
