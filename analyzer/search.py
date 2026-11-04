"""종목코드·종목명 검색. DB에 있는 기업과 DART 상장사 목록(data/corp_codes.json)을 함께 찾는다."""
import re

from . import universe
from .collectors import dart


def _norm(s):
    return re.sub(r"[\s()（）㈜]|주식회사", "", (s or "")).lower()


def search(conn, q, limit=10):
    q = (q or "").strip()
    if not q:
        return []
    out, seen = [], set()
    nq = _norm(q)
    db_rows = {r["stock_code"]: r["name"] for r in conn.execute("SELECT stock_code, name FROM companies")}
    try:
        corp = dart.corp_list()
    except dart.DartUnavailable:
        corp = {}
    names = dict((c, v["name"]) for c, v in corp.items())
    names.update({c: n for c, n in universe.load()})          # 기본 종목 목록(키가 없어도 이름 검색 가능)
    names.update({c: n for c, n in db_rows.items() if n})

    def add(code, name, score):
        if code not in seen:
            seen.add(code)
            out.append({"code": code, "name": name, "in_db": code in db_rows, "score": score})

    uq = q.upper()
    if re.match(r"^[0-9][0-9A-Z]{0,5}$", uq):
        for c, n in names.items():
            if c.startswith(uq):
                add(c, n, 0 if c == uq else 1)
        if universe.is_code(uq) and uq not in seen:
            add(uq, db_rows.get(uq) or "", 0)
    else:
        for c, n in names.items():
            nn = _norm(n)
            if nn == nq:
                add(c, n, 0)
            elif nn.startswith(nq):
                add(c, n, 1)
            elif nq in nn:
                add(c, n, 2)
    out.sort(key=lambda x: (x["score"], not x["in_db"], len(x["name"] or ""), x["code"]))
    return out[:limit]


class Ambiguous(Exception):
    def __init__(self, q, cands):
        self.cands = cands
        super().__init__(f"'{q}'에 해당하는 종목이 여러 개입니다: " +
                         ", ".join(f"{c['name']}({c['code']})" for c in cands[:8]))


def resolve(conn, q):
    """종목코드 또는 종목명 → 종목코드. 정확히 같은 이름이 하나면 그것, 후보가 하나뿐이면 그것."""
    q = (q or "").strip()
    if universe.is_code(q):
        return q.upper()
    cands = search(conn, q, limit=20)
    exact = [c for c in cands if c["score"] == 0]
    if len(exact) == 1:
        return exact[0]["code"]
    if len(cands) == 1:
        return cands[0]["code"]
    if not cands:
        hint = "" if dart.has_key() else " (DART 키가 없으면 DB에 있는 기업 이름만 찾을 수 있습니다)"
        raise LookupError(f"'{q}'에 해당하는 종목을 찾지 못했습니다{hint}.")
    raise Ambiguous(q, cands)
