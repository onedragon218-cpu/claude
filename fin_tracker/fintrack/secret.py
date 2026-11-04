"""DART 인증키 보관.

Windows: '자격 증명 관리자'(Windows 자격 증명)에 저장한다. 파일·화면·로그에는 남기지 않는다.
확인 방법: 제어판 → 자격 증명 관리자 → Windows 자격 증명 → 'fintrack' 항목.
보관소를 쓸 수 없는 환경에서는 환경변수 DART_API_KEY만 읽는다(저장하지 않음).
"""
import os

SERVICE, NAME = "fintrack", "DART_API_KEY"


def _kr():
    try:
        import keyring
        from keyring.backends import fail
        k = keyring.get_keyring()
        if isinstance(k, fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def get_dart_key():
    kr = _kr()
    if kr:
        try:
            v = kr.get_password(SERVICE, NAME)
            if v:
                return v
        except Exception:
            pass
    return os.environ.get("DART_API_KEY") or None


def set_dart_key(value):
    value = (value or "").strip()
    if len(value) != 40 or not value.isalnum():
        raise ValueError("DART 인증키는 영문·숫자 40자입니다.")
    kr = _kr()
    if not kr:
        raise RuntimeError("이 PC에서는 자격 증명 보관소를 쓸 수 없습니다. 'pip install keyring' 후 다시 시도하세요.")
    kr.set_password(SERVICE, NAME, value)


def delete_dart_key():
    kr = _kr()
    if kr:
        try:
            kr.delete_password(SERVICE, NAME)
        except Exception:
            pass


def masked():
    k = get_dart_key()
    return None if not k else k[:4] + "…" + k[-2:]


def where():
    if _kr() and _kr().get_password(SERVICE, NAME):
        return "Windows 자격 증명 관리자"
    if os.environ.get("DART_API_KEY"):
        return "환경변수"
    return None
