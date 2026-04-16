import os
import hashlib
import hmac
import base64
import json
import time
import secrets
import logging

logger = logging.getLogger(__name__)

# JWT_SECRET: должен быть установлен через env. Если нет — используем дефолт
# (для обратной совместимости со старыми токенами), но логируем предупреждение.
_DEFAULT_SECRET = "reflex_arena_default_dev_secret_CHANGE_IN_PROD"
SECRET = os.environ.get("JWT_SECRET", _DEFAULT_SECRET)
if SECRET == _DEFAULT_SECRET:
    logger.warning(
        "WARNING: JWT_SECRET environment variable not set. Using default "
        "secret. Set JWT_SECRET in production for security!"
    )


def _hash_with_salt(password: str, salt: str) -> str:
    """SHA-256 c солью. Формат хранения: 'salt$hash'."""
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def hash_password(password: str, salt: str = None) -> str:
    """Хеширует пароль с новой солью. Используется при создании/смене пароля."""
    if salt is None:
        salt = secrets.token_hex(16)  # 32 символа
    return _hash_with_salt(password, salt)


def verify_password(password: str, stored: str) -> bool:
    """Проверяет пароль. Поддерживает старый формат (просто sha256) и новый (salt$hash)."""
    if not stored:
        return False
    try:
        if "$" in stored:
            # Новый формат с солью
            salt, _ = stored.split("$", 1)
            expected = _hash_with_salt(password, salt)
            return hmac.compare_digest(stored, expected)
        else:
            # Старый формат: голый sha256 — для обратной совместимости
            expected = hashlib.sha256(password.encode()).hexdigest()
            return hmac.compare_digest(stored, expected)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    """True, если сохранённый хеш в старом формате и его надо обновить."""
    return bool(stored) and "$" not in stored


def make_token(player_id: int, nickname: str) -> str:
    payload = json.dumps({
        "player_id": player_id,
        "nickname": nickname,
        "exp": int(time.time()) + 86400 * 30
    })
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_token(token: str):
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64 + "==").decode())
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None
