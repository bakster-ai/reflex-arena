"""Integration-тесты на критические API-flow."""
from tests.conftest import _auth_guest


def test_guest_auth_creates_player(client):
    r = client.post("/api/auth/guest")
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert data.get("player_id")


def test_me_returns_gems_and_tier(client):
    token, _, _ = _auth_guest(client)
    r = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    me = r.json()
    assert me["authenticated"] is True
    assert "gems" in me
    assert "tier" in me
    assert me["tier"]["tier"] in ("Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master", "Grandmaster")


def test_tiers_list(client):
    r = client.get("/api/ranked/tiers")
    assert r.status_code == 200
    tiers = r.json()["tiers"]
    names = [t["name"] for t in tiers]
    assert names == ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master", "Grandmaster"]


def test_weekly_event(client):
    r = client.get("/api/weekly_event")
    assert r.status_code == 200
    e = r.json()
    assert "category" in e
    assert e["category"] in ("reaction", "logic", "memory", "coordination", "trivia")


def test_seasonal_event_endpoint(client):
    r = client.get("/api/seasonal_event")
    assert r.status_code == 200
    # event может быть None вне сезона
    assert "event" in r.json()


def test_gems_shop(client):
    r = client.get("/api/gems/shop")
    assert r.status_code == 200
    data = r.json()
    assert "spend_items" in data and len(data["spend_items"]) >= 3
    assert "buy_packs" in data and len(data["buy_packs"]) >= 2


def test_cases_list_unauthenticated(client):
    r = client.get("/api/cases")
    assert r.status_code == 200
    cases = r.json()["cases"]
    assert len(cases) == 5  # 5 категорий


def test_shop_themes_requires_no_auth_for_list(client):
    r = client.get("/api/shop/themes")
    assert r.status_code == 200
    assert "themes" in r.json()


def test_tournament_current_creates_on_demand(client):
    r = client.get("/api/tournament/current")
    assert r.status_code == 200
    t = r.json()
    assert "week_key" in t
    assert "status" in t
    assert t["entry_coins"] == 100


def test_report_player_requires_auth(client):
    r = client.post("/api/report_player", json={"nickname": "someone"})
    # Без токена — 200 но ok=False
    data = r.json()
    assert data.get("ok") is False


def test_report_player_with_auth(client):
    """Создаём 2 игроков (через TG dev-auth, чтобы не съесть rate-limit guest) и шлём репорт."""
    # Главный юзер — через guest (всё равно рано, лимит ещё не съеден)
    r1 = client.post("/api/auth/guest")
    token1 = r1.json().get("token")
    assert token1, f"guest auth failed: {r1.json()}"
    # Второй юзер через TG dev — не съедает лимит гостей
    r2 = client.post("/api/auth/telegram",
                     json={"init_data": "x=1", "tg_user": {"id": 9999001, "username": "tgtest1"}})
    data2 = r2.json()
    assert data2.get("ok") and data2.get("token"), f"TG dev auth failed: {data2}"
    target_nick = data2["nickname"]
    # Отправляем жалобу
    r = client.post("/api/report_player",
                    json={"nickname": target_nick, "reason": "cheating"},
                    headers={"Authorization": f"Bearer {token1}"})
    assert r.json().get("ok") is True


def test_rate_limit_guest_auth(client):
    # Дёргаем /api/auth/guest много раз — должен хотя бы раз дать 429
    statuses = []
    for _ in range(10):
        r = client.post("/api/auth/guest")
        statuses.append(r.status_code)
    assert 429 in statuses, f"expected 429 in {statuses}"


def test_titles_endpoint(client):
    token, _, _ = _auth_guest(client)
    r = client.get("/api/titles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["authenticated"] is True
    assert "titles" in data
    assert len(data["titles"]) >= 10


def test_admin_without_token(client):
    r = client.get("/api/admin/dashboard")
    data = r.json()
    assert data.get("ok") is False


def test_correlation_id_header(client):
    r = client.get("/api/weekly_event")
    # Проверяем что middleware добавил X-Request-ID
    assert "X-Request-ID" in r.headers or "x-request-id" in r.headers
