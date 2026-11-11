"""같은 와이파이(집 안 네트워크)의 태블릿·휴대폰에서 화면 서버에 접속하기 — https + 비밀번호

- 설정 파일: %APPDATA%\\corp-analyzer\\lan.json (비밀번호 원문은 저장하지 않고 PBKDF2-SHA256 60만 회 해시만 저장)
- 이 PC: 지금처럼 http://localhost:8765 (127.0.0.1에서만 받음, 비밀번호 없음)
- 다른 기기: 켜져 있을 때만 https://PC주소:8766 (암호화, 자체 서명 인증서). 집 안 네트워크 주소일 때만 받고 로그인 필요.
- 로그인: 7일짜리 쿠키(__Host-, Secure, HttpOnly, SameSite=Strict, 서버가 다시 켜지면 다시 로그인).
  같은 기기 5번 틀리면 10분 차단, 모든 기기 합쳐 10분에 20번 틀리면 10분 동안 모든 로그인 잠금.
- 보기 전용(기본): 다른 기기에서는 화면 보기만 되고 수집·갱신·저장 요청은 거부. 설정에서 허용을 고를 수 있다.
- 로그인 성공·실패는 데이터 폴더 logs\\lan_access.log 에 시각·기기 주소와 함께 기록한다(비밀번호는 기록하지 않음).
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time

ITER = 600_000
SESSION_DAYS = 7
MAX_FAIL, LOCK_SEC = 5, 600
GLOBAL_MAX_FAIL = 20
COOKIE = "__Host-ca_session"
HTTPS_PORT = 8766
COMMON = {"12345678", "123456789", "1234567890", "password", "password1", "qwerty123", "11111111", "00000000",
          "abcd1234", "1q2w3e4r", "1q2w3e4r5t", "asdf1234", "qwer1234", "iloveyou", "admin123", "corpanalyzer"}

_sessions = {}           # 토큰 → 만료 시각
_fails = {}              # IP → [실패 횟수, 처음 실패 시각]
_global_fails = []       # 최근 실패 시각들(모든 기기)
_lock = threading.Lock()


def path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "lan.json")


def load():
    try:
        with open(path(), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def enabled():
    d = load()
    return bool(d.get("enabled") and d.get("hash"))


def allow_write():
    return bool(load().get("allow_write"))


def _hash(pw, salt, it=ITER):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), it).hex()


def password_problem(pw):
    """약한 비밀번호면 이유, 괜찮으면 None"""
    if len(pw) < 8:
        return "8자 이상으로 정해 주세요"
    if len(pw) > 128:
        return "128자 이하로 정해 주세요"
    kinds = sum(bool(re.search(rx, pw)) for rx in (r"[A-Za-z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if kinds < 2:
        return "영문·숫자·기호 중 두 가지 이상을 섞어 주세요"
    if pw.lower() in COMMON or len(set(pw)) <= 2:
        return "너무 쉬운 비밀번호입니다"
    if pw.isdigit() or re.fullmatch(r"(.)\1+", pw):
        return "너무 쉬운 비밀번호입니다"
    return None


def _write(d):
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, path())


def enable(password, allow_write=False):
    why = password_problem(password)
    if why:
        raise ValueError(why)
    salt = secrets.token_hex(16)
    _write({"enabled": True, "salt": salt, "hash": _hash(password, salt), "iter": ITER, "allow_write": bool(allow_write),
            "set_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def disable():
    d = load()
    d["enabled"] = False
    _write(d)


def check_password(pw):
    d = load()
    if not d.get("hash") or not pw or len(pw) > 128:
        return False
    return hmac.compare_digest(_hash(pw, d["salt"], int(d.get("iter") or ITER)), d["hash"])


def is_local(ip):
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_loopback
    except ValueError:
        return False


def is_home_network(ip):
    """집 안 네트워크 주소인지(인터넷 쪽 주소는 비밀번호가 맞아도 받지 않음)"""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_private or a.is_link_local


# ---- 기록 ----
def log_access(ip, event):
    try:
        from . import config
        d = os.path.join(config.DATA_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "lan_access.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{ip}\t{event}\n")
    except OSError:
        pass


def recent_log(n=15):
    try:
        from . import config
        with open(os.path.join(config.DATA_DIR, "logs", "lan_access.log"), encoding="utf-8") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


# ---- 로그인 ----
def locked_out(ip):
    now = time.time()
    with _lock:
        _global_fails[:] = [t for t in _global_fails if now - t < LOCK_SEC]
        if len(_global_fails) >= GLOBAL_MAX_FAIL:
            return "모든 기기"
        n, t0 = _fails.get(ip, (0, 0))
        if n >= MAX_FAIL and now - t0 < LOCK_SEC:
            return "이 기기"
        if now - t0 >= LOCK_SEC:
            _fails.pop(ip, None)
        return None


def login(ip, pw):
    """맞으면 새 토큰, 틀리면 None"""
    if locked_out(ip):
        log_access(ip, "차단 중 로그인 시도")
        return None
    if check_password(pw):
        tok = secrets.token_urlsafe(32)
        with _lock:
            _sessions[tok] = time.time() + SESSION_DAYS * 86400
            _fails.pop(ip, None)
        log_access(ip, "로그인 성공")
        return tok
    with _lock:
        n, t0 = _fails.get(ip, (0, time.time()))
        _fails[ip] = (n + 1, t0)
        _global_fails.append(time.time())
    log_access(ip, f"비밀번호 틀림({n + 1}회)")
    time.sleep(1)            # 비밀번호 대입을 느리게
    return None


def valid(tok):
    if not tok:
        return False
    with _lock:
        exp = _sessions.get(tok)
        if exp and exp > time.time():
            return True
        _sessions.pop(tok, None)
    return False


def logout(tok):
    with _lock:
        _sessions.pop(tok, None)


def cookie_token(header):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def local_addresses():
    """태블릿에서 열 주소 후보(이 PC의 집 안 네트워크 IPv4)"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))          # 실제로 보내지 않음(경로만 확인)
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if is_home_network(ip) and not is_local(ip))


LOGIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>재무분석 로그인</title><style>
body{font-family:system-ui,-apple-system,'Malgun Gothic',sans-serif;background:#f6f6f4;color:#222;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
@media (prefers-color-scheme:dark){body{background:#1c1c1b;color:#eee}.box{background:#262624!important;border-color:#444!important}input{background:#1c1c1b;color:#eee;border-color:#555!important}}
.box{background:#fff;border:1px solid #ddd;border-radius:12px;padding:28px 24px;width:min(360px,calc(100% - 32px))}
h1{font-size:20px;margin:0 0 6px}p{color:#777;font-size:14px;margin:0 0 16px}
input{font:inherit;width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:8px;margin-bottom:12px}
button{font:inherit;width:100%;padding:12px;border:0;border-radius:8px;background:#2a78d6;color:#fff;cursor:pointer}
.err{color:#cf222e;font-size:14px;margin:0 0 12px}</style></head><body>
<form class="box" method="post" action="/login"><h1>상장기업 재무분석</h1><p>같은 와이파이의 기기에서 접속하려면 비밀번호를 넣으세요. 연결은 암호화(https)되어 있습니다.</p>
__ERR__<input type="password" name="pw" placeholder="비밀번호" autofocus autocomplete="current-password">
<input type="hidden" name="next" value="__NEXT__"><button>들어가기</button></form></body></html>"""
