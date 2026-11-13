"""무위험수익률(국고채 10년) 자동 갱신 — 한국은행 ECOS Open API (키 필요, 무료)

- 통계: 721Y001 「시장금리(월,분기,년)」 항목 5050000 「국고채(10년)」 (2026-11-13 PlayMCP koreaEcos로 코드 확인)
  지난 해 = 연(A) 평균, 올해 = 지금까지 나온 월(M) 평균의 평균
- 키 위치: 환경변수 ECOS_API_KEY 또는 %APPDATA%\\corp-analyzer\\ecos_api_key.txt
- 받은 값은 데이터 폴더 rates.json 에 저장(프로그램 폴더의 assumptions.json 은 새 버전으로 덮어써지므로 건드리지 않음).
  가치평가는 assumptions.json 표 위에 rates.json 값을 덮어써서 쓴다.
- 주의: 이 개발 환경에서는 ECOS 직접 접속이 막혀 실제 호출은 시험하지 못했다(응답 형식은 benchmark.fetch_ecos와 같음).
"""
import datetime as dt
import json
import os
import urllib.request

from . import config

STAT, ITEM = "721Y001", "5050000"


def key_file():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "ecos_api_key.txt")


def api_key():
    k = os.environ.get("ECOS_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(key_file(), encoding="utf-8-sig") as f:
            return f.read().strip() or None
    except OSError:
        return None


def path():
    return os.path.join(config.DATA_DIR, "rates.json")


def load():
    """{연도(int): 금리(%)}, 메타"""
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    return {int(k): v for k, v in (d.get("kr10y") or {}).items()}, {k: v for k, v in d.items() if k != "kr10y"}


def _rows(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    return (d.get("StatisticSearch") or {}).get("row") or []


def fetch(today=None):
    """ECOS에서 받아 rates.json 에 저장. 반환: 저장한 {연도: 금리} 또는 None(키 없음·실패)"""
    k = api_key()
    if not k:
        return None
    today = today or dt.date.today()
    cy = today.year
    base = f"https://ecos.bok.or.kr/api/StatisticSearch/{k}/json/kr/1/100/{STAT}"
    out = {}
    for row in _rows(f"{base}/A/2010/{cy - 1}/{ITEM}"):
        if row.get("DATA_VALUE") not in (None, ""):
            out[int(row["TIME"])] = round(float(row["DATA_VALUE"]), 3)
    months = [float(r["DATA_VALUE"]) for r in _rows(f"{base}/M/{cy}01/{cy}12/{ITEM}") if r.get("DATA_VALUE") not in (None, "")]
    meta = {"source": "한국은행 ECOS 721Y001 국고채(10년)", "fetched_at": dt.datetime.now().isoformat(" ", "seconds")}
    if months:
        out[cy] = round(sum(months) / len(months), 3)
        meta["current_note"] = f"{cy}년은 {len(months)}개월 평균"
    if not out:
        return None
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "w", encoding="utf-8") as f:
        json.dump(dict(meta, kr10y={str(y): v for y, v in sorted(out.items())}), f, ensure_ascii=False, indent=1)
    return out


def table(A):
    """가치평가에 쓰는 연도별 국고채 10년(%) = 설정 파일 표 + ECOS 자동 값(있으면 덮어씀). 반환: ({연도: %}, {연도: 출처})"""
    t = {int(k): v for k, v in (A.get("무위험수익률_국고채10년") or {}).items() if str(k).isdigit()}
    src = {y: "설정 파일" for y in t}
    auto, _ = load()
    for y, v in auto.items():
        t[y] = v
        src[y] = "ECOS 자동"
    return t, src


def info(A, year):
    """올해 금리가 있는지(화면 알림용)"""
    t, src = table(A)
    if year in t:
        return {"year": year, "ok": True, "value": t[year], "source": src[year], "has_key": bool(api_key())}
    past = [y for y in t if y <= year]
    used = max(past) if past else None
    return {"year": year, "ok": False, "used": used, "value": t.get(used), "source": src.get(used), "has_key": bool(api_key())}


def maybe_fetch(conn, offline=False):
    """키가 있으면 7일에 한 번 받는다(실패하면 하루 뒤 다시 시도). 실패해도 분석은 계속."""
    from . import db
    if offline or not api_key():
        return None
    if db.fetched_recently(conn, "ecos_rf", "kr10y", 7, ok_only=True) or db.fetched_recently(conn, "ecos_rf", "kr10y", 1):
        return None
    try:
        out = fetch()
        db.log_fetch(conn, "ecos_rf", "kr10y", None, bool(out), len(out or {}), None if out else "빈 응답")
        return out
    except Exception as e:       # 네트워크·키 오류
        db.log_fetch(conn, "ecos_rf", "kr10y", None, False, 0, str(e)[:200])
        return None
