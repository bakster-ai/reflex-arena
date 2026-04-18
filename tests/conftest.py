"""Pytest fixtures для integration-тестов.
Каждый тест получает свежую SQLite-БД в temp-папке.
"""
import os
import tempfile
import pytest


@pytest.fixture(scope="session", autouse=True)
def _setup_env():
    # Временная БД + отключаем scheduler в тестах
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    os.environ["DISABLE_SCHEDULER"] = "1"
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only"
    yield
    try:
        os.remove(tmp.name)
    except Exception:
        pass


@pytest.fixture
def client():
    """TestClient поверх FastAPI app."""
    from fastapi.testclient import TestClient
    # Импорт после setup_env чтобы подцепился правильный DATABASE_URL
    import importlib
    # Принудительно переимпортируем модули зависящие от env
    for m in ("core.database", "main"):
        if m in list(__import__("sys").modules.keys()):
            del __import__("sys").modules[m]
    from main import app
    with TestClient(app) as c:
        yield c


def _auth_guest(client):
    """Хелпер: создать гостя, вернуть (token, player_id, nickname).
    При исчерпании rate-limit гостей — переходим на TG dev-auth."""
    r = client.post("/api/auth/guest")
    data = r.json()
    if "token" in data:
        return data["token"], data.get("player_id"), data.get("nickname")
    # Fallback: TG dev-auth (не бьёт лимит guest)
    import random
    tg_id = random.randint(100000, 9999999)
    r2 = client.post("/api/auth/telegram",
                     json={"init_data": "x=1", "tg_user": {"id": tg_id, "username": f"dev{tg_id}"}})
    d2 = r2.json()
    return d2.get("token"), d2.get("player_id"), d2.get("nickname")
