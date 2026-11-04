"""사용법 (Windows에서는 run_fintrack.bat 더블클릭이 가장 쉽습니다)
  python -m fintrack                       # 화면 서버 실행 후 브라우저 열기 (기본)
  python -m fintrack serve --port 8765 --no-browser
  python -m fintrack run 213500 [--refresh] [--base-year 2027] [--offline]
  python -m fintrack set-workdir D:\\투자\\fintrack
  python -m fintrack set-key               # 인증키 입력(화면에 표시 안 됨) → 자격 증명 관리자에 저장
  python -m fintrack import-sample         # 한솔제지 예시 원본을 작업 폴더로 복사
  python -m fintrack where                 # 작업 폴더·설정 파일·키 저장 위치 확인
"""
import argparse, getpass, json, sys

from . import paths, secret, service


def main():
    ap = argparse.ArgumentParser(prog="fintrack")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--no-browser", action="store_true")
    r = sub.add_parser("run"); r.add_argument("code"); r.add_argument("--base-year", type=int)
    r.add_argument("--refresh", action="store_true"); r.add_argument("--offline", action="store_true"); r.add_argument("--reverify", action="store_true")
    w = sub.add_parser("set-workdir"); w.add_argument("path", nargs="?")
    sub.add_parser("set-key"); sub.add_parser("delete-key"); sub.add_parser("import-sample"); sub.add_parser("where")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if a.cmd in (None, "serve"):
        from .server import serve
        return serve(getattr(a, "port", 8765), not getattr(a, "no_browser", False))
    if a.cmd == "set-workdir":
        d = a.path or paths.pick_folder_dialog()
        if not d:
            sys.exit("폴더를 고르지 않았습니다")
        print("작업 폴더:", paths.set_workdir(d)); return
    if a.cmd == "set-key":
        secret.set_dart_key(getpass.getpass("DART 인증키(입력 내용은 보이지 않습니다): "))
        print("저장했습니다:", secret.where()); return
    if a.cmd == "delete-key":
        secret.delete_dart_key(); print("삭제했습니다"); return
    if a.cmd == "where":
        print(json.dumps({"작업 폴더": paths.load_settings().get("workdir"), "설정 파일": paths.settings_path(),
                          "인증키": secret.where() or "미등록"}, ensure_ascii=False, indent=1)); return
    wd = paths.W(paths.workdir())
    if a.cmd == "import-sample":
        print("가져옴:", service.import_sample(wd)); return
    if a.cmd == "run":
        res = service.analyze(wd, a.code, base_year=a.base_year, refresh=a.refresh, offline=a.offline,
                              reverify=a.reverify, progress=lambda m: print("  -", m), settings=paths.load_settings())
        print("  요약:", json.dumps(res["summary"], ensure_ascii=False))
        print("  화면 파일:", res["export"])


if __name__ == "__main__":
    main()
