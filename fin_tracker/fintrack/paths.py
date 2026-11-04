"""작업 폴더 관리.

- 프로그램 설정(작업 폴더 위치)은 %APPDATA%\\fintrack\\settings.json 에 저장한다.
- 데이터는 모두 작업 폴더 안에 둔다. 이 폴더만 백업하면 DB·원본·화면이 함께 보존된다.

작업 폴더 구조
  <작업 폴더>/fintrack.sqlite   DB
  <작업 폴더>/raw/<출처>/...    받은 원본(gzip)
  <작업 폴더>/manual/...        손으로 넣는 보조 자료(선택)
  <작업 폴더>/output/<종목>.html 공유용 화면 파일
"""
import json, os, sys

APP = "fintrack"
CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP, "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    p = settings_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def pick_folder_dialog():
    """Windows 폴더 선택 창. 창을 띄울 수 없으면 None."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        d = filedialog.askdirectory(title="fintrack 작업 폴더를 고르세요 (DB와 자료가 여기에 저장됩니다)")
        root.destroy()
        return d or None
    except Exception:
        return None


def workdir(interactive=True):
    """설정된 작업 폴더. 없으면 고르게 한다."""
    env = os.environ.get("FINTRACK_WORKDIR")
    if env:
        return ensure(env)
    s = load_settings()
    if s.get("workdir") and os.path.isdir(s["workdir"]):
        return ensure(s["workdir"])
    if not interactive:
        raise SystemExit("작업 폴더가 정해지지 않았습니다. 'python -m fintrack set-workdir <폴더>'로 정하세요.")
    d = pick_folder_dialog()
    if not d:
        print("작업 폴더 경로를 입력하세요 (예: D:\\투자\\fintrack):", end=" ", flush=True)
        d = input().strip().strip('"')
    set_workdir(d)
    return ensure(d)


def set_workdir(d):
    d = os.path.abspath(d)
    ensure(d)
    s = load_settings(); s["workdir"] = d; save_settings(s)
    return d


def ensure(d):
    for sub in ("", "raw", "raw/dart", "raw/fnguide", "raw/wisereport", "raw/krx", "manual", "output"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


class W:
    """작업 폴더 안 경로 모음"""
    def __init__(self, root):
        self.root = root
        self.db = os.path.join(root, "fintrack.sqlite")
        self.raw = os.path.join(root, "raw")
        self.manual = os.path.join(root, "manual")
        self.output = os.path.join(root, "output")

    def rawdir(self, source):
        return os.path.join(self.raw, source)
