"""한국거래소 정보데이터시스템(data.krx.co.kr) 종목 검색 자료로 전체 상장 종목 목록을 받는다.

- 쓰는 자료: 종목 찾기 화면이 쓰는 'finder_stkisu'(로그인 없이 제공, 2026-11-04 확인).
  주가·시가총액 같은 통계 화면(MDCSTAT…)은 로그인이 필요해 쓰지 않는다(접근 권한을 우회하지 않음).
- 반환: 종목코드, 종목명, 시장(KOSPI·KOSDAQ·KOSDAQ GLOBAL·KONEX), ISIN.
- 결과는 데이터 폴더 krx_listing.json 에 저장하고 7일 안에는 다시 받지 않는다.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from .. import config

URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
REFERER = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"
UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"
CACHE = lambda: os.path.join(config.DATA_DIR, "krx_listing.json")


class KrxUnavailable(Exception):
    pass


def fetch():
    body = urllib.parse.urlencode({"bld": "dbms/comm/finder/finder_stkisu", "mktsel": "ALL", "searchText": "",
                                   "typeNo": "0"}).encode()
    req = urllib.request.Request(URL, data=body, headers={"User-Agent": UA, "Referer": REFERER,
                                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception as e:
        raise KrxUnavailable(f"한국거래소 접속 실패: {e}") from e
    if txt.strip() == "LOGOUT":
        raise KrxUnavailable("한국거래소가 로그인을 요구함(LOGOUT)")
    try:
        rows = json.loads(txt)["block1"]
    except (ValueError, KeyError) as e:
        raise KrxUnavailable(f"한국거래소 응답을 읽지 못함: {txt[:80]!r}") from e
    return [{"code": r["short_code"].upper(), "name": r["codeName"], "market": r.get("marketEngName"),
             "isin": r.get("full_code")} for r in rows if r.get("short_code")]


def listing(max_age_days=7):
    """저장본이 7일 안이면 그대로, 아니면 새로 받는다. 실패하면 저장본(있으면)을 쓴다."""
    path = CACHE()
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_days * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    try:
        rows = fetch()
    except KrxUnavailable:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


def cached():
    """네트워크 없이 저장본만(검색용)"""
    if os.path.exists(CACHE()):
        with open(CACHE(), encoding="utf-8") as f:
            return json.load(f)
    return []
