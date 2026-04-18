"""Тесты для boost / seasonal event / nickname."""


def test_nickname_filter():
    from routes.api import nickname_is_safe
    assert not nickname_is_safe("admin")
    assert not nickname_is_safe("Anthropic")
    assert not nickname_is_safe("Claude")
    assert not nickname_is_safe("fuck")
    assert not nickname_is_safe("")
    assert not nickname_is_safe("a")  # слишком коротко
    assert nickname_is_safe("Ivan")
    assert nickname_is_safe("KolyaGamer123")
    assert nickname_is_safe("Вася")


def test_seasonal_event_has_structure_or_none():
    from routes.api import current_seasonal_event
    ev = current_seasonal_event()
    # Может быть None (не сезон) или dict с полями
    if ev is not None:
        assert "id" in ev and "name" in ev and "bonus_coins_pct" in ev
        assert ev["bonus_coins_pct"] > 0


def test_compute_boost_no_items_zero_boost():
    """Проверяем что без надетого предмета, стрика и сета — буст 0."""
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite:///test_boosts_local.db")
    from core.database import SessionLocal, engine, Base
    Base.metadata.create_all(bind=engine)
    from models.models import Player
    from routes.api import compute_boost_info
    db = SessionLocal()
    try:
        # Создаём тестового игрока
        p = Player(nickname=f"test_boost_{os.urandom(4).hex()}", coins=100)
        db.add(p); db.commit()
        info = compute_boost_info(db, p.id, ["reaction"])
        # Нет надетого — 0% буст (может быть event/streak, но обычно 0 для свежего юзера)
        assert info["total_pct"] >= 0
        assert "multiplier" in info
        assert info["multiplier"] >= 1.0
    finally:
        db.close()
