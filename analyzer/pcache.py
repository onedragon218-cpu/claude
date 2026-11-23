"""화면 자료 중 '모든 회사를 훑는 계산'을 한 번 하고 저장해 두기(2026-11-23, 종목 바꿀 때 빠르게)

- 열쇠: (칸 이름, 기준 연도). 저장 조건(서명): DB 파일의 날짜·크기, 데이터 폴더 파일들의 날짜, 지식 사전 파일 날짜, 오늘 날짜.
  자료가 갱신되면 DB 파일이 바뀌므로 다음 화면에서 자동으로 다시 계산한다. 날짜가 바뀌어도 다시 계산(신선도 색 등).
- 단계별 시간도 여기서 잰다(B탭 '화면 자료 만들기' 표시용).
"""
import datetime as dt
import os
import threading
import time

from . import config

_C = {}
_L = threading.Lock()
BATCH = threading.local()   # 전체 갱신 중(이 스레드): 회사마다 저장하는 화면 파일은 모든 회사 계산을 처음 한 번만(조금 묵은 값 허용)
KDIR = os.path.join(os.path.dirname(__file__), "knowledge")


def _mt(d):
    """폴더 안 파일(DB 파일 제외) 중 가장 최근 날짜"""
    try:
        return max(((e.stat().st_mtime_ns, e.name) for e in os.scandir(d)
                    if e.is_file() and not e.name.startswith("analyzer.db")), default=None)
    except OSError:
        return None


SKIP = {"validation", "valuations", "sqlite_sequence"}   # 화면을 열 때마다 그 회사 몫을 다시 쓰는 표(모든 회사 계산은 이 표를 읽지 않음)


def signature(conn):
    """DB 표마다 마지막 줄 번호 + 회사 수·수정 시각, 데이터 폴더 파일 날짜(DB 파일 제외), 지식 사전 날짜, 오늘.
    DB 파일 날짜를 쓰지 않는 까닭: 화면을 열 때마다 가치평가·검증 결과를 다시 써서 파일 날짜가 늘 바뀜."""
    parts = []
    try:
        path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        parts.append(path)
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            if t in SKIP:
                continue
            try:
                m = conn.execute(f'SELECT MAX(rowid) FROM "{t}"').fetchone()[0]
            except Exception:      # noqa: BLE001
                m = None
            if m is not None:      # 빈 표는 넣지 않음 — 설치 뒤 빈 표가 새로 생겨도 '자료 바뀜'으로 보지 않게
                parts.append((t, m))
        parts.append(tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM companies").fetchone()))
    except Exception:      # noqa: BLE001
        parts.append(None)
    return (tuple(parts), _mt(config.DATA_DIR), _mt(KDIR), dt.date.today().isoformat())


def get(name, key, fn, sig):
    """반환: (값, 저장본을 썼나)"""
    with _L:
        hit = _C.get((name, key))
    if hit and (hit[0] == sig or getattr(BATCH, "on", False)):
        return hit[1], True
    v = fn()
    with _L:
        _C[(name, key)] = (sig, v)
    return v, False


def is_warm(sig):
    """이 서명으로 저장해 둔 칸이 하나라도 있나"""
    with _L:
        return any(v[0] == sig for v in _C.values())


class batch:
    """with pcache.batch(): 전체 갱신 — 끝나면 저장본을 비워 다음 화면은 새로 계산"""

    def __enter__(self):
        BATCH.on = True
        return self

    def __exit__(self, *a):
        BATCH.on = False
        clear()


def clear():
    with _L:
        _C.clear()


class Timer:
    """칸마다 걸린 시간 기록: with T("이름", cached): ..."""

    def __init__(self):
        self.t0 = time.time()
        self.items = []

    def run(self, name, fn, cached_name=None, key=None, sig=None):
        t = time.time()
        if cached_name:
            v, hit = get(cached_name, key, fn, sig)
        else:
            v, hit = fn(), False
        self.items.append({"name": name, "sec": round(time.time() - t, 3), "cached": hit})
        return v

    def summary(self):
        tot = round(time.time() - self.t0, 3)
        top = sorted(self.items, key=lambda x: -x["sec"])[:8]
        return {"total": tot, "top": top, "n_cached": sum(1 for x in self.items if x["cached"])}
