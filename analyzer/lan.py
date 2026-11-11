"""같은 와이파이(집 안 네트워크)의 태블릿·휴대폰에서 화면 서버에 접속하기 — 비밀번호 필요

- 설정 파일: %APPDATA%\\corp-analyzer\\lan.json (비밀번호 원문은 저장하지 않고 PBKDF2-SHA256 해시만 저장)
- 켜져 있으면 서버가 모든 네트워크 카드(0.0.0.0)에서 접속을 받는다. 꺼져 있으면 지금처럼 이 PC(127.0.0.1)만.
- 이 PC 자신(127.0.0.1)의 접속은 비밀번호 없이 그대로 쓴다(바탕화면 바로가기·자동 수집).
- 다른 기기는 집 안 네트워크 주소(10.x, 172.16~31.x, 192.168.x, 169.254.x, IPv6 fe80·fc/fd)일 때만 받고,
  로그인 후 30일짜리 쿠키(서버가 다시 켜지면 다시 로그인)로 접속한다. 비밀번호 5번 틀리면 그 기기는 10분간 막는다.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time

ITER = 200_000
SESSION_DAYS = 30
MAX_FAIL, LOCK_SEC = 5, 600
COOKIE = "ca_session"

_sessions = {}           # 토큰 → 만료 시각
_fails = {}              # IP → [실패 횟수, 처음 실패 시각]
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


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), ITER).hex()


def enable(password):
    if len(password) < 6:
        raise ValueError("비밀번호는 6자 이상으로 정해 주세요")
    salt = secrets.token_hex(16)
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "w", encoding="utf-8") as f:
        json.dump({"enabled": True, "salt": salt, "hash": _hash(password, salt), "iter": ITER}, f)


def disable():
    d = load()
    d["enabled"] = False
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "w", encoding="utf-8") as f:
        json.dump(d, f)


def check_password(pw):
    d = load()
    if not d.get("hash"):
        return False
    return hmac.compare_digest(_hash(pw or "", d["salt"]), d["hash"])


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


# ---- 로그인 ----
def locked_out(ip):
    with _lock:
        n, t0 = _fails.get(ip, (0, 0))
        if n >= MAX_FAIL and time.time() - t0 < LOCK_SEC:
            return True
        if time.time() - t0 >= LOCK_SEC:
            _fails.pop(ip, None)
        return False


def login(ip, pw):
    """맞으면 새 토큰, 틀리면 None"""
    if locked_out(ip):
        return None
    if check_password(pw):
        tok = secrets.token_urlsafe(32)
        with _lock:
            _sessions[tok] = time.time() + SESSION_DAYS * 86400
            _fails.pop(ip, None)
        return tok
    with _lock:
        n, t0 = _fails.get(ip, (0, time.time()))
        _fails[ip] = (n + 1, t0)
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
<form class="box" method="post" action="/login"><h1>상장기업 재무분석</h1><p>같은 와이파이의 기기에서 접속하려면 비밀번호를 넣으세요.</p>
__ERR__<input type="password" name="pw" placeholder="비밀번호" autofocus autocomplete="current-password">
<input type="hidden" name="next" value="__NEXT__"><button>들어가기</button></form></body></html>"""
