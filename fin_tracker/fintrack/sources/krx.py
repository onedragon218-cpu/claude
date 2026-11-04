"""연말 시가총액.

1순위: 한국거래소 자료(pykrx 라이브러리). 그해 마지막 거래일의 시가총액·상장주식수·종가를 그대로 쓴다 → 검증 가능.
2순위: 네이버 일봉 차트 종가 × DART 보통주 발행주식수. 차트 종가는 수정주가일 수 있어 '계산값(미검증)'으로 둔다.
받은 값은 작업 폴더 raw/krx/에 보관하고, 지난해 이전 값은 다시 받지 않는다.
"""
import gzip, json, os, re
from datetime import date

SOURCE_KRX = "한국거래소(pykrx)"
SOURCE_NAVER = "네이버 차트 종가×DART 주식수"


def _cache_path(w, code, year):
    return os.path.join(w.rawdir("krx"), f"{code}_{year}.json.gz")


def year_end(w, code, year):
    """반환: (dict 또는 None, 채널, 설명). dict: {date, close, market_cap, listed_shares, source}"""
    p = _cache_path(w, code, year)
    if os.path.exists(p):
        return json.load(gzip.open(p, "rt", encoding="utf-8")), "raw-cache", os.path.basename(p)
    res, why = _pykrx(code, year)
    if res is None:
        return None, "api", why
    if year < date.today().year:   # 끝난 해만 보관(올해는 계속 바뀜)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    return res, "api", "pykrx 조회"


def _pykrx(code, year):
    try:
        from pykrx import stock
    except Exception:
        return None, "pykrx 미설치"
    try:
        end = f"{year}1231"
        df = stock.get_market_cap_by_date(f"{year}1201", end, code)
        if df is None or df.empty:
            return None, "pykrx 자료 없음"
        last = df.iloc[-1]
        d = df.index[-1].strftime("%Y-%m-%d")
        oh = stock.get_market_ohlcv_by_date(df.index[-1].strftime("%Y%m%d"), df.index[-1].strftime("%Y%m%d"), code, adjusted=False)
        close = float(oh.iloc[-1]["종가"]) if oh is not None and not oh.empty else None
        return {"date": d, "close": close, "market_cap": float(last["시가총액"]),
                "listed_shares": float(last["상장주식수"]), "source": SOURCE_KRX}, "ok"
    except Exception as e:
        return None, f"pykrx 실패: {type(e).__name__}"


def naver_closes(w, code):
    """{연도: (날짜, 종가)} 연말 종가. 하루 한 번만 받는다."""
    p = os.path.join(w.rawdir("krx"), f"{code}_naver_{date.today():%Y%m%d}.json.gz")
    if os.path.exists(p):
        d = json.load(gzip.open(p, "rt", encoding="utf-8"))
        return {int(k): tuple(v) for k, v in d.items()}, "raw-cache"
    import requests
    try:
        r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                         params={"symbol": code, "timeframe": "day", "count": 4000, "requestType": 0}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return None, f"네이버 차트 조회 실패: {type(e).__name__}"
    out = {}
    for m in re.finditer(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', r.text):
        dt, close = m.group(1), float(m.group(2))
        out[int(dt[:4])] = (f"{dt[:4]}-{dt[4:6]}-{dt[6:]}", close)   # 날짜순이라 마지막 값이 그해 마지막 거래일
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(out, f)
    return out, "web"
