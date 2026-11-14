"""컨센서스 보정계수: 과거 컨센서스가 실제 실적과 얼마나 달랐는지를 자료로 재서 만든다.

방법
- 표본: 기본 종목 목록(컨센서스 제공 기업). 회계연도 T(가장 최근에 끝난 해, 2026-11 기준 2025년)에 대해
  WiseReport '컨센서스 추이'(월별 컨센서스)와 실제 실적(A)을 받는다.
- 비율 = 실제 ÷ 그때의 컨센서스(컨센서스가 0보다 클 때만). 'T년 말까지 남은 개월 수'별로 기업들의 중앙값을 낸다.
  중앙값을 쓰는 이유: 적자 전환·소형주 등 극단값의 영향을 줄이기 위해.
- 보정계수 = 그 중앙값. 예) 0.85면 '컨센서스의 85% 수준으로 실현되어 왔다'는 뜻.
적용
- 원래 컨센서스는 DB에 그대로 두고, 화면 그래프·가치평가에 쓸 때만 곱한다.
- 남은 개월 수가 측정 범위(0~11개월)를 넘는 경우(내년·내후년 추정)는 가장 긴 측정값(11개월)을 쓰고 '근사'로 표시한다.
한계(화면에도 표시): 한 해(2025년) 자료만으로 잰 값이며, 경기 국면에 따라 해마다 달라질 수 있다.
"""
import datetime as dt
import json
import os
import statistics
import time

from . import config, universe
from .collectors import wisereport

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration_default.json")
CACHE = lambda: os.path.join(config.DATA_DIR, "consensus_calibration.json")
METRICS = ["revenue", "op_income", "eps"]


def _months_before(date_str, fy):
    d = dt.date.fromisoformat(date_str.replace("/", "-"))
    end = dt.date(fy, 12, 31)
    return round((end - d).days / 30.44)


def sample_company(code, fy, delay=0.25):
    """한 기업의 (지표 → [(남은 개월, 비율)])"""
    est, act, _ = wisereport.parse_data(wisereport._get(wisereport.DATA_URL.format(code=code, d=dt.date.today().strftime("%Y%m%d"))))
    actual = {a["metric"]: a["value"] for a in act if a["target_year"] == fy}
    time.sleep(delay)
    out = {m: [] for m in METRICS}
    for metric in ("revenue", "op_income"):
        h = wisereport.history(code, fy, metric)
        time.sleep(delay)
        for key in (metric, "eps") if metric == "revenue" else (metric,):
            a = actual.get(key)
            if a is None:
                continue
            for d, v in zip(h["dates"], h[key]):
                if v is None or v <= 0:
                    continue
                mb = _months_before(d, fy)
                if 0 <= mb <= 11:
                    out[key].append((mb, a / v))
    return out


def build(codes=None, fy=None, delay=0.25, progress=None):
    fy = fy or dt.date.today().year - 1
    codes = codes or [c for c, _ in universe.load()]
    per = {m: {} for m in METRICS}    # metric → months → [ratio]
    used, failed = 0, []
    for i, code in enumerate(codes):
        try:
            res = sample_company(code, fy, delay)
            used += 1
            for m, pts in res.items():
                for mb, r in pts:
                    per[m].setdefault(mb, []).append(r)
        except Exception as e:
            failed.append([code, str(e)[:80]])
        if progress:
            progress(i + 1, len(codes))
    factors = {}
    for m, buckets in per.items():
        factors[m] = {}
        for mb, rs in sorted(buckets.items()):
            rs = sorted(rs)
            factors[m][str(mb)] = {"median": statistics.median(rs), "n": len(rs),
                                   "p25": rs[len(rs) // 4], "p75": rs[(3 * len(rs)) // 4]}
    out = {"fiscal_year": fy, "built_at": dt.datetime.now().isoformat(" ", "seconds"), "companies": used,
           "failed": failed, "factors": factors,
           "_설명": "보정계수 = 실제 ÷ 그때 컨센서스의 기업 간 중앙값, 키는 회계연도 말까지 남은 개월 수(0~11)"}
    os.makedirs(os.path.dirname(CACHE()), exist_ok=True)
    with open(CACHE(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def load():
    for p in (CACHE(), DEFAULT):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return None


def factor(metric, target_year, today=None, cal=None, measured=False):
    """(계수, 설명) — 보정 정보가 없으면 (1.0, '보정 없음'). measured=True면 설정과 무관하게 실측 보정계수(화면 Q 토글용)"""
    from . import valuation
    A = valuation.load_assumptions().get("컨센서스_보정", {})
    if not A.get("사용", True) and not measured:
        return 1.0, "보정 끔(설정)"
    key0 = {"net_income": "eps"}.get(metric, metric)
    manual = (A.get("직접입력") or {}).get(key0)
    if manual and not measured:
        return float(manual), "직접 입력한 계수"
    if A.get("방식", "고정") == "고정" and not measured:
        v = (A.get("고정계수") or {}).get(key0)
        if v is not None:
            return float(v), f"고정 보정계수 {float(v):.2f}(설정 '고정계수', 실무 경험칙)"
        return 1.0, "보정 대상 아님"
    cal = cal or load()
    key = {"revenue": "revenue", "op_income": "op_income", "eps": "eps", "net_income": "eps"}.get(metric)
    if not cal or not key or not cal["factors"].get(key):
        return 1.0, "보정 자료 없음"
    today = today or dt.date.today()
    mb = round((dt.date(target_year, 12, 31) - today).days / 30.44)
    f = cal["factors"][key]
    avail = sorted(int(k) for k in f)
    approx = mb > max(avail) or mb < min(avail)
    k = min(max(mb, min(avail)), max(avail))
    k = min(avail, key=lambda a: abs(a - k))
    v = f[str(k)]
    return v["median"], (f"{cal['fiscal_year']}년 컨센서스 {k}개월 전 대비 실제 중앙값(기업 {v['n']}곳)"
                         + (f", 남은 기간 {mb}개월은 측정 범위 밖 → 근사" if approx else ""))
