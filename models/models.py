from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.database import Base


class Player(Base):
    __tablename__ = "players"

    id          = Column(Integer, primary_key=True, index=True)
    nickname    = Column(String, nullable=False, unique=True)
    xp          = Column(Integer, default=0)
    coins       = Column(Integer, default=0)
    gems        = Column(Integer, default=0)  # премиум-валюта

    # Общий ELO Reflex Arena
    reflex_elo  = Column(Float, default=1000.0)
    reflex_wins = Column(Integer, default=0)
    reflex_losses = Column(Integer, default=0)

    # Категорийные ELO (5 категорий)
    elo_reaction     = Column(Float, default=1000.0)
    elo_logic        = Column(Float, default=1000.0)
    elo_memory       = Column(Float, default=1000.0)
    elo_coordination = Column(Float, default=1000.0)
    elo_trivia       = Column(Float, default=1000.0)

    # Рефералка
    referred_by            = Column(Integer, ForeignKey("players.id"), nullable=True)
    referral_bonus_claimed = Column(Boolean, default=False)

    # Гостевой режим
    is_guest         = Column(Boolean, default=False)
    reflex_onboarded = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PlayerPassword(Base):
    __tablename__ = "player_passwords"

    id            = Column(Integer, primary_key=True, index=True)
    player_id     = Column(Integer, ForeignKey("players.id"), nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class ReflexMatch(Base):
    __tablename__ = "reflex_matches"

    id             = Column(Integer, primary_key=True, index=True)
    p1_id          = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    p2_id          = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    winner_id      = Column(Integer, ForeignKey("players.id"), nullable=True)
    rounds_p1      = Column(Integer, default=0)
    rounds_p2      = Column(Integer, default=0)
    stake_coins    = Column(Integer, default=0)
    elo_change_p1  = Column(Float, default=0.0)
    elo_change_p2  = Column(Float, default=0.0)
    rounds_log     = Column(JSON, nullable=True)
    status         = Column(String, default="active")
    started_at     = Column(DateTime(timezone=True), server_default=func.now())
    finished_at    = Column(DateTime(timezone=True), nullable=True)


class ReflexAchievement(Base):
    """Универсальная таблица — хранит достижения, владения скинами/темами/аватарами/VFX/предметами/экипировки.
    Код формата: 'first_win' / 'theme_owned_neon' / 'vfx_equipped_fire' / 'item_owned_reaction_gloves_7' etc.
    """
    __tablename__ = "reflex_achievements"

    id          = Column(Integer, primary_key=True, index=True)
    player_id   = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    code        = Column(String, nullable=False)
    unlocked_at = Column(DateTime(timezone=True), server_default=func.now())


class ReflexDailyTask(Base):
    __tablename__ = "reflex_daily_tasks"

    id          = Column(Integer, primary_key=True, index=True)
    player_id   = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    date        = Column(String, nullable=False)
    code        = Column(String, nullable=False)
    progress    = Column(Integer, default=0)
    target      = Column(Integer, nullable=False)
    reward_coins = Column(Integer, default=0)
    claimed     = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class ReflexDailyChallenge(Base):
    __tablename__ = "reflex_daily_challenges"

    id         = Column(Integer, primary_key=True, index=True)
    player_id  = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    date       = Column(String, nullable=False)
    game       = Column(String, nullable=False)
    best_score = Column(Integer, default=0)
    attempts   = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReflexLoginStreak(Base):
    """Ежедневный login streak. Один ряд на игрока."""
    __tablename__ = "reflex_login_streaks"

    id               = Column(Integer, primary_key=True, index=True)
    player_id        = Column(Integer, ForeignKey("players.id"), nullable=False, unique=True, index=True)
    current_streak   = Column(Integer, default=0)
    max_streak       = Column(Integer, default=0)
    last_login_date  = Column(String, nullable=True)   # YYYY-MM-DD
    last_claimed_date = Column(String, nullable=True)  # YYYY-MM-DD
    total_days_logged = Column(Integer, default=0)


class ReflexEvent(Base):
    """Продуктовая аналитика. Event-log."""
    __tablename__ = "reflex_events"

    id         = Column(Integer, primary_key=True, index=True)
    player_id  = Column(Integer, ForeignKey("players.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    payload    = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ReflexPushSubscription(Base):
    """Web Push subscriptions (PWA push)."""
    __tablename__ = "reflex_push_subscriptions"

    id          = Column(Integer, primary_key=True, index=True)
    player_id   = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    endpoint    = Column(String, nullable=False, unique=True)
    keys_json   = Column(JSON, nullable=False)  # {auth, p256dh}
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class ReflexClub(Base):
    __tablename__ = "reflex_clubs"

    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String, nullable=False, unique=True)
    tag            = Column(String(5), nullable=False, unique=True)
    owner_id       = Column(Integer, ForeignKey("players.id"), nullable=False)
    description    = Column(String, nullable=True)
    icon           = Column(String, default="🏰")
    member_count   = Column(Integer, default=1)
    total_wins     = Column(Integer, default=0)
    total_matches  = Column(Integer, default=0)
    rating         = Column(Float, default=0.0)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


class ReflexClubMember(Base):
    __tablename__ = "reflex_club_members"

    id            = Column(Integer, primary_key=True, index=True)
    club_id       = Column(Integer, ForeignKey("reflex_clubs.id"), nullable=False, index=True)
    player_id     = Column(Integer, ForeignKey("players.id"), nullable=False, unique=True, index=True)
    role          = Column(String, default="member")
    contribution  = Column(Integer, default=0)
    joined_at     = Column(DateTime(timezone=True), server_default=func.now())


class ReflexTournament(Base):
    __tablename__ = "reflex_tournaments"

    id          = Column(Integer, primary_key=True, index=True)
    week_key    = Column(String, nullable=False, unique=True)
    status      = Column(String, default="open")
    bracket     = Column(JSON, nullable=True)
    winner_id   = Column(Integer, ForeignKey("players.id"), nullable=True)
    started_at  = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class ReflexTournamentSignup(Base):
    __tablename__ = "reflex_tournament_signups"

    id                   = Column(Integer, primary_key=True, index=True)
    tournament_id        = Column(Integer, ForeignKey("reflex_tournaments.id"), nullable=False, index=True)
    player_id            = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    seed                 = Column(Integer, nullable=True)
    eliminated_at_round  = Column(Integer, nullable=True)
    final_rank           = Column(Integer, nullable=True)
    joined_at            = Column(DateTime(timezone=True), server_default=func.now())


class ReflexPayment(Base):
    __tablename__ = "reflex_payments"

    id            = Column(Integer, primary_key=True, index=True)
    player_id     = Column(Integer, ForeignKey("players.id"), nullable=True, index=True)
    provider      = Column(String, nullable=False)  # 'tg_stars', 'yookassa', 'dev'
    external_id   = Column(String, nullable=True, index=True)
    product_id    = Column(String, nullable=False)
    amount_minor  = Column(Integer, default=0)  # minor units (копейки/stars)
    currency      = Column(String, default="XTR")  # XTR = Telegram Stars
    status        = Column(String, default="pending")  # pending/completed/failed
    gems_granted  = Column(Integer, default=0)
    payload       = Column(JSON, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    completed_at  = Column(DateTime(timezone=True), nullable=True)


class ReflexFriend(Base):
    __tablename__ = "reflex_friends"

    id         = Column(Integer, primary_key=True, index=True)
    player_id  = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    friend_id  = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    status     = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ReflexSeason(Base):
    __tablename__ = "reflex_seasons"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String, nullable=False)
    start_at    = Column(DateTime(timezone=True), server_default=func.now())
    end_at      = Column(DateTime(timezone=True), nullable=False)
    status      = Column(String, default="active")
    finished_at = Column(DateTime(timezone=True), nullable=True)


class ReflexPassProgress(Base):
    __tablename__ = "reflex_pass_progress"

    id                     = Column(Integer, primary_key=True, index=True)
    player_id              = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    season_id              = Column(Integer, ForeignKey("reflex_seasons.id"), nullable=False, index=True)
    xp                     = Column(Integer, default=0)
    level                  = Column(Integer, default=0)
    premium                = Column(Boolean, default=False)
    claimed_levels_free    = Column(JSON, default=list)
    claimed_levels_premium = Column(JSON, default=list)
    updated_at             = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReflexSeasonReward(Base):
    __tablename__ = "reflex_season_rewards"

    id          = Column(Integer, primary_key=True, index=True)
    player_id   = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    season_id   = Column(Integer, ForeignKey("reflex_seasons.id"), nullable=False, index=True)
    rank        = Column(Integer, nullable=False)
    final_elo   = Column(Float, nullable=False)
    coins_given = Column(Integer, default=0)
    claimed     = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
