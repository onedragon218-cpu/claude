"""키움증권 REST API — 키움 CLI(kiwoomcli, PyPI 'kwcli')를 불러 쓰기 (사용자 지시 2026-11-19, 조회만)

- 앱은 키를 다루지 않는다. 키·토큰은 키움 CLI가 Windows 자격 증명 관리자(OS 키링)와 자기 캐시로 관리한다.
- 허용한 조회 명령만 실행한다(아래 ALLOWED). 주문(orders)·계좌(accounts)·인증(auth)·실시간(streams)은 부르지 않는다.
- 키움 서버는 등록된 공인 IP에서만 받는다(오류 8050). 실패하면 호출한 쪽이 기존 출처로 넘어간다.
- 응답 칸은 원래 코드(cur_prc)와 한글 이름(현재가) 둘 다 읽는다(--named 여부와 무관).
"""
import json
import os
import shutil
import subprocess
import time

ALLOWED = {("domestic", "stocks", "info"), ("domestic", "candles", "daily"), ("domestic", "candles", "by-stock"), ("doctor",)}
_GAP = 0.35          # 호출 사이 최소 간격(초) — 호출 한도(429) 예방
_T = {"last": 0.0}


class KiwoomUnavailable(Exception):
    pass


def find_cli():
    """환경변수 KIWOOM_CLI > PATH > uv 기본 설치 위치(%USERPROFILE%\\.local\\bin)"""
    p = os.environ.get("KIWOOM_CLI")
    if p and os.path.exists(p):
        return p
    p = shutil.which("kiwoomcli")
    if p:
        return p
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    for name in ("kiwoomcli.exe", "kiwoomcli"):
        c = os.path.join(home, ".local", "bin", name)
        if os.path.exists(c):
            return c
    return None


def _call(args, timeout=90):
    """허용한 조회 명령만 실행해 JSON을 돌려준다."""
    if tuple(args[:3]) not in ALLOWED and tuple(args[:1]) not in ALLOWED:
        raise KiwoomUnavailable(f"허용하지 않은 명령: {' '.join(args[:3])}")
    if any(a in ("--confirm", "orders", "accounts", "auth", "streams") for a in args):
        raise KiwoomUnavailable("조회 외 명령은 실행하지 않습니다")
    cli = find_cli()
    if not cli:
        raise KiwoomUnavailable("키움 CLI(kiwoomcli)가 설치되어 있지 않음")
    wait = _T["last"] + _GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW — 자동 수집 때 검은 창이 뜨지 않게
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([cli] + list(args) + ["--format", "json"], capture_output=True, timeout=timeout, env=env, **kw)
    except subprocess.TimeoutExpired:
        raise KiwoomUnavailable("키움 CLI 응답 시간 초과") from None
    except OSError as e:
        raise KiwoomUnavailable(f"키움 CLI 실행 실패: {e}") from None
    finally:
        _T["last"] = time.time()
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").strip()
    try:
        d = json.loads(out) if out else None
    except ValueError:
        d = None
    if r.returncode != 0 or not isinstance(d, dict):
        msg = (err or out or f"종료 코드 {r.returncode}").splitlines()
        msg = next((m for m in msg if "8050" in m or "실패" in m or "오류" in m or "Error" in m), msg[-1] if msg else "")
        raise KiwoomUnavailable(msg[:200] + (" — 키움 홈페이지 「API 사용신청」에 지금 공인 IP를 등록하세요" if "8050" in msg else ""))
    if str(d.get("return_code", "0")) not in ("0", ""):
        raise KiwoomUnavailable(f"키움 응답 {d.get('return_code')}: {d.get('return_msg', '')}"[:200])
    return d


def _g(row, code, name):
    v = row.get(code)
    return row.get(name) if v is None else v


def num(s, absolute=False):
    """'+9810'·'-6220'·'1,480' → 숫자. 앞의 +/-는 등락 표시라 absolute=True면 뗀다."""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-", "+"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return abs(v) if absolute else v


def _list(d, code, name):
    v = d.get(code)
    if v is None:
        v = d.get(name)
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------- 조회
def stock_info(code):
    """종목 기본정보(ka10001). 상장·유통주식 단위 천 주 → 주, 시가총액 억 원"""
    d = _call(["domestic", "stocks", "info", "--code", code])
    sh = num(_g(d, "flo_stk", "상장주식"))
    fl = num(_g(d, "dstr_stk", "유통주식"))
    return {"name": _g(d, "stk_nm", "종목명"), "price": num(_g(d, "cur_prc", "현재가"), True),
            "shares_listed": sh * 1000 if sh else None, "float_shares": fl * 1000 if fl else None,
            "float_ratio": num(_g(d, "dstr_rt", "유통비율")), "foreign_ratio": num(_g(d, "for_exh_rt", "외인소진률"), True),
            "market_cap": num(_g(d, "mac", "시가총액")), "per": num(_g(d, "per", "PER")), "pbr": num(_g(d, "pbr", "PBR")),
            "bps": num(_g(d, "bps", "BPS")), "eps": num(_g(d, "eps", "EPS"))}


def daily(code, date, pages=1, adjusted=True):
    """일봉(ka10081). adjusted=True면 수정주가, False면 수정 전 가격(당시 실제 거래된 종가 — 2026-11-28, 시가총액용).
    반환: {'source','basis','ref','rows': [(YYYYMMDD, 종가, 메모)]}"""
    d = _call(["domestic", "candles", "daily", "--code", code, "--date", date, "--adjusted", "1" if adjusted else "0", "--pages", str(pages)])
    rows = []
    for r in _list(d, "stk_dt_pole_chart_qry", "주식일봉차트조회"):
        dt, c = str(_g(r, "dt", "일자") or ""), num(_g(r, "cur_prc", "현재가"), True)
        if len(dt) == 8 and c:
            rows.append((dt, c, None))
    if not rows:
        raise KiwoomUnavailable("일봉 응답에 값이 없음")
    return {"source": "Kiwoom", "basis": "보통주 종가(키움 수정주가)" if adjusted else "보통주 종가(키움 수정 전 — 당시 실제 종가)",
            "ref": f"키움 REST ka10081 {code}" + ("" if adjusted else " 수정 전"), "rows": rows}


def flows(code, date, pages=1):
    """투자자별 순매수 금액(ka10060, 백만 원). 반환: [{dt, price, foreign, inst, indiv}] 최신순"""
    d = _call(["domestic", "candles", "by-stock", "--date", date, "--code", code, "--basis", "amount", "--side", "net-buy",
               "--unit", "thousand", "--pages", str(pages)])
    out = []
    for r in _list(d, "stk_invsr_orgn_chart", "종목별투자자기관별차트"):
        dt = str(_g(r, "dt", "일자") or "")
        if len(dt) != 8:
            continue
        out.append({"dt": dt, "price": num(_g(r, "cur_prc", "현재가"), True), "foreign": num(_g(r, "frgnr_invsr", "외국인투자자")),
                    "inst": num(_g(r, "orgn", "기관계")), "indiv": num(_g(r, "ind_invsr", "개인투자자"))})
    return sorted(out, key=lambda x: x["dt"], reverse=True)
