"""FnGuide(wcomp) 공개 페이지에서 상장사 컨센서스 수집.
단계: 1) 기업 목록  2) 종목별 연간 컨센서스(2028E)  3) 2028E 통과 종목만 스냅샷(추정기관수)
결과는 out/ 폴더에 JSON으로 저장(중간 저장, 재시작 가능)."""
import json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
BASE = "https://wcomp.fnguide.com"
UA = {"User-Agent": "Mozilla/5.0"}
DELAY = 0.4
WORKERS = 4


def get(path, params, as_json=True, tries=4):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                body = r.read().decode("utf-8", "ignore")
            time.sleep(DELAY)
            return json.loads(body) if as_json else body
        except Exception as e:  # 네트워크 오류 시 재시도
            time.sleep(2 ** (i + 1))
            err = e
    raise err


def load(name, default):
    p = os.path.join(OUT, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default


def save(name, obj):
    p = os.path.join(OUT, name)
    json.dump(obj, open(p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(p + ".tmp", p)


# 1) 기업 목록
corps = load("corps.json", None)
if corps is None:
    corps = {}
    for c in list("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ") + ["a", "0"]:
        for d in get("/SideInfo/getInfCrp", {"consonant": c})["dataset"]["data"] or []:
            corps[d["CMP_CD"]] = {"code": d["CMP_CD"], "name": d["CMP_KOR"], "mkt": d["MKT"]}
    save("corps.json", corps)
print("기업 수", len(corps), flush=True)

TARGET = "2028/12"
ITEMS = {"매출액": "sales", "영업이익": "op", "EPS": "eps"}


def perf(code, consol):
    ds = get("/CompanyInfo/getCnsPerforTrend",
             {"cmp_cd": code, "consol_typ": consol, "freq_typ": "Y", "data_typ": "1"})["dataset"]
    hdr = {h["CD"]: (h["YYMM"], (h.get("EP_CHK") or "").strip()) for h in ds.get("header") or []}
    res = {}
    for row in ds.get("data") or []:
        nm = row["NAME"].strip()
        if nm in ITEMS and row["NAME"] == row["NAME"].lstrip():
            for cd, (yymm, ep) in hdr.items():
                if ep == "E":
                    res[f"{ITEMS[nm]}_{yymm[:4]}E"] = row.get(cd)
    return res


# 2) 컨센서스
cons = load("cons.json", {})
def skip(c):  # 우선주·스팩·리츠는 수집 생략 (엑셀 단계에서 제외 사유로 기록)
    nm = corps[c]["name"]
    return c[-1] != "0" or "스팩" in nm or "기업인수목적" in nm or "리츠" in nm or "REIT" in nm.upper()


todo = [c for c in corps if c not in cons and corps[c]["mkt"] in ("코스피", "코스닥") and not skip(c)]
def one_cons(code):
    rec = {}
    for consol in ("C", "P"):
        try:
            v = perf(code, consol)
        except Exception as e:
            return code, {"error": str(e)}
        if all(v.get(f"{k}_2028E") not in (None, "") for k in ITEMS.values()):
            return code, {"consol": consol, **v}
        if not rec:
            rec = {"consol": None, **v}
    return code, rec


with ThreadPoolExecutor(WORKERS) as ex:
    for i, (code, rec) in enumerate(ex.map(one_cons, todo), 1):
        cons[code] = rec
        if i % 20 == 0:
            save("cons.json", cons)
            print("컨센서스", i, "/", len(todo), time.strftime("%H:%M:%S"), flush=True)
save("cons.json", cons)

# 3) 스냅샷 (2028E 3개 항목 모두 있는 종목만)
snap = load("snap.json", {})
cand = [c for c, r in cons.items() if r.get("consol") and c not in snap]
def one_snap(code):
    try:
        t = get("/CompanyInfo/Snapshot", {"cmp_cd": code}, as_json=False)
        j = t.find("추정기관수")
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t[j:j + 1500])) if j >= 0 else ""
        m = re.match(r"추정기관수 ([\d.,\-]+) ([\d.,\-]+) ([\d.,\-]+) ([\d.,\-]+) ([\d,\-]+) ", txt)
        title = re.search(r"<title>([^<|]*)", t)
        return code, {"title": title.group(1).strip() if title else "",
                      "opinion": m.group(1) if m else None, "target": m.group(2) if m else None,
                      "n_inst": m.group(5) if m else None}
    except Exception as e:
        return code, {"error": str(e)}


with ThreadPoolExecutor(WORKERS) as ex:
    for i, (code, rec) in enumerate(ex.map(one_snap, cand), 1):
        snap[code] = rec
        if i % 20 == 0:
            save("snap.json", snap)
            print("스냅샷", i, "/", len(cand), time.strftime("%H:%M:%S"), flush=True)
save("snap.json", snap)
print("완료", flush=True)
