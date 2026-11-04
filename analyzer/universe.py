"""기본 종목 목록(컨센서스가 있는 상장기업, 사용자 제공 2026-11-04, 281곳).

- 목록만 들고 있다. 이 목록에 있다고 자동으로 수집하지 않는다.
  수집은 사용자가 화면에서 고르거나 '분석 실행'을 눌렀을 때만 한다(전체 갱신·자동 수집은 DB에 자료가 있는 기업만).
- 종목코드는 6자리이며 영문이 섞일 수 있다(예: 0126Z0).
"""
import os
import re

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.tsv")


def user_path():
    """사용자가 '종목 찾기'에서 추가한 목록(데이터 폴더에 두어 프로그램을 업데이트해도 남는다)"""
    from . import config
    return os.path.join(config.DATA_DIR, "universe_user.tsv")
CODE_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")


def is_code(s):
    return bool(CODE_RE.match((s or "").strip().upper()))


def _read(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and is_code(parts[0]):
                    out.append((parts[0].strip().upper(), parts[1].strip()))
    return out


def load():
    """기본 목록 + 사용자 추가 목록(중복 제거, 기본 목록 순서 우선)"""
    seen, out = set(), []
    for c, n in _read(PATH) + _read(user_path()):
        if c not in seen:
            seen.add(c)
            out.append((c, n))
    return out


def add(items):
    """[(code, name)] 을 사용자 목록에 추가. 새로 추가된 개수 반환"""
    have = {c for c, _ in load()}
    new = [(c.upper(), n) for c, n in items if is_code(c) and c.upper() not in have]
    if new:
        os.makedirs(os.path.dirname(user_path()), exist_ok=True)
        with open(user_path(), "a", encoding="utf-8") as f:
            for c, n in new:
                f.write(f"{c}\t{n}\n")
    return len(new)
