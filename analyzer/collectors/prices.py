"""주가 수집기.

한국거래소 정보데이터시스템(data.krx.co.kr)은 2026-11 현재 로그인을 요구하므로(응답 'LOGOUT') 자동 수집에 쓰지 않는다.
대신 아래 두 경로를 쓴다.
  1) KIS 기간별 시세를 JSON으로 저장한 파일 가져오기(import_kis_json)
  2) 네이버 금융 차트 데이터(fetch_naver_daily) — 공개 XML. 수정주가일 수 있어 basis에 표시한다.
"""
import json
import re
import urllib.request

UA = "corp-analyzer/1.0 (personal research; github.com/onedragon218-cpu/claude)"


class PriceUnavailable(Exception):
    pass


def import_kis_json(path):
    d = json.load(open(path, encoding="utf-8"))
    rows = [(r["date"], float(r["close"]), r.get("_note")) for r in d["rows"]]
    return {"source": "KIS", "basis": "보통주 종가(수정주가 아님)", "ref": d.get("_source", "KIS"),
            "collected_at": d.get("_collected_at"), "rows": rows}


def fetch_naver_daily(code, count=3000):
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("euc-kr", "replace")
    except Exception as e:
        raise PriceUnavailable(f"네이버 시세 접속 실패: {e}") from e
    rows = []
    for m in re.finditer(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', txt):
        rows.append((m.group(1), float(m.group(2)), None))
    if not rows:
        raise PriceUnavailable("네이버 시세 응답에서 값을 찾지 못했습니다.")
    return {"source": "Naver", "basis": "보통주 종가(네이버 차트, 수정주가일 수 있음)", "ref": url, "rows": rows}


def year_end(rows):
    """[(YYYYMMDD, close, note)] → {year: (date, close, note)} 연도별 마지막 거래일."""
    out = {}
    for d, c, n in sorted(rows):
        out[int(d[:4])] = (d, c, n)
    return out
