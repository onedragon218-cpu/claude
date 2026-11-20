"""뉴스 제목 수집기. 본문은 받지 않고 제목·날짜·언론사·링크만 저장한다.

1순위: 네이버 뉴스 검색 API(openapi.naver.com, 무료, 키 필요 — 하루 25,000건)
   키 위치: 환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
            또는 %APPDATA%\\corp-analyzer\\naver_api_key.txt (1줄: Client ID, 2줄: Client Secret)
2순위: 구글 뉴스 RSS(news.google.com, 키 불필요). 한 번에 최대 100건이라 한 달씩 나눠 검색한다.
로그인·쿠키·브라우저 흉내는 쓰지 않는다.
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


UA = "corp-analyzer/1.0 (personal research)"


class NewsUnavailable(Exception):
    pass


class NaverAuthError(NewsUnavailable):
    """네이버가 키를 거절함(401 인증 실패·403 권한 없음). 이 프로그램이 도는 동안은 네이버를 건너뛰고 구글로."""


_NAVER_BAD = {}       # {키 묶음: 사유} — 거절된 키는 같은 실행 중 다시 쓰지 않음(파일이 바뀌면 새 키로 다시 시도)


def key_file():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "naver_api_key.txt")


def naver_key():
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if cid and sec:
        k = (cid.strip(), sec.strip())
        return None if k in _NAVER_BAD else k
    try:
        with open(key_file(), encoding="utf-8-sig") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip()]
        if len(lines) >= 2:
            k = (lines[0], lines[1])
            return None if k in _NAVER_BAD else k
    except OSError:
        pass
    return None


def naver_problem():
    """거절된 네이버 키 사유(없으면 None) — 화면·기록용, 키 값은 담지 않음"""
    return next(iter(_NAVER_BAD.values()), None)


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _date(s):
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return None


def _press(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return None
    return h[4:] if h.startswith("www.") else h or None


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            d = json.loads(e.read().decode("utf-8", "replace") or "{}")
            body = f" {d.get('errorCode', '')} {d.get('errorMessage', '')}".rstrip()
        except Exception:     # noqa: BLE001
            pass
        if "openapi.naver.com" in url and e.code in (401, 403):
            why = ("네이버 키 인증 실패(401" + (f",{body}" if body else "") + ") — Client ID·Secret이 맞지 않음: 「재무분석 인증키 설정」 3번으로 다시 넣기"
                   if e.code == 401 else
                   "네이버 API 권한 없음(403" + (f",{body}" if body else "") + ") — 네이버 개발자센터 내 애플리케이션 → API 설정에 '검색' 추가")
            raise NaverAuthError(why) from None
        raise NewsUnavailable(f"뉴스 접속 실패: HTTP {e.code}{body}") from None
    except Exception as e:
        raise NewsUnavailable(f"뉴스 접속 실패: {e}") from e


def naver(query, since, key=None, max_pages=5):
    """네이버 뉴스 검색 API, 최신순. since(date) 이전 기사가 나오면 멈춘다. 반환: (요청 수, 기사 목록)"""
    key = key or naver_key()
    if not key:
        raise NewsUnavailable("네이버 API 키 없음")
    out, n = [], 0
    for page in range(max_pages):
        url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
            {"query": query, "display": 100, "start": page * 100 + 1, "sort": "date"})
        n += 1
        try:
            d = json.loads(_get(url, {"X-Naver-Client-Id": key[0], "X-Naver-Client-Secret": key[1]}))
        except NaverAuthError as e:
            _NAVER_BAD[tuple(key)] = str(e)
            raise
        except ValueError as e:
            raise NewsUnavailable("네이버 응답을 읽지 못함") from e
        items = d.get("items") or []
        old = False
        for it in items:
            day = _date(it.get("pubDate"))
            if not day:
                continue
            if day < since.isoformat():
                old = True
                continue
            link = it.get("originallink") or it.get("link")
            out.append({"published": day, "title": clean(it.get("title")), "link": it.get("link") or link,
                        "press": _press(link), "description": clean(it.get("description"))[:200]})
        if old or len(items) < 100:
            break
    return n, out


def google(query, since, until=None):
    """구글 뉴스 RSS를 한 달씩 나눠 검색. 반환: (요청 수, 기사 목록)"""
    until = until or dt.date.today()
    out, n, start = [], 0, since
    while start <= until:
        end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        q = f"{query} after:{start.isoformat()} before:{min(end, until + dt.timedelta(days=1)).isoformat()}"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        n += 1
        try:
            root = ET.fromstring(_get(url))
        except ET.ParseError as e:
            raise NewsUnavailable("구글 뉴스 응답을 읽지 못함") from e
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            press = clean(it.findtext("source")) or None
            if press and title.endswith(" - " + press):
                title = title[: -len(press) - 3]
            day = _date(it.findtext("pubDate"))
            if day and title:
                out.append({"published": day, "title": title, "link": it.findtext("link"), "press": press})
        start = end
    return n, out


def search_link(title):
    """링크가 없는 제목(예: 증권사 시세 뉴스 제목)은 네이버 뉴스 제목 검색 주소로 연결한다."""
    return "https://search.naver.com/search.naver?where=news&query=" + urllib.parse.quote(f'"{title}"')


def fetch(query, since):
    """네이버 API(키 있을 때) → 구글 뉴스 RSS 순서. 반환: (출처 이름, 요청 수, 기사 목록)"""
    errs = []
    if naver_key():
        try:
            n, items = naver(query, since)
            return "네이버 뉴스 API", n, items
        except NewsUnavailable as e:
            errs.append(f"네이버: {e}")
    try:
        n, items = google(query, since)
        bad = naver_problem()
        return ("구글 뉴스 RSS" + (" (네이버 키 거절됨 — B탭 메모 참고)" if bad else "")), n, items
    except NewsUnavailable as e:
        errs.append(f"구글: {e}")
    raise NewsUnavailable("; ".join(errs))
