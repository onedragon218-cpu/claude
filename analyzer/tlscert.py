"""태블릿 접속용 https 인증서를 이 PC에서 직접 만든다(표준 라이브러리만, 외부 프로그램 없음).

- RSA 2048비트 키 + 자체 서명 X.509 v3 인증서(SHA-256), 유효기간 825일(아이패드가 신뢰 설정을 허용하는 최대 기간).
- 인증서에 이 PC의 집 안 네트워크 주소(IP)와 localhost를 넣는다.
- 키·인증서는 %APPDATA%\\corp-analyzer\\tls\\ 에 저장(키 파일은 이 PC 사용자만 읽도록 권한을 줄임).
- 자체 서명이므로 태블릿 브라우저가 처음에 '안전하지 않음' 경고를 띄운다. 설정 창에 나오는 지문(SHA-256)과
  브라우저 인증서 정보의 지문이 같은지 확인한 뒤 진행하면 된다.
"""
import base64
import datetime as dt
import hashlib
import ipaddress
import os
import secrets

E = 65537
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2; r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if c % E != 1 and _is_probable_prime(c):
            return c


def gen_rsa(bits=2048):
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        d = pow(E, -1, phi)
        return {"n": n, "e": E, "d": d, "p": p, "q": q, "dp": d % (p - 1), "dq": d % (q - 1), "qi": pow(q, -1, p)}


# ---- DER(ASN.1) ----
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(v):
    b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big")   # 맨 앞 비트가 1이면 0x00을 붙여 양수로
    return _tlv(0x02, b)


def _seq(*parts):
    return _tlv(0x30, b"".join(parts))


def _set(*parts):
    return _tlv(0x31, b"".join(parts))


def _oid(s):
    a = [int(x) for x in s.split(".")]
    out = bytes([40 * a[0] + a[1]])
    for v in a[2:]:
        enc = [v & 0x7F]
        v >>= 7
        while v:
            enc.insert(0, 0x80 | (v & 0x7F)); v >>= 7
        out += bytes(enc)
    return _tlv(0x06, out)


NULL = b"\x05\x00"
SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), NULL)


def _time(t):
    if t.year < 2050:
        return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


def _name(cn):
    return _seq(_set(_seq(_oid("2.5.4.3"), _tlv(0x0C, cn.encode("utf-8")))))


def _sign(k, data):
    di = _seq(_seq(_oid("2.16.840.1.101.3.4.2.1"), NULL), _tlv(0x04, hashlib.sha256(data).digest()))
    klen = (k["n"].bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (klen - len(di) - 3) + b"\x00" + di
    m = int.from_bytes(em, "big")
    m1, m2 = pow(m, k["dp"], k["p"]), pow(m, k["dq"], k["q"])       # 중국인 나머지 정리로 빠르게
    s = m2 + k["q"] * ((k["qi"] * (m1 - m2)) % k["p"])
    return s.to_bytes(klen, "big")


def make_cert(k, ips, cn="CorpAnalyzer (이 PC)", days=825):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    spki = _seq(_seq(_oid("1.2.840.113549.1.1.1"), NULL), _tlv(0x03, b"\x00" + _seq(_int(k["n"]), _int(k["e"]))))
    san = [_tlv(0x82, b"localhost")]
    for ip in ips:
        san.append(_tlv(0x87, ipaddress.ip_address(ip).packed))
    exts = _seq(
        _seq(_oid("2.5.29.19"), _tlv(0x01, b"\xff"), _tlv(0x04, _seq(_tlv(0x01, b"\xff")))),              # CA:TRUE(자체 서명)
        _seq(_oid("2.5.29.15"), _tlv(0x01, b"\xff"), _tlv(0x04, _tlv(0x03, b"\x02\xa4"))),                # digitalSignature(0)·keyEncipherment(2)·keyCertSign(5) → 6비트 사용, 남는 비트 2
        _seq(_oid("2.5.29.37"), _tlv(0x04, _seq(_oid("1.3.6.1.5.5.7.3.1")))),                            # serverAuth
        _seq(_oid("2.5.29.17"), _tlv(0x04, _seq(*san))),                                                 # 주소(SAN)
    )
    tbs = _seq(_tlv(0xA0, _int(2)), _int(int.from_bytes(secrets.token_bytes(16), "big") >> 1), SHA256_RSA, _name(cn),
               _seq(_time(now - dt.timedelta(days=1)), _time(now + dt.timedelta(days=days))), _name(cn), spki,
               _tlv(0xA3, exts))
    return _seq(tbs, SHA256_RSA, _tlv(0x03, b"\x00" + _sign(k, tbs)))


def _pem(der, label):
    b = base64.b64encode(der).decode()
    return f"-----BEGIN {label}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {label}-----\n"


def key_der(k):
    return _seq(_int(0), _int(k["n"]), _int(k["e"]), _int(k["d"]), _int(k["p"]), _int(k["q"]), _int(k["dp"]), _int(k["dq"]), _int(k["qi"]))


def folder():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corp-analyzer", "tls")


def paths():
    d = folder()
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")


def fingerprint(cert_pem_path=None):
    cp, _ = paths()
    with open(cert_pem_path or cp, encoding="ascii") as f:
        der = base64.b64decode("".join(l for l in f.read().splitlines() if not l.startswith("-----")))
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def ensure(ips, force=False):
    """인증서가 없거나 force면 새로 만든다. 반환: (cert, key, 새로 만들었는지)"""
    cp, kp = paths()
    if not force and os.path.exists(cp) and os.path.exists(kp):
        return cp, kp, False
    os.makedirs(folder(), exist_ok=True)
    k = gen_rsa()
    with open(cp, "w", encoding="ascii") as f:
        f.write(_pem(make_cert(k, ips), "CERTIFICATE"))
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(_pem(key_der(k), "RSA PRIVATE KEY"))
    return cp, kp, True
