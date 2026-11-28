"""회사 정의(화면 R 맨 위) — 이 회사가 무엇을 하는 회사인지 (사용자 지시 2026-11-18)

자료
 ① WiseReport 기업 페이지 「기업개요」 3~4줄 + WICS 업종 (컨센서스를 받는 같은 사이트, 30일마다)
 ② DART 사업보고서 원문 「II. 사업의 내용」 — 1. 사업의 개요(앞부분) + 주요 제품·서비스 매출 비율 표
    (OpenDART document.xml, 키 필요. 새 사업보고서가 나올 때만 다시 받음. 회사마다 표 모양이 달라 못 읽으면 빈칸 + 이유)
저장: company_profile 표(원문 전체가 아니라 읽어 낸 결과와 앞부분만).
리서치 참고자료이며 투자자문이 아님
"""
import html
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile

from . import db

DOC_API = "https://opendart.fss.or.kr/api/document.xml"


def ensure(conn):
    db.ensure_tables(conn, """
    CREATE TABLE IF NOT EXISTS company_profile (
      stock_code TEXT PRIMARY KEY, wr_lines TEXT, wics TEXT, wr_url TEXT, wr_at TEXT,
      dart_rcept_no TEXT, dart_year INTEGER, dart_overview TEXT, dart_products TEXT, dart_note TEXT, dart_at TEXT);
    """)


def _text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------------------------------------------------------------- ① WiseReport 기업개요
def parse_wr(page):
    lines = [_text(m) for m in re.findall(r'<li class="dot_cmp"[^>]*>(.*?)</li>', page, re.S)]
    lines = [re.sub(r"\s+", " ", x) for x in lines if x]
    m = re.search(r"WICS\s*:\s*([^<&\n]+)", page)
    return {"lines": lines, "wics": m.group(1).strip() if m else None}


def fetch_wr(code):
    from .collectors import wisereport
    url = wisereport.PAGE_URL.format(code=code)
    return dict(parse_wr(wisereport._get(url)), url=url)


# ---------------------------------------------------------------- ② DART 사업보고서 「사업의 내용」
def _decode(b):
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def main_xml(zip_bytes, rcept_no=None):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = [n for n in z.namelist() if n.lower().endswith(".xml")]
    if not names:
        return None
    pick = next((n for n in names if rcept_no and n.startswith(rcept_no) and "_" not in n[len(rcept_no):len(rcept_no) + 1]), None)
    pick = pick or max(names, key=lambda n: z.getinfo(n).file_size)      # 본문이 가장 큼(첨부는 따로)
    return _decode(z.read(pick))


def _section(xml, title_re, level_tag=None):
    """TITLE이 title_re에 맞는 곳부터 같은(또는 더 높은) 단계의 다음 제목 전까지"""
    m = re.search(r"<TITLE[^>]*>\s*" + title_re, xml)
    if not m:
        return None
    start = m.start()
    tag = level_tag
    if not tag:
        before = xml[max(0, start - 400):start]
        tags = re.findall(r"<(SECTION-\d)", before)
        tag = tags[-1] if tags else "SECTION-1"
    end = xml.find("</" + tag + ">", m.end())
    return xml[start:end if end > 0 else len(xml)]


def _tables(x):
    out = []
    for t in re.findall(r"<TABLE\b.*?</TABLE>", x or "", re.S | re.I):
        rows = []
        for tr in re.findall(r"<TR\b.*?</TR>", t, re.S | re.I):
            cells = [_text(c[1]) for c in re.findall(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1>", tr, re.S | re.I)]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


def _pct(s):
    s = (s or "").replace(",", "").replace("%", "").strip()
    m = re.fullmatch(r"\(?(-?\d+(?:\.\d+)?)\)?", s)
    return float(m.group(1)) if m else None


SKIP = re.compile(r"^(합\s*계|총\s*계|소\s*계|계|합계\s*\(.*\)|내부거래.*|연결조정.*|기타\s*조정.*)$")


def product_table(rows):
    """주요 제품 표에서 [{name, desc, pct}] 읽기. 머리줄에 '비율·비중'이 있는 표만."""
    hi = next((i for i, r in enumerate(rows[:4]) if any(re.search(r"비\s*율|비\s*중", c) for c in r)), None)
    if hi is None:
        return []
    head = rows[hi]
    find = lambda pats: next((i for p in pats for i, c in enumerate(head) if re.search(p, c)), None)   # 앞 패턴 우선
    pi = find([r"비\s*율", r"비\s*중"])
    ni = find([r"품\s*목", r"제\s*품\s*명?$", r"사업\s*부문", r"부\s*문", r"구\s*분"])
    di = find([r"용\s*도", r"주요\s*제품", r"상표", r"브랜드", r"내\s*용", r"주요\s*품목"])
    ni = 0 if ni is None or ni == pi else ni
    out, last = [], None
    for r in rows[hi + 1:]:
        if len(r) < 2 or any(SKIP.match((c or "").replace(" ", "")) for c in r[:3] if c):   # 합계·소계 줄
            continue
        off = len(head) - len(r)                       # 앞 칸이 합쳐진 줄(rowspan) — 앞 이름을 이어 씀
        get = lambda i: r[i - off] if i is not None and 0 <= i - off < len(r) else None
        p = _pct(get(pi)) if pi is not None else None
        if p is None:
            p = next((_pct(c) for c in reversed(r) if _pct(c) is not None and 0 <= _pct(c) <= 100), None)
        name = get(ni) if off <= ni else last
        name = (name or last or "").strip()
        if not name or SKIP.match(name.replace(" ", "")) or p is None or not (0 < p <= 100):
            continue
        last = name
        desc = (get(di) or "").strip() if di is not None and di != ni else ""
        out.append({"name": name[:60], "desc": desc[:120], "pct": p})
    tot = sum(x["pct"] for x in out)
    return out if len(out) >= 1 and 50 <= tot <= 130 else []


def parse_business(xml):
    """반환: {overview, products, note}"""
    biz = _section(xml, r"[IVX]+\.\s*사업의\s*내용", "SECTION-1")
    if not biz:
        return {"overview": None, "products": [], "note": "「사업의 내용」 장을 찾지 못함"}
    ov = _section(biz, r"\d+\.\s*사업의\s*개요", "SECTION-2")
    overview = _text(re.sub(r"<TABLE\b.*?</TABLE>", " ", ov or "", flags=re.S | re.I).split("</TITLE>", 1)[-1])[:1500] if ov else None
    prods, note = [], None
    pr = _section(biz, r"\d+\.\s*주요\s*제품", "SECTION-2")
    for t in _tables(pr) + (_tables(biz) if not pr else []):
        prods = product_table(t)
        if prods:
            break
    if not prods:
        note = "주요 제품 매출 비율 표를 읽지 못함(표 모양이 다름)"
    return {"overview": overview, "products": prods, "note": note}


def fetch_dart_doc(rcept_no):
    from .collectors import dart
    url = DOC_API + "?" + urllib.parse.urlencode({"crtfc_key": dart.api_key(), "rcept_no": rcept_no})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": dart.UA}), timeout=90) as r:
            data = r.read()
    except Exception as e:
        raise dart.DartUnavailable(f"사업보고서 원문 접속 실패: {type(e).__name__}") from None   # 주소(키)는 내지 않음
    if not data.startswith(b"PK"):
        raise dart.DartUnavailable(f"사업보고서 원문이 아님(응답: {_decode(data[:150])})")
    return data


# ---------------------------------------------------------------- 수집(분석·갱신 때)
# 리츠·인프라펀드처럼 WiseReport 페이지에 「기업개요」 칸이 원래 없는 회사(2026-11-28): 예전엔 실패로 적고(메모 없음) 수집 때마다 다시 시도해
# '출처 고장 경보(최근 24시간 N번 모두 실패)'가 떴음. 이제 '없음'으로 적어 실패로 세지 않고 30일 뒤에 다시 확인. 접속 오류는 하루 뒤 다시.
WR_NONE = "기업개요 없음(리츠·인프라펀드 등 페이지에 개요 칸이 없음)"


def _wr_due(conn, code):
    import datetime as dt
    if db.fetched_recently(conn, "profile_wr", code, 30, ok_only=True):
        return False
    last = db.last_fetch(conn, "profile_wr", code)
    if last is None or "profile_wr" in db.FORCE:
        return True
    age = (dt.datetime.now() - dt.datetime.fromisoformat(last["fetched_at"])).days
    return age >= (30 if (last["note"] or "").startswith("기업개요 없음") else 1)


def collect(conn, code, has_key, stats=None):
    ensure(conn)
    row = conn.execute("SELECT * FROM company_profile WHERE stock_code=?", (code,)).fetchone()
    if _wr_due(conn, code):
        try:
            if stats:
                stats.req("WiseReport")
            w = fetch_wr(code)
            conn.execute("INSERT INTO company_profile(stock_code, wr_lines, wics, wr_url, wr_at) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(stock_code) DO UPDATE SET wr_lines=excluded.wr_lines, wics=excluded.wics, wr_url=excluded.wr_url, wr_at=excluded.wr_at",
                         (code, json.dumps(w["lines"], ensure_ascii=False), w["wics"], w["url"], db.now()))
            db.log_fetch(conn, "profile_wr", code, code, bool(w["lines"]), len(w["lines"]), None if w["lines"] else WR_NONE)
        except Exception as e:      # noqa: BLE001
            db.log_fetch(conn, "profile_wr", code, code, False, 0, str(e)[:200])
    if has_key:
        rep = conn.execute("SELECT rcept_no, report_year FROM disclosures WHERE stock_code=? AND reprt_code='11011' "
                           "ORDER BY report_year DESC, rcept_no DESC LIMIT 1", (code,)).fetchone()
        if rep and (row is None or row["dart_rcept_no"] != rep["rcept_no"]) and \
                not db.fetched_recently(conn, "profile_dart", f"{code}:{rep['rcept_no']}", 7):
            try:
                if stats:
                    stats.req("OpenDART")
                xml = main_xml(fetch_dart_doc(rep["rcept_no"]), rep["rcept_no"])
                b = parse_business(xml or "")
                conn.execute("INSERT INTO company_profile(stock_code, dart_rcept_no, dart_year, dart_overview, dart_products, dart_note, dart_at) "
                             "VALUES (?,?,?,?,?,?,?) ON CONFLICT(stock_code) DO UPDATE SET dart_rcept_no=excluded.dart_rcept_no, "
                             "dart_year=excluded.dart_year, dart_overview=excluded.dart_overview, dart_products=excluded.dart_products, "
                             "dart_note=excluded.dart_note, dart_at=excluded.dart_at",
                             (code, rep["rcept_no"], rep["report_year"], b["overview"], json.dumps(b["products"], ensure_ascii=False),
                              b["note"], db.now()))
                db.log_fetch(conn, "profile_dart", f"{code}:{rep['rcept_no']}", code, True, len(b["products"]), b["note"])
            except Exception as e:      # noqa: BLE001
                msg = str(e)[:200]
                if re.search(r'"status"\s*:\s*"01[34]"|<status>01[34]</status>|조회된 데이타가 없|파일이 존재하지 않', msg):
                    msg = "사업보고서 원문 없음(DART가 원문 파일을 주지 않음) — " + msg[:120]    # 원래 없는 자료: 출처 고장으로 세지 않음
                db.log_fetch(conn, "profile_dart", f"{code}:{rep['rcept_no']}", code, False, 0, msg)
    conn.commit()


def get(conn, code):
    ensure(conn)
    r = conn.execute("SELECT * FROM company_profile WHERE stock_code=?", (code,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("wr_lines", "dart_products"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except ValueError:
            d[k] = []
    return d
