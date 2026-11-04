"""기본 종목 목록(컨센서스가 있는 상장기업, 사용자 제공 2026-11-04, 281곳).

- 목록만 들고 있다. 이 목록에 있다고 자동으로 수집하지 않는다.
  수집은 사용자가 화면에서 고르거나 '분석 실행'을 눌렀을 때만 한다(전체 갱신·자동 수집은 DB에 자료가 있는 기업만).
- 종목코드는 6자리이며 영문이 섞일 수 있다(예: 0126Z0).
"""
import os
import re

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.tsv")
CODE_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")


def is_code(s):
    return bool(CODE_RE.match((s or "").strip().upper()))


def load():
    out = []
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and is_code(parts[0]):
                out.append((parts[0].strip().upper(), parts[1].strip()))
    return out
