"""본질가치 4가지 방법(S-RIM, DCF, DDM, 상대가치)을 연도별로 계산한다.

과거(2016~2025): 그해 말 기준. 그해 사업보고서 숫자와 그해 금리만 쓴다(뒤늦게 알게 된 정보 배제).
올해(2026):    2026-11-03 기준. 재무는 최근 12개월(2025년 + 2026년 상반기 − 2025년 상반기), 자본은 2026-06-30.
미래(2027~2029): 2026 값을 출발점으로 보수·기본·낙관 3개 시나리오를 굴려 계산한다.

실행: python3 scripts/model.py  ->  output/results.json, output/results.csv
"""
import json, glob, os, csv, statistics

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INP = json.load(open(os.path.join(ROOT, "inputs.json")))
A = INP["가정"]
ERP, G, W = A["위험프리미엄_ERP"], A["영구성장률_g"], A["S-RIM_초과이익지속계수_w"]
PER_REF, PBR_REF = A["상대가치_기준PER"], A["상대가치_기준PBR"]
RF = {int(k): v / 100 for k, v in INP["국고채10년_연평균"].items() if not k.startswith("_")}
PAST = list(range(2016, 2026))
NOW = 2026
FUTURE = [2027, 2028, 2029]

SCEN = {  # 미래 시나리오: ROE 가감(%p), 요구수익률 가감(%p), 배당 배수
    "보수": {"roe": -0.03, "k": +0.005, "div": 0.7},
    "기본": {"roe": 0.0, "k": 0.0, "div": 1.0},
    "낙관": {"roe": +0.03, "k": -0.005, "div": 1.2},
}


def load_fin(key):
    """연도별 재무. 각 연도는 그해 자기 사업보고서 값을 우선(당시 공시 기준)."""
    m = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "raw", f"{key}_20[0-9][0-9].json"))):
        own = int(os.path.basename(f)[-9:-5])
        for y, r in json.load(open(f))["years"].items():
            y = int(y)
            if not r:
                continue
            if y not in m or y == own:
                m[y] = dict(r)
    return m


def wavg3(vals):
    """최근값 3 : 직전 2 : 전전 1 가중평균. None은 건너뜀."""
    pairs = [(v, w) for v, w in zip(vals, (3, 2, 1)) if v is not None]
    if not pairs:
        return None
    return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)


def fcfe(r):
    """주주잉여현금흐름(간이) = (영업현금흐름 − 유형·무형자산 취득) × 지배지분 비율."""
    if r.get("ocf") is None:
        return None
    f = r["ocf"] - r.get("capex_ppe", 0) - r.get("capex_int", 0)
    share = r["eq_owner"] / r["eq"] if r.get("eq") else 1.0
    return f * share


def value_all(B, roes, nis, fcfes, div, k):
    """4가지 방법으로 지배주주 지분가치(원) 계산."""
    out, detail = {}, {}
    # 1) S-RIM: B + B×(ROE−k)×w/(1+k−w)
    roe = wavg3(roes)
    out["S-RIM"] = B + B * (roe - k) * W / (1 + k - W)
    detail["가중ROE"] = roe
    # 2) DCF(FCFE 고든형): F×(1+g)/(k−g). F가 0 이하이면 계산 불가
    F = wavg3(fcfes)
    detail["가중FCFE"] = F
    out["DCF"] = F * (1 + G) / (k - G) if F is not None and F > 0 else None
    # 3) DDM(고든형): D×(1+g)/(k−g)
    out["DDM"] = div * (1 + G) / (k - G)
    detail["배당총액"] = div
    # 4) 상대가치: ½×(기준PER×가중순이익) + ½×(기준PBR×자본). 순이익이 0 이하이면 PBR만
    NI = wavg3(nis)
    detail["가중순이익"] = NI
    pbr_v = PBR_REF * B
    out["상대가치"] = 0.5 * PER_REF * NI + 0.5 * pbr_v if NI and NI > 0 else pbr_v
    vals = [v for v in out.values() if v is not None]
    out["종합(중앙값)"] = statistics.median(vals)
    return out, detail


def run(key):
    c = INP["companies"][key]
    fin = load_fin(key)
    h1 = json.load(open(os.path.join(ROOT, "raw", f"{key}_2026_H1.json")))
    divs = {int(y): v * 1e6 for y, v in c["cash_div_total"].items()}
    price = {int(y): v for y, v in c["price_close"].items()}
    sh = c["shares"]

    # ROE = 지배순이익 / 평균 지배자본
    def roe(y):
        r, p = fin.get(y), fin.get(y - 1)
        if not r:
            return None
        b = (r["eq_owner"] + p["eq_owner"]) / 2 if p else r["eq_owner"]
        return r["ni_owner"] / b

    rows = []
    for y in PAST:
        if y not in fin:
            continue
        k = RF[y] + ERP
        ys = [y, y - 1, y - 2]
        v, d = value_all(fin[y]["eq_owner"], [roe(t) for t in ys],
                         [fin[t]["ni_owner"] if t in fin else None for t in ys],
                         [fcfe(fin[t]) if t in fin else None for t in ys], divs.get(y, 0), k)
        rows.append({"year": y, "kind": "과거", "k": k, "B": fin[y]["eq_owner"], **d, "value": v, "price": price.get(y)})

    # 올해: 최근 12개월
    cur, prv = h1["h1_2026"], h1["h1_2025"]
    ttm = {x: fin[2025][x] + cur[x] - prv[x] for x in ("ni_owner", "ni", "ocf", "capex_ppe", "capex_int", "revenue", "op")}
    ttm["eq_owner"], ttm["eq"] = cur["eq_owner"], cur["eq"]
    b_avg = (ttm["eq_owner"] + fin[2025]["eq_owner"]) / 2
    roe_now = ttm["ni_owner"] / b_avg
    k_now = RF[NOW] + ERP
    v, d = value_all(ttm["eq_owner"], [roe_now, roe(2025), roe(2024)],
                     [ttm["ni_owner"], fin[2025]["ni_owner"], fin[2024]["ni_owner"]],
                     [fcfe(ttm), fcfe(fin[2025]), fcfe(fin[2024])], divs[2025], k_now)
    rows.append({"year": NOW, "kind": "올해(최근12개월)", "k": k_now, "B": ttm["eq_owner"], **d, "value": v, "price": price.get(NOW)})
    base_roe = d["가중ROE"]
    f_now = fcfe(ttm)

    # 미래 시나리오
    scen_rows = {}
    for s, p in SCEN.items():
        B = ttm["eq_owner"]
        hist_roe = [roe_now, roe(2025)]
        hist_ni = [ttm["ni_owner"], fin[2025]["ni_owner"]]
        hist_f = [f_now, fcfe(fin[2025])]
        div = divs[2025] * p["div"]
        k = k_now + p["k"]
        r_ = base_roe + p["roe"]
        out = []
        for y in FUTURE:
            ni = B * r_
            B_new = B + ni - div
            f = 0.5 * (f_now or 0) + 0.5 * ni  # 현금흐름은 현재 수준과 이익 경로의 중간으로 가정
            hist_roe.insert(0, r_); hist_ni.insert(0, ni); hist_f.insert(0, f)
            v, d = value_all(B_new, hist_roe[:3], hist_ni[:3], hist_f[:3], div, k)
            out.append({"year": y, "kind": f"미래-{s}", "k": k, "B": B_new, **d, "value": v})
            B = B_new
        scen_rows[s] = out

    def ps(x):
        return None if x is None else x / sh

    return {
        "key": key, "name": c["name"], "stock_code": c["stock_code"], "shares": sh,
        "rows": rows, "scenarios": scen_rows, "ps": ps,
    }


def main():
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    out = {"기준일": INP["기준일"], "가정": A, "시나리오": SCEN, "companies": []}
    csv_rows = []
    for key in INP["companies"]:
        r = run(key)
        sh = r["shares"]
        comp = {"key": key, "name": r["name"], "stock_code": r["stock_code"], "shares": sh, "series": []}
        def per_share(row):
            return {m: (None if v is None else round(v / sh)) for m, v in row["value"].items()}
        for row in r["rows"]:
            comp["series"].append({"year": row["year"], "kind": row["kind"], "k": round(row["k"], 4),
                                   "roe_w": round(row["가중ROE"], 4), "price": row["price"], "per_share": per_share(row)})
            csv_rows.append([r["name"], row["year"], row["kind"], round(row["k"] * 100, 2), round(row["가중ROE"] * 100, 2),
                             round(row["B"] / 1e8), *[per_share(row)[m] for m in ("S-RIM", "DCF", "DDM", "상대가치", "종합(중앙값)")], row["price"]])
        comp["future"] = {}
        for s, rows in r["scenarios"].items():
            comp["future"][s] = []
            for row in rows:
                comp["future"][s].append({"year": row["year"], "k": round(row["k"], 4), "roe_w": round(row["가중ROE"], 4), "per_share": per_share(row)})
                csv_rows.append([r["name"], row["year"], row["kind"], round(row["k"] * 100, 2), round(row["가중ROE"] * 100, 2),
                                 round(row["B"] / 1e8), *[per_share(row)[m] for m in ("S-RIM", "DCF", "DDM", "상대가치", "종합(중앙값)")], None])
        out["companies"].append(comp)
    json.dump(out, open(os.path.join(ROOT, "output", "results.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "output", "results.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["회사", "연도", "구분", "요구수익률%", "가중ROE%", "지배자본(억원)", "S-RIM", "DCF", "DDM", "상대가치", "종합(중앙값)", "연말주가"])
        w.writerows(csv_rows)
    for c in out["companies"]:
        print(c["name"])
        for s in c["series"]:
            p = s["per_share"]
            print(f'  {s["year"]} k={s["k"]*100:.1f}% ROE={s["roe_w"]*100:5.1f}%  SRIM={p["S-RIM"]:>9,} DCF={"-" if p["DCF"] is None else format(p["DCF"],","):>9} DDM={p["DDM"]:>9,} REL={p["상대가치"]:>9,} MED={p["종합(중앙값)"]:>9,}  주가={s["price"]:,}')
        for sname, fr in c["future"].items():
            print("  ", sname, [(x["year"], x["per_share"]["종합(중앙값)"]) for x in fr])


if __name__ == "__main__":
    main()
