"""Тесты для ranked-tier calculation."""


def test_tier_bronze_at_base():
    from routes.api import compute_tier
    t = compute_tier(1000)
    assert t["tier"] == "Silver", f"at 1000 ELO expected Silver but got {t['tier']}"


def test_tier_grandmaster():
    from routes.api import compute_tier
    t = compute_tier(2500)
    assert t["tier"] == "Grandmaster"
    assert t["division"] == ""  # GM без дивизионов


def test_tier_bronze_low():
    from routes.api import compute_tier
    t = compute_tier(500)
    assert t["tier"] == "Bronze"


def test_tier_divisions():
    from routes.api import compute_tier
    # На 1000 ELO — Silver. Дивизион в начале — III.
    t1 = compute_tier(900)  # нижняя граница Silver
    assert t1["division"] == "III"
    t2 = compute_tier(1050)  # ~75% через Silver (Silver: 900-1100)
    assert t2["division"] in ("II", "I")


def test_tier_progress_pct():
    from routes.api import compute_tier
    t = compute_tier(1000)  # Silver, середина
    assert 40 <= t["progress_pct"] <= 60
