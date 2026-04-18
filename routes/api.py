"""
Reflex Arena — HTTP endpoints.
  GET /api/reflex/leaderboard         — топ по reflex_elo
  GET /api/reflex/me                  — моя статистика + последние матчи
  GET /api/reflex/matches             — последние матчи на платформе
  GET /share/{match_id}        — share-страница с OG-тегами (ссылка для шеринга)
  GET /share/{match_id}/og.png — PNG для OG-изображения
"""
import html
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from core.database import get_db
from core.auth import verify_token
from datetime import datetime, timezone
import random as _rnd
from models.models import (
    Player, ReflexMatch, ReflexAchievement, ReflexDailyTask, ReflexDailyChallenge,
    ReflexFriend, ReflexSeason, ReflexPassProgress, ReflexSeasonReward,
    ReflexLoginStreak, ReflexEvent, ReflexPushSubscription,
    ReflexClub, ReflexClubMember, ReflexTournament, ReflexTournamentSignup,
    ReflexPayment,
)
from datetime import timedelta

router = APIRouter(prefix="/api", tags=["reflex"])
share_router = APIRouter(tags=["reflex-share"])

REFERRAL_BONUS = 100  # coins, обоим

# ═══════════════════════════════════════════════════════════════
#   RANKED TIERS
# ═══════════════════════════════════════════════════════════════
TIERS = [
    # name, short, icon, color, min_elo
    ("Bronze",    "Бронза",       "🥉", "#cd7f32",   0),
    ("Silver",    "Серебро",      "🥈", "#c0c0c0",  900),
    ("Gold",      "Золото",       "🥇", "#ffd700", 1100),
    ("Platinum",  "Платина",      "💠", "#7dd3fc", 1300),
    ("Diamond",   "Алмаз",        "💎", "#a78bfa", 1500),
    ("Master",    "Мастер",        "👑", "#ff7a29", 1800),
    ("Grandmaster","Грандмастер", "🌟", "#ff4081", 2100),
]

PLACEMENT_MATCHES = 5
RANKED_SEASON_LENGTH_DAYS = 90
SEASON_SOFT_RESET_PCT = 25


def compute_tier(elo: float) -> dict:
    """Возвращает {tier, tier_ru, icon, color, division(1-3), next_tier_elo, progress_pct}."""
    elo = float(elo or 1000.0)
    tier_idx = 0
    for i, t in enumerate(TIERS):
        if elo >= t[4]:
            tier_idx = i
    name, name_ru, icon, color, min_elo = TIERS[tier_idx]
    # Дивизион (III → II → I) внутри тира
    next_min = TIERS[tier_idx + 1][4] if tier_idx + 1 < len(TIERS) else min_elo + 300
    span = max(1, next_min - min_elo)
    progress = max(0.0, min(1.0, (elo - min_elo) / span))
    # 3 дивизиона: progress 0-33% = III, 33-66% = II, 66-100% = I
    if progress < 0.33:
        div = "III"
    elif progress < 0.66:
        div = "II"
    else:
        div = "I"
    if tier_idx == len(TIERS) - 1:
        div = ""  # Grandmaster — без дивизионов
    return {
        "tier": name,
        "tier_ru": name_ru,
        "icon": icon,
        "color": color,
        "division": div,
        "min_elo": min_elo,
        "next_tier_elo": next_min,
        "progress_pct": round(progress * 100, 1),
    }


# ═══════════════════════════════════════════════════════════════
#   СЕЗОННЫЕ СОБЫТИЯ (хэллоуин, НГ, валентинки, лето)
# ═══════════════════════════════════════════════════════════════

def current_seasonal_event() -> Optional[dict]:
    """Определяем событие по текущей дате."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    m, d = now.month, now.day
    if (m == 10 and d >= 20) or (m == 11 and d <= 3):
        return {"id": "halloween", "name": "Хэллоуин 🎃",
                "banner_color": "#ff7a29",
                "bonus_coins_pct": 15,
                "theme": "orange"}
    if (m == 12 and d >= 20) or (m == 1 and d <= 10):
        return {"id": "newyear", "name": "Новый Год ⛄",
                "banner_color": "#7dd3fc",
                "bonus_coins_pct": 20,
                "theme": "winter"}
    if m == 2 and 10 <= d <= 17:
        return {"id": "valentines", "name": "День всех влюблённых 💖",
                "banner_color": "#ff4081",
                "bonus_coins_pct": 15,
                "theme": "pink"}
    if m == 7:
        return {"id": "summer", "name": "Летний марафон ☀️",
                "banner_color": "#ffd740",
                "bonus_coins_pct": 10,
                "theme": "beach"}
    return None


# Каталог дневных задач — каждое утро выбирается случайные 3
DAILY_TASK_CATALOG = [
    {"code": "win_3",     "title": "Выиграй 3 матча",                 "target": 3,   "reward": 60,  "icon": "🏆"},
    {"code": "play_5",    "title": "Сыграй 5 матчей",                 "target": 5,   "reward": 40,  "icon": "🎮"},
    {"code": "stake_win", "title": "Выиграй матч со ставкой",         "target": 1,   "reward": 80,  "icon": "💰"},
    {"code": "ai_3",      "title": "Побей 3 раза бота",               "target": 3,   "reward": 50,  "icon": "🤖"},
    {"code": "perfect",   "title": "Выиграй матч 3:0",                "target": 1,   "reward": 100, "icon": "💯"},
    {"code": "games_4",   "title": "Сыграй в 4 разных мини-игры",     "target": 4,   "reward": 60,  "icon": "🎭"},
    {"code": "elo_up",    "title": "Повысь ELO на 20+",               "target": 20,  "reward": 70,  "icon": "📈"},
    {"code": "score_3k",  "title": "Набери 3000+ очков в мини-игре",  "target": 3000,"reward": 50,  "icon": "🎯"},
]


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_daily_tasks(db: Session, player_id: int):
    """Генерит 3 задачи если на сегодня их ещё нет."""
    today = _today_str()
    existing = db.query(ReflexDailyTask).filter(
        ReflexDailyTask.player_id == player_id,
        ReflexDailyTask.date == today,
    ).count()
    if existing >= 3:
        return
    need = 3 - existing
    # Не повторяем коды на сегодня
    used_codes = {r.code for r in db.query(ReflexDailyTask).filter(
        ReflexDailyTask.player_id == player_id, ReflexDailyTask.date == today,
    ).all()}
    avail = [t for t in DAILY_TASK_CATALOG if t["code"] not in used_codes]
    _rnd.shuffle(avail)
    for t in avail[:need]:
        db.add(ReflexDailyTask(
            player_id=player_id, date=today,
            code=t["code"], progress=0, target=t["target"],
            reward_coins=t["reward"], claimed=False,
        ))
    db.commit()


@router.get("/daily_tasks")
def daily_tasks(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    pid = payload.get("player_id")
    _ensure_daily_tasks(db, pid)
    today = _today_str()
    rows = db.query(ReflexDailyTask).filter(
        ReflexDailyTask.player_id == pid,
        ReflexDailyTask.date == today,
    ).all()
    catalog = {t["code"]: t for t in DAILY_TASK_CATALOG}
    return {
        "authenticated": True,
        "tasks": [
            {
                "id": r.id,
                "code": r.code,
                "title": catalog.get(r.code, {}).get("title", r.code),
                "icon": catalog.get(r.code, {}).get("icon", "🎯"),
                "progress": r.progress,
                "target": r.target,
                "reward_coins": r.reward_coins,
                "claimed": r.claimed,
                "completed": r.progress >= r.target,
            }
            for r in rows
        ],
    }


@router.post("/daily_tasks/claim")
def claim_daily_task(
    data: dict,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    task_id = (data or {}).get("id")
    r = db.query(ReflexDailyTask).filter(
        ReflexDailyTask.id == task_id,
        ReflexDailyTask.player_id == pid,
    ).with_for_update().first()
    if not r: return {"ok": False, "msg": "Задание не найдено"}
    if r.claimed: return {"ok": False, "msg": "Уже получено"}
    if r.progress < r.target: return {"ok": False, "msg": "Задание не выполнено"}
    p = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if p:
        p.coins = (p.coins or 0) + r.reward_coins
    r.claimed = True
    db.commit()
    return {"ok": True, "reward": r.reward_coins, "new_coins": p.coins if p else 0}


# Пул мини-игр, которые ротируются в Daily Challenge
DAILY_CHALLENGE_POOL = [
    "aim", "odd",
    "math", "stroop", "number_chain",
    "memory", "spatial", "visual_memory", "word_memory",
    "typing", "rhythm", "balance", "quick_draw",
    "flags_rain", "sort_zones", "map_tap", "timeline",
    "reaction_grid", "count_dots",
    "simon", "go_nogo", "laser_maze", "memory_matrix", "einstein",
]

# Anti-cheat границы (совпадают с GAMES в reflex_ws.py)
_DC_LIMITS = {
    "aim":     {"min_ms": 14500, "max_ms": 16500, "max_score": 100},
    "math":    {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "odd":     {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "typing":  {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "stroop":  {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "memory":  {"min_ms": 3000,  "max_ms": 32000, "max_score": 30},
    "spatial": {"min_ms": 2000,  "max_ms": 60000, "max_score": 30},
    "rhythm":  {"min_ms": 15000, "max_ms": 22000, "max_score": 50},
    "balance": {"min_ms": 29500, "max_ms": 31000, "max_score": 20},
    "quick_draw": {"min_ms": 3000, "max_ms": 30000, "max_score": 25},
    "number_chain": {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "visual_memory": {"min_ms": 10000, "max_ms": 60000, "max_score": 12},
    "word_memory": {"min_ms": 10000, "max_ms": 60000, "max_score": 20},
    "flags_rain": {"min_ms": 19500, "max_ms": 21000, "max_score": 30},
    "sort_zones": {"min_ms": 19500, "max_ms": 21000, "max_score": 40},
    "map_tap": {"min_ms": 19500, "max_ms": 21000, "max_score": 20},
    "timeline": {"min_ms": 5000, "max_ms": 32000, "max_score": 15},
    "reaction_grid": {"min_ms": 19500, "max_ms": 21000, "max_score": 50},
    "count_dots": {"min_ms": 19500, "max_ms": 21000, "max_score": 30},
    "simon": {"min_ms": 5000, "max_ms": 62000, "max_score": 15},
    "go_nogo": {"min_ms": 19500, "max_ms": 21000, "max_score": 30},
    "laser_maze": {"min_ms": 3000, "max_ms": 32000, "max_score": 20},
    "memory_matrix": {"min_ms": 5000, "max_ms": 62000, "max_score": 12},
    "einstein": {"min_ms": 5000, "max_ms": 47000, "max_score": 8},
}


def _challenge_game_for_date(date_str: str) -> str:
    """Детерминированно выбирает игру дня по дате (чтобы у всех в мире была одна и та же)."""
    import hashlib
    h = int(hashlib.md5(date_str.encode()).hexdigest(), 16)
    return DAILY_CHALLENGE_POOL[h % len(DAILY_CHALLENGE_POOL)]


@router.get("/daily_challenge")
def daily_challenge_info(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Инфо о челлендже: игра дня + мой лучший + топ-20."""
    today = _today_str()
    game = _challenge_game_for_date(today)
    my_best = None
    my_attempts = 0
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            pid = payload.get("player_id")
            r = db.query(ReflexDailyChallenge).filter(
                ReflexDailyChallenge.player_id == pid,
                ReflexDailyChallenge.date == today,
            ).first()
            if r:
                my_best = r.best_score
                my_attempts = r.attempts
    # Топ-20 дня
    top_rows = (
        db.query(ReflexDailyChallenge, Player.nickname)
        .join(Player, Player.id == ReflexDailyChallenge.player_id)
        .filter(ReflexDailyChallenge.date == today)
        .order_by(ReflexDailyChallenge.best_score.desc())
        .limit(20)
        .all()
    )
    return {
        "date": today,
        "game": game,
        "game_name": {
            "aim": "Снайпер", "math": "Устный счёт", "odd": "Найди отличие",
            "typing": "Скорость печати", "stroop": "Строп-тест",
            "memory": "Память", "spatial": "Пространственная память",
            "visual_memory": "Визуальная память", "word_memory": "Слова",
            "rhythm": "Ритм", "balance": "Гольф", "quick_draw": "Быстрый штрих",
            "number_chain": "Числовая цепочка",
            "flags_rain": "Поймай флаг", "sort_zones": "Сортировка",
            "map_tap": "Карта мира", "timeline": "Хронология",
            "reaction_grid": "Сетка", "count_dots": "Счёт точек",
        }.get(game, game),
        "my_best": my_best,
        "my_attempts": my_attempts,
        "top": [
            {"rank": i + 1, "nickname": nick, "score": row.best_score}
            for i, (row, nick) in enumerate(top_rows)
        ],
    }


@router.post("/daily_challenge/submit")
def daily_challenge_submit(
    data: dict,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    pid = payload.get("player_id")
    today = _today_str()
    game = _challenge_game_for_date(today)

    try:
        score = int((data or {}).get("score", 0))
        elapsed = int((data or {}).get("elapsed_ms", 0))
    except Exception:
        return {"ok": False, "msg": "bad payload"}

    limits = _DC_LIMITS.get(game, {})
    if limits:
        if score < 0 or score > limits.get("max_score", 99999):
            score = 0
        if elapsed < limits.get("min_ms", 0):
            score = 0

    r = db.query(ReflexDailyChallenge).filter(
        ReflexDailyChallenge.player_id == pid,
        ReflexDailyChallenge.date == today,
    ).with_for_update().first()
    is_best = False
    if not r:
        r = ReflexDailyChallenge(player_id=pid, date=today, game=game,
                                 best_score=score, attempts=1)
        db.add(r)
        is_best = True
    else:
        r.attempts += 1
        if score > (r.best_score or 0):
            r.best_score = score
            is_best = True
    db.commit()
    return {"ok": True, "best_score": r.best_score, "is_best": is_best, "attempts": r.attempts}


# ─── Shop: темы интерфейса ───
THEMES = {
    "classic": {"name": "Классика", "price": 0,   "accent": "#ff7a29", "accent2": "#ffb86b"},
    "neon":    {"name": "Неон",     "price": 500, "accent": "#00ff9d", "accent2": "#ff00e5"},
    "midnight":{"name": "Полночь",  "price": 800, "accent": "#7dd3fc", "accent2": "#a78bfa"},
    "emerald": {"name": "Изумруд",  "price": 800, "accent": "#26d67f", "accent2": "#7fe3b0"},
    # style-presets — полный фирменный стиль, а не только accent
    "retro":     {"name": "🕹️ Ретро-аркада", "price": 1500, "accent": "#ff4081", "accent2": "#ffd740", "style": "retro"},
    "cyberpunk": {"name": "🧠 Cyberpunk",    "price": 1500, "accent": "#00fff7", "accent2": "#ff00e5", "style": "cyberpunk"},
    "duolingo":  {"name": "🌈 Duolingo",     "price": 1500, "accent": "#58cc02", "accent2": "#ffc800", "style": "duolingo"},
    "brutal":    {"name": "🧱 Брутал",        "price": 1500, "accent": "#000000", "accent2": "#ffeb3b", "style": "brutal"},
}


def _owned_themes(db: Session, player_id: int):
    rows = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("theme_owned_%"),
    ).all()
    owned = {r.code[len("theme_owned_"):] for r in rows}
    owned.add("classic")
    return owned


def _equipped_theme(db: Session, player_id: int) -> str:
    eq = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("theme_equipped_%"),
    ).first()
    if eq:
        return eq.code[len("theme_equipped_"):]
    return "classic"


@router.get("/shop/themes")
def shop_themes(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    owned = {"classic"}; equipped = "classic"
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            pid = payload.get("player_id")
            owned = _owned_themes(db, pid)
            equipped = _equipped_theme(db, pid)
    return {
        "themes": [
            {"id": k, "name": v["name"], "price": v["price"],
             "accent": v["accent"], "accent2": v["accent2"],
             "style": v.get("style", ""),
             "owned": k in owned, "equipped": k == equipped}
            for k, v in THEMES.items()
        ],
    }


@router.post("/shop/buy_theme")
def buy_theme(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    tid = (data or {}).get("theme_id", "")
    theme = THEMES.get(tid)
    if not theme: return {"ok": False, "msg": "Тема не найдена"}
    owned = _owned_themes(db, me.id)
    if tid in owned: return {"ok": False, "msg": "Уже куплено"}
    if (me.coins or 0) < theme["price"]:
        return {"ok": False, "msg": f"Нужно {theme['price']} 💰"}
    me.coins -= theme["price"]
    db.add(ReflexAchievement(player_id=me.id, code=f"theme_owned_{tid}"))
    db.commit()
    return {"ok": True, "new_coins": me.coins}


@router.post("/shop/equip_theme")
def equip_theme(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    tid = (data or {}).get("theme_id", "")
    if tid not in THEMES: return {"ok": False}
    owned = _owned_themes(db, pid)
    if tid not in owned: return {"ok": False, "msg": "Не куплено"}
    db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("theme_equipped_%"),
    ).delete(synchronize_session=False)
    db.add(ReflexAchievement(player_id=pid, code=f"theme_equipped_{tid}"))
    db.commit()
    return {"ok": True, "equipped": tid}


ACHIEVEMENTS_CATALOG = {
    "first_win":    {"name": "Первая кровь", "desc": "Выиграть первый матч", "icon": "🩸"},
    "win_3":        {"name": "Серия", "desc": "Выиграть 3 матча подряд", "icon": "🔥"},
    "win_10":       {"name": "Ветеран", "desc": "Выиграть 10 матчей", "icon": "🎖️"},
    "perfect":      {"name": "Тотальное превосходство", "desc": "Выиграть матч 3:0", "icon": "💯"},
    "stake_winner": {"name": "Жадный", "desc": "Выиграть матч со ставкой", "icon": "💰"},
    "comeback":     {"name": "Камбек", "desc": "Выиграть проигрывая 0:2", "icon": "🔄"},
    "diverse":      {"name": "Мастер на все руки", "desc": "Поиграть во все мини-игры", "icon": "🎭"},
}


@router.get("/achievements")
def achievements(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Список достижений — все с пометкой разблокировано/нет."""
    unlocked_codes = set()
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            rows = db.query(ReflexAchievement).filter(
                ReflexAchievement.player_id == payload.get("player_id")
            ).all()
            unlocked_codes = {r.code for r in rows}
    result = []
    for code, info in ACHIEVEMENTS_CATALOG.items():
        result.append({
            "code": code, "name": info["name"], "desc": info["desc"], "icon": info["icon"],
            "unlocked": code in unlocked_codes,
        })
    return result


@router.post("/attach_referrer")
def attach_referrer(
    data: dict,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Привязывает текущего игрока к рефереру. Можно сделать только один раз.
    Оба получают REFERRAL_BONUS coins."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False, "msg": "Не авторизован"}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False, "msg": "Не авторизован"}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).first()
    if not me:
        return {"ok": False, "msg": "Игрок не найден"}
    if me.referred_by or me.referral_bonus_claimed:
        return {"ok": False, "msg": "Реферер уже задан"}
    ref_nick = (data or {}).get("ref_nickname", "").strip()
    if not ref_nick:
        return {"ok": False, "msg": "Нужен ref_nickname"}
    ref = db.query(Player).filter(Player.nickname == ref_nick).first()
    if not ref or ref.id == me.id:
        return {"ok": False, "msg": "Реферер не найден"}
    me.referred_by = ref.id
    me.referral_bonus_claimed = True
    me.coins = (me.coins or 0) + REFERRAL_BONUS
    ref.coins = (ref.coins or 0) + REFERRAL_BONUS
    db.commit()
    return {"ok": True, "bonus": REFERRAL_BONUS}


@router.get("/referral_info")
def referral_info(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).first()
    if not me:
        return {"authenticated": False}
    invited = db.query(Player).filter(Player.referred_by == me.id).count()
    return {
        "authenticated": True,
        "my_nickname": me.nickname,
        "invited_count": invited,
        "bonus_per_invite": REFERRAL_BONUS,
        "referred_by_me_already": bool(me.referred_by),
    }


def _get_player(authorization: Optional[str], db: Session) -> Optional[Player]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    data = verify_token(authorization[7:])
    if not data:
        return None
    return db.query(Player).filter(Player.id == data.get("player_id")).first()


@router.get("/leaderboard/category/{category}")
def leaderboard_category(category: str, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    field_map = {
        "reaction": Player.elo_reaction,
        "logic": Player.elo_logic,
        "memory": Player.elo_memory,
        "coordination": Player.elo_coordination,
        "trivia": Player.elo_trivia,
    }
    field = field_map.get(category)
    if not field:
        return []
    players = (
        db.query(Player)
        .filter((Player.reflex_wins + Player.reflex_losses) > 0)
        .order_by(desc(field))
        .limit(limit)
        .all()
    )
    return [
        {
            "rank": i + 1,
            "player_id": p.id,
            "nickname": p.nickname,
            "elo": round(getattr(p, f"elo_{category.replace('elo_', '')}") if hasattr(p, f"elo_{category}") else (getattr(p, field.key, 1000.0) or 1000.0), 1),
            "wins": p.reflex_wins or 0,
        }
        for i, p in enumerate(players)
    ]


@router.get("/leaderboard")
def leaderboard(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    players = (
        db.query(Player)
        .filter((Player.reflex_wins + Player.reflex_losses) > 0)
        .order_by(desc(Player.reflex_elo))
        .limit(limit)
        .all()
    )
    return [
        {
            "rank": i + 1,
            "player_id": p.id,
            "nickname": p.nickname,
            "elo": round(p.reflex_elo or 1000.0, 1),
            "wins": p.reflex_wins or 0,
            "losses": p.reflex_losses or 0,
            "winrate": round(
                (p.reflex_wins or 0) * 100 / max(1, (p.reflex_wins or 0) + (p.reflex_losses or 0)),
                1,
            ),
        }
        for i, p in enumerate(players)
    ]


@router.get("/me")
def me(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    player = _get_player(authorization, db)
    if not player:
        return {"authenticated": False}
    recent = (
        db.query(ReflexMatch)
        .filter(or_(ReflexMatch.p1_id == player.id, ReflexMatch.p2_id == player.id))
        .filter(ReflexMatch.status == "finished")
        .order_by(desc(ReflexMatch.finished_at))
        .limit(10)
        .all()
    )
    recent_serialized = []
    for m in recent:
        is_p1 = m.p1_id == player.id
        opp_id = m.p2_id if is_p1 else m.p1_id
        opp = db.query(Player).filter(Player.id == opp_id).first()
        my_rounds = m.rounds_p1 if is_p1 else m.rounds_p2
        opp_rounds = m.rounds_p2 if is_p1 else m.rounds_p1
        my_delta = m.elo_change_p1 if is_p1 else m.elo_change_p2
        recent_serialized.append({
            "id": m.id,
            "opponent": opp.nickname if opp else "?",
            "score": f"{my_rounds}:{opp_rounds}",
            "won": m.winner_id == player.id,
            "elo_delta": round(my_delta or 0, 1),
            "stake": m.stake_coins or 0,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
        })
    total = (player.reflex_wins or 0) + (player.reflex_losses or 0)
    return {
        "authenticated": True,
        "player_id": player.id,
        "nickname": player.nickname,
        "coins": player.coins or 0,
        "gems": player.gems or 0,
        "elo": round(player.reflex_elo or 1000.0, 1),
        "tier": compute_tier(player.reflex_elo or 1000.0),
        "wins": player.reflex_wins or 0,
        "losses": player.reflex_losses or 0,
        "winrate": round((player.reflex_wins or 0) * 100 / max(1, total), 1) if total else 0,
        "is_guest": bool(player.is_guest),
        "onboarded": bool(player.reflex_onboarded),
        "category_elo": {
            "reaction": round(player.elo_reaction or 1000.0, 1),
            "logic": round(player.elo_logic or 1000.0, 1),
            "memory": round(player.elo_memory or 1000.0, 1),
            "coordination": round(player.elo_coordination or 1000.0, 1),
            "trivia": round(player.elo_trivia or 1000.0, 1),
        },
        "recent_matches": recent_serialized,
    }


@share_router.get("/profile/{nickname}", response_class=HTMLResponse)
def public_profile_page(nickname: str, db: Session = Depends(get_db)):
    """Публичная страница профиля с OG-тегами (для шеринга)."""
    p = db.query(Player).filter(Player.nickname == nickname).first()
    if not p:
        return HTMLResponse("<h1>Игрок не найден</h1>", status_code=404)
    elo = round(p.reflex_elo or 1000.0, 1)
    wins = p.reflex_wins or 0
    losses = p.reflex_losses or 0
    total = wins + losses
    wr = round(wins * 100 / max(1, total), 1) if total else 0
    # Лига
    leagues = [
        (0, "Bronze", "🥉"), (900, "Silver", "🥈"), (1000, "Gold", "🥇"),
        (1150, "Platinum", "💎"), (1300, "Diamond", "💠"),
        (1500, "Master", "🏆"), (1700, "Grandmaster", "👑"),
    ]
    cur = leagues[0]
    for l in leagues:
        if elo >= l[0]: cur = l
    league_name, league_icon = cur[1], cur[2]
    nick_e = html.escape(p.nickname)
    title = f"{nick_e} — Reflex Arena (ELO {int(elo)} {league_name})"
    desc = f"{nick_e}: {wins}W / {losses}L • {wr}% winrate • {league_icon} {league_name}. Вызови на 1v1!"
    og_img = f"/reflex/share/profile/{nick_e}/og.png"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<meta property="og:type" content="profile"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:image" content="{og_img}"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:image" content="{og_img}"/>
<style>
body{{margin:0;background:#0e0f13;color:#eaecef;font-family:-apple-system,system-ui,sans-serif;min-height:100vh;padding:20px;}}
.wrap{{max-width:520px;margin:40px auto;}}
.card{{background:#16181f;border:1px solid #2a2e3a;border-radius:18px;padding:26px;}}
h1{{margin:0 0 6px;font-size:28px;}}
.sub{{color:#8a93a6;font-size:14px;margin-bottom:20px;}}
.league{{display:inline-flex;align-items:center;gap:6px;background:rgba(255,122,41,0.15);color:#ff7a29;padding:6px 12px;border-radius:20px;font-weight:700;font-size:13px;margin-bottom:16px;}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0;}}
.s{{background:#1c1f28;border:1px solid #2a2e3a;border-radius:10px;padding:12px;text-align:center;}}
.s .v{{font-size:22px;font-weight:800;}}
.s .l{{font-size:11px;color:#8a93a6;text-transform:uppercase;margin-top:2px;}}
h3{{font-size:12px;color:#8a93a6;text-transform:uppercase;letter-spacing:1px;margin:20px 0 10px;}}
.match{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #2a2e3a;font-size:13px;}}
.match:last-child{{border-bottom:none;}}
.w{{color:#26d67f;font-weight:700;}}
.l{{color:#ff4d6d;font-weight:700;}}
.btn{{display:block;text-align:center;background:linear-gradient(90deg,#ff7a29,#ffb86b);color:#1a1205;padding:14px;border-radius:10px;text-decoration:none;font-weight:800;margin-top:20px;}}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="league">{league_icon} {league_name}</div>
    <h1>{nick_e}</h1>
    <div class="sub">Reflex Arena</div>
    <div class="stats">
      <div class="s"><div class="v">{int(elo)}</div><div class="l">ELO</div></div>
      <div class="s"><div class="v">{wins}</div><div class="l">Побед</div></div>
      <div class="s"><div class="v">{losses}</div><div class="l">Пораж.</div></div>
      <div class="s"><div class="v">{wr}%</div><div class="l">Winrate</div></div>
    </div>
    <div id="matches-slot"></div>
    <a class="btn" href="/reflex">Играй 1v1 →</a>
  </div>
</div>
<script>
fetch('/api/reflex/profile/' + encodeURIComponent({nick_e!r})).then(r=>r.json()).then(d=>{{
  if (!d.found || !d.recent || !d.recent.length) return;
  const html = ['<h3>Последние матчи</h3>'];
  d.recent.forEach(m => {{
    const cls = m.won ? 'w' : 'l';
    html.push('<div class="match"><div>vs <b>' + m.opponent + '</b> — ' + m.score + '</div><div class="' + cls + '">' + (m.won?'победа':'поражение') + '</div></div>');
  }});
  document.getElementById('matches-slot').innerHTML = html.join('');
}});
</script>
</body></html>""")


@share_router.get("/share/profile/{nickname}/og.png")
def profile_og(nickname: str, db: Session = Depends(get_db)):
    p = db.query(Player).filter(Player.nickname == nickname).first()
    if not p: return Response(status_code=404)
    elo = round(p.reflex_elo or 1000.0, 1)
    wins = p.reflex_wins or 0
    losses = p.reflex_losses or 0
    nick_e = html.escape(p.nickname)
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#0e0f13"/><stop offset="1" stop-color="#1c1116"/>
</linearGradient></defs>
<rect width="1200" height="630" fill="url(#bg)"/>
<text x="60" y="90" fill="#ff7a29" font-family="system-ui,Arial" font-weight="800" font-size="40">⚡ REFLEX ARENA</text>
<text x="600" y="270" text-anchor="middle" fill="#eaecef" font-family="system-ui,Arial" font-weight="800" font-size="90">{nick_e}</text>
<text x="600" y="380" text-anchor="middle" fill="#ffb86b" font-family="'JetBrains Mono',monospace" font-weight="800" font-size="100">ELO {int(elo)}</text>
<text x="600" y="470" text-anchor="middle" fill="#8a93a6" font-family="system-ui,Arial" font-size="32">{wins}W / {losses}L</text>
<text x="600" y="570" text-anchor="middle" fill="#26d67f" font-family="system-ui,Arial" font-weight="700" font-size="30">Вызови на 1v1!</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@share_router.get("/share/{match_id}", response_class=HTMLResponse)
def share_match(match_id: int, db: Session = Depends(get_db)):
    """HTML-страница с OG-тегами для шеринга."""
    m = db.query(ReflexMatch).filter(ReflexMatch.id == match_id).first()
    if not m:
        return HTMLResponse("<h1>Матч не найден</h1>", status_code=404)
    p1 = db.query(Player).filter(Player.id == m.p1_id).first()
    p2 = db.query(Player).filter(Player.id == m.p2_id).first()
    n1 = html.escape(p1.nickname if p1 else "?")
    n2 = html.escape(p2.nickname if p2 else "?")
    score = f"{m.rounds_p1}:{m.rounds_p2}"
    winner = n1 if m.winner_id == m.p1_id else (n2 if m.winner_id == m.p2_id else "Ничья")
    title = f"{n1} {score} {n2} — Reflex Arena"
    desc = f"{winner} выиграл матч со счётом {score} в Reflex Arena. Играй 1v1 на реакцию!"
    og_img = f"/share/{match_id}/og.png"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<meta property="og:type" content="website"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:image" content="{og_img}"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{title}"/>
<meta name="twitter:description" content="{desc}"/>
<meta name="twitter:image" content="{og_img}"/>
<style>
body{{margin:0;background:#0e0f13;color:#eaecef;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px}}
.card{{background:#16181f;border:1px solid #2a2e3a;border-radius:16px;padding:28px;max-width:520px;width:100%;text-align:center}}
.logo{{color:#ff7a29;font-weight:800;margin-bottom:14px;letter-spacing:1px}}
.score{{font-size:56px;font-weight:800;font-family:'JetBrains Mono',monospace;margin:10px 0}}
.players{{color:#8a93a6;font-size:15px;margin-bottom:18px}}
.winner{{color:#26d67f;font-weight:700;margin-bottom:22px}}
.btn{{display:inline-block;background:linear-gradient(90deg,#ff7a29,#ffb86b);color:#1a1205;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:800;font-size:15px}}
</style></head><body>
<div class="card">
  <div class="logo">⚡ REFLEX ARENA</div>
  <div class="score">{score}</div>
  <div class="players">{n1}  vs  {n2}</div>
  <div class="winner">🏆 {winner}</div>
  <a class="btn" href="/reflex">Играть 1v1 →</a>
</div>
</body></html>""")


@share_router.get("/share/{match_id}/og.png")
def share_match_og(match_id: int, db: Session = Depends(get_db)):
    """Рендерит OG-картинку (1200x630) с результатом матча."""
    m = db.query(ReflexMatch).filter(ReflexMatch.id == match_id).first()
    if not m:
        return Response(status_code=404)
    p1 = db.query(Player).filter(Player.id == m.p1_id).first()
    p2 = db.query(Player).filter(Player.id == m.p2_id).first()
    n1 = p1.nickname if p1 else "?"
    n2 = p2.nickname if p2 else "?"
    score = f"{m.rounds_p1}:{m.rounds_p2}"
    winner_nick = n1 if m.winner_id == m.p1_id else (n2 if m.winner_id == m.p2_id else "Ничья")

    # Fallback через SVG — без зависимостей, работает на любом Python
    n1e = html.escape(n1); n2e = html.escape(n2); we = html.escape(winner_nick)
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0e0f13"/>
    <stop offset="1" stop-color="#1c1116"/>
  </linearGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="#26d67f" stop-opacity="0.18"/>
    <stop offset="1" stop-color="#000" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="1200" height="630" fill="url(#bg)"/>
<rect width="1200" height="630" fill="url(#glow)"/>
<text x="60" y="90" fill="#ff7a29" font-family="system-ui,Arial" font-weight="800" font-size="40">⚡ REFLEX ARENA</text>
<text x="600" y="340" text-anchor="middle" fill="#eaecef" font-family="'JetBrains Mono',monospace" font-weight="800" font-size="170">{score}</text>
<text x="600" y="420" text-anchor="middle" fill="#8a93a6" font-family="system-ui,Arial" font-size="32">{n1e}  vs  {n2e}</text>
<text x="600" y="500" text-anchor="middle" fill="#26d67f" font-family="system-ui,Arial" font-weight="700" font-size="50">🏆 {we}</text>
<text x="600" y="580" text-anchor="middle" fill="#ffb86b" font-family="system-ui,Arial" font-size="28">Играй 1v1 на reflex-arena</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/matches")
def recent_matches(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    """Лента последних матчей — для главного экрана, 'что сейчас идёт'."""
    matches = (
        db.query(ReflexMatch)
        .filter(ReflexMatch.status == "finished")
        .order_by(desc(ReflexMatch.finished_at))
        .limit(limit)
        .all()
    )
    out = []
    for m in matches:
        p1 = db.query(Player).filter(Player.id == m.p1_id).first()
        p2 = db.query(Player).filter(Player.id == m.p2_id).first()
        out.append({
            "id": m.id,
            "p1": p1.nickname if p1 else "?",
            "p2": p2.nickname if p2 else "?",
            "score": f"{m.rounds_p1}:{m.rounds_p2}",
            "winner_id": m.winner_id,
            "stake": m.stake_coins or 0,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
        })
    return out


# ─── Друзья ───
def _friends_symmetric(db, a_id: int, b_id: int):
    """Оба направления связи."""
    return db.query(ReflexFriend).filter(
        ((ReflexFriend.player_id == a_id) & (ReflexFriend.friend_id == b_id)) |
        ((ReflexFriend.player_id == b_id) & (ReflexFriend.friend_id == a_id))
    ).all()


@router.post("/friends/add")
def add_friend(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me_id = payload.get("player_id")
    target_nick = (data or {}).get("nickname", "").strip()
    if not target_nick: return {"ok": False, "msg": "Нужен ник"}
    target = db.query(Player).filter(Player.nickname == target_nick).first()
    if not target or target.id == me_id:
        return {"ok": False, "msg": "Игрок не найден"}
    existing = _friends_symmetric(db, me_id, target.id)
    if any(f.status == "accepted" for f in existing):
        return {"ok": False, "msg": "Уже друзья"}
    if any(f.status == "pending" and f.player_id == me_id for f in existing):
        return {"ok": False, "msg": "Запрос уже отправлен"}
    # Если есть встречный pending — акцептим
    incoming = next((f for f in existing if f.status == "pending" and f.player_id == target.id), None)
    if incoming:
        incoming.status = "accepted"
        db.add(ReflexFriend(player_id=me_id, friend_id=target.id, status="accepted"))
        db.commit()
        return {"ok": True, "accepted": True}
    # Новый pending
    db.add(ReflexFriend(player_id=me_id, friend_id=target.id, status="pending"))
    db.commit()
    return {"ok": True, "pending": True}


@router.post("/friends/accept")
def accept_friend(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me_id = payload.get("player_id")
    friend_id = int((data or {}).get("friend_id", 0))
    incoming = db.query(ReflexFriend).filter(
        ReflexFriend.player_id == friend_id,
        ReflexFriend.friend_id == me_id,
        ReflexFriend.status == "pending",
    ).first()
    if not incoming:
        return {"ok": False, "msg": "Запрос не найден"}
    incoming.status = "accepted"
    # Симметричная запись (если нет)
    existing = db.query(ReflexFriend).filter(
        ReflexFriend.player_id == me_id, ReflexFriend.friend_id == friend_id,
    ).first()
    if not existing:
        db.add(ReflexFriend(player_id=me_id, friend_id=friend_id, status="accepted"))
    else:
        existing.status = "accepted"
    db.commit()
    return {"ok": True}


@router.post("/friends/remove")
def remove_friend(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me_id = payload.get("player_id")
    friend_id = int((data or {}).get("friend_id", 0))
    db.query(ReflexFriend).filter(
        ((ReflexFriend.player_id == me_id) & (ReflexFriend.friend_id == friend_id)) |
        ((ReflexFriend.player_id == friend_id) & (ReflexFriend.friend_id == me_id))
    ).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.get("/friends")
def list_friends(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    me_id = payload.get("player_id")
    # Accepted (только direction from me — симметрия гарантирована accept-ом)
    accepted = db.query(ReflexFriend, Player).join(
        Player, Player.id == ReflexFriend.friend_id
    ).filter(
        ReflexFriend.player_id == me_id,
        ReflexFriend.status == "accepted",
    ).all()
    # Incoming pending (кто-то отправил мне запрос)
    incoming = db.query(ReflexFriend, Player).join(
        Player, Player.id == ReflexFriend.player_id
    ).filter(
        ReflexFriend.friend_id == me_id,
        ReflexFriend.status == "pending",
    ).all()
    # Outgoing pending (я отправил запрос)
    outgoing = db.query(ReflexFriend, Player).join(
        Player, Player.id == ReflexFriend.friend_id
    ).filter(
        ReflexFriend.player_id == me_id,
        ReflexFriend.status == "pending",
    ).all()
    return {
        "authenticated": True,
        "friends": [
            {"id": p.id, "nickname": p.nickname, "elo": round(p.reflex_elo or 1000.0, 1)}
            for _, p in accepted
        ],
        "incoming": [
            {"id": p.id, "nickname": p.nickname, "elo": round(p.reflex_elo or 1000.0, 1)}
            for _, p in incoming
        ],
        "outgoing": [
            {"id": p.id, "nickname": p.nickname, "elo": round(p.reflex_elo or 1000.0, 1)}
            for _, p in outgoing
        ],
    }


# ─── Публичный профиль ───
@router.get("/profile/{nickname}")
def public_profile(nickname: str, db: Session = Depends(get_db)):
    p = db.query(Player).filter(Player.nickname == nickname).first()
    if not p:
        return {"found": False}
    wins = p.reflex_wins or 0; losses = p.reflex_losses or 0; total = wins + losses
    # Последние 10 матчей
    recent = db.query(ReflexMatch).filter(
        (ReflexMatch.p1_id == p.id) | (ReflexMatch.p2_id == p.id),
        ReflexMatch.status == "finished",
    ).order_by(ReflexMatch.finished_at.desc()).limit(10).all()
    recent_ser = []
    for m in recent:
        is_p1 = m.p1_id == p.id
        opp_id = m.p2_id if is_p1 else m.p1_id
        opp = db.query(Player).filter(Player.id == opp_id).first()
        my_r = m.rounds_p1 if is_p1 else m.rounds_p2
        opp_r = m.rounds_p2 if is_p1 else m.rounds_p1
        recent_ser.append({
            "opponent": opp.nickname if opp else "?",
            "score": f"{my_r}:{opp_r}",
            "won": m.winner_id == p.id,
        })
    # Достижения
    ach = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == p.id,
        ~ReflexAchievement.code.like("theme_%"),  # исключаем скины из публичных достижений
    ).all()
    return {
        "found": True,
        "nickname": p.nickname,
        "elo": round(p.reflex_elo or 1000.0, 1),
        "wins": wins, "losses": losses,
        "winrate": round(wins * 100 / max(1, total), 1),
        "recent": recent_ser,
        "achievements_count": len(ach),
    }


# ═══════════════════════════════════════════════════════
#   Battle Pass + Сезоны
# ═══════════════════════════════════════════════════════

SEASON_DAYS = 28
PASS_XP_PER_LEVEL = 100
PASS_MAX_LEVEL = 30
PREMIUM_PASS_PRICE = 999  # coins (позже — реальные деньги)

# Награды по уровням: код + описание. Free и Premium tracks.
# Код вида "coins_50", "theme_neon", "avatar_1", "vfx_fire", "emote_pack_1", "frame_ep1"
PASS_REWARDS = {}
for lvl in range(1, PASS_MAX_LEVEL + 1):
    free_reward = None; premium_reward = None
    # Free — простой линейный: каждые 2 уровня = 50 монет, на круглых — эмоут
    if lvl % 5 == 0:
        free_reward = {"code": f"coins_{lvl * 20}", "type": "coins", "amount": lvl * 20, "icon": "💰"}
    elif lvl % 3 == 0:
        free_reward = {"code": f"xp_boost_free_{lvl}", "type": "coins", "amount": 50, "icon": "💰"}
    else:
        free_reward = {"code": f"coins_small_{lvl}", "type": "coins", "amount": 30, "icon": "💰"}
    # Premium — ценнее
    if lvl == 5:
        premium_reward = {"code": "vfx_confetti", "type": "vfx", "name": "Конфетти", "icon": "🎉"}
    elif lvl == 10:
        premium_reward = {"code": "theme_neon", "type": "theme", "name": "Тема «Неон»", "icon": "🎨"}
    elif lvl == 15:
        premium_reward = {"code": "vfx_fire", "type": "vfx", "name": "Огонь", "icon": "🔥"}
    elif lvl == 20:
        premium_reward = {"code": "theme_emerald", "type": "theme", "name": "Тема «Изумруд»", "icon": "💚"}
    elif lvl == 25:
        premium_reward = {"code": "vfx_lightning", "type": "vfx", "name": "Молния", "icon": "⚡"}
    elif lvl == 30:
        premium_reward = {"code": "frame_legendary", "type": "frame", "name": "Легендарная рамка", "icon": "👑"}
    elif lvl % 4 == 0:
        premium_reward = {"code": f"coins_pp_{lvl}", "type": "coins", "amount": lvl * 40, "icon": "💰"}
    else:
        premium_reward = {"code": f"coins_pp_small_{lvl}", "type": "coins", "amount": lvl * 15, "icon": "💰"}
    PASS_REWARDS[lvl] = {"free": free_reward, "premium": premium_reward}


def _ensure_active_season(db: Session) -> ReflexSeason:
    """Возвращает активный сезон, создавая/завершая по необходимости."""
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    active = db.query(ReflexSeason).filter(ReflexSeason.status == "active").first()
    if active:
        # Проверяем не истёк ли
        end_at = active.end_at
        if end_at and end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=tz.utc)
        if end_at and end_at < now:
            # Завершаем сезон + soft ELO reset + выдаём награды
            _finalize_season(db, active)
            active = None
    if not active:
        # Новый сезон
        prev_count = db.query(ReflexSeason).count()
        s = ReflexSeason(
            name=f"Сезон {prev_count + 1}",
            end_at=now + timedelta(days=SEASON_DAYS),
            status="active",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        active = s
    return active


def _finalize_season(db: Session, season: ReflexSeason):
    """Финализирует сезон: записывает награды, soft reset ELO."""
    from datetime import datetime
    # Топ-100 по ELO
    top = db.query(Player).filter(
        (Player.reflex_wins + Player.reflex_losses) > 0
    ).order_by(Player.reflex_elo.desc()).limit(100).all()
    for rank, p in enumerate(top, 1):
        # Начисляем монеты: топ-1 = 10к, 2-10 = 3к, 11-50 = 1к, 51-100 = 500
        if rank == 1:
            coins = 10000
        elif rank <= 10:
            coins = 3000
        elif rank <= 50:
            coins = 1000
        else:
            coins = 500
        db.add(ReflexSeasonReward(
            player_id=p.id, season_id=season.id, rank=rank,
            final_elo=p.reflex_elo or 1000.0,
            coins_given=coins, claimed=False,
        ))
    # Soft ELO reset для всех
    all_players = db.query(Player).filter(Player.reflex_elo != None).all()
    for p in all_players:
        cur = p.reflex_elo or 1000.0
        p.reflex_elo = round(1000.0 + (cur - 1000.0) * 0.3, 2)
    season.status = "finished"
    season.finished_at = datetime.utcnow()
    db.commit()


def _grant_pass_xp(db: Session, player_id: int, xp: int):
    """Добавляет XP к пассу текущего сезона. Вызывается после матча."""
    season = _ensure_active_season(db)
    prog = db.query(ReflexPassProgress).filter(
        ReflexPassProgress.player_id == player_id,
        ReflexPassProgress.season_id == season.id,
    ).with_for_update().first()
    if not prog:
        prog = ReflexPassProgress(
            player_id=player_id, season_id=season.id,
            xp=0, level=0, premium=False,
            claimed_levels_free=[], claimed_levels_premium=[],
        )
        db.add(prog); db.flush()
    prog.xp = (prog.xp or 0) + xp
    prog.level = min(PASS_MAX_LEVEL, prog.xp // PASS_XP_PER_LEVEL)


@router.get("/pass")
def get_pass(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    season = _ensure_active_season(db)
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    end_at = season.end_at
    if end_at and end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=tz.utc)
    days_left = max(0, (end_at - now).days) if end_at else 0

    prog_data = None
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            prog = db.query(ReflexPassProgress).filter(
                ReflexPassProgress.player_id == payload.get("player_id"),
                ReflexPassProgress.season_id == season.id,
            ).first()
            if prog:
                prog_data = {
                    "xp": prog.xp or 0,
                    "level": prog.level or 0,
                    "premium": bool(prog.premium),
                    "claimed_free": prog.claimed_levels_free or [],
                    "claimed_premium": prog.claimed_levels_premium or [],
                }
            else:
                prog_data = {"xp": 0, "level": 0, "premium": False,
                             "claimed_free": [], "claimed_premium": []}

    return {
        "season": {
            "id": season.id, "name": season.name,
            "days_left": days_left,
            "end_at": end_at.isoformat() if end_at else None,
        },
        "progress": prog_data,
        "xp_per_level": PASS_XP_PER_LEVEL,
        "max_level": PASS_MAX_LEVEL,
        "premium_price": PREMIUM_PASS_PRICE,
        "rewards": [
            {
                "level": lvl,
                "free": PASS_REWARDS[lvl]["free"],
                "premium": PASS_REWARDS[lvl]["premium"],
            }
            for lvl in range(1, PASS_MAX_LEVEL + 1)
        ],
    }


@router.post("/pass/buy_premium")
def buy_premium(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    season = _ensure_active_season(db)
    prog = db.query(ReflexPassProgress).filter(
        ReflexPassProgress.player_id == me.id,
        ReflexPassProgress.season_id == season.id,
    ).with_for_update().first()
    if not prog:
        prog = ReflexPassProgress(
            player_id=me.id, season_id=season.id,
            xp=0, level=0, claimed_levels_free=[], claimed_levels_premium=[],
        )
        db.add(prog); db.flush()
    if prog.premium:
        return {"ok": False, "msg": "Премиум уже куплен"}
    if (me.coins or 0) < PREMIUM_PASS_PRICE:
        return {"ok": False, "msg": f"Нужно {PREMIUM_PASS_PRICE} 💰"}
    me.coins -= PREMIUM_PASS_PRICE
    prog.premium = True
    db.commit()
    return {"ok": True, "new_coins": me.coins}


@router.post("/pass/claim")
def claim_pass_reward(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    level = int((data or {}).get("level", 0))
    track = (data or {}).get("track", "free")  # free | premium
    if level < 1 or level > PASS_MAX_LEVEL:
        return {"ok": False, "msg": "Неверный уровень"}

    season = _ensure_active_season(db)
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    prog = db.query(ReflexPassProgress).filter(
        ReflexPassProgress.player_id == pid,
        ReflexPassProgress.season_id == season.id,
    ).with_for_update().first()
    if not prog or (prog.level or 0) < level:
        return {"ok": False, "msg": "Уровень не достигнут"}
    if track == "premium" and not prog.premium:
        return {"ok": False, "msg": "Премиум не активирован"}

    claimed = prog.claimed_levels_premium if track == "premium" else prog.claimed_levels_free
    if claimed is None: claimed = []
    if level in claimed:
        return {"ok": False, "msg": "Уже забрано"}

    reward = PASS_REWARDS.get(level, {}).get(track)
    if not reward:
        return {"ok": False, "msg": "Нет награды"}

    # Выдача
    if reward.get("type") == "coins":
        me.coins = (me.coins or 0) + int(reward.get("amount", 0))
    elif reward.get("type") == "theme":
        # Разблокируем тему (через achievement theme_owned_<id>)
        theme_id = reward["code"].replace("theme_", "")
        existing = db.query(ReflexAchievement).filter(
            ReflexAchievement.player_id == pid,
            ReflexAchievement.code == f"theme_owned_{theme_id}",
        ).first()
        if not existing:
            db.add(ReflexAchievement(player_id=pid, code=f"theme_owned_{theme_id}"))
    elif reward.get("type") == "vfx":
        vfx_id = reward["code"].replace("vfx_", "")
        existing = db.query(ReflexAchievement).filter(
            ReflexAchievement.player_id == pid,
            ReflexAchievement.code == f"vfx_owned_{vfx_id}",
        ).first()
        if not existing:
            db.add(ReflexAchievement(player_id=pid, code=f"vfx_owned_{vfx_id}"))
    elif reward.get("type") == "frame":
        fid = reward["code"].replace("frame_", "")
        existing = db.query(ReflexAchievement).filter(
            ReflexAchievement.player_id == pid,
            ReflexAchievement.code == f"frame_owned_{fid}",
        ).first()
        if not existing:
            db.add(ReflexAchievement(player_id=pid, code=f"frame_owned_{fid}"))

    # Обновляем claimed (пересоздаём список чтобы SQLAlchemy заметил изменение JSON)
    new_claimed = list(claimed) + [level]
    if track == "premium":
        prog.claimed_levels_premium = new_claimed
    else:
        prog.claimed_levels_free = new_claimed
    db.commit()
    return {"ok": True, "reward": reward, "new_coins": me.coins}


# ═══════════════════════════════════════════════════════
#   Победные эффекты (VFX) и аватары
# ═══════════════════════════════════════════════════════

VFX_CATALOG = {
    "confetti":  {"name": "Конфетти",   "price": 400},
    "fire":      {"name": "Огонь",      "price": 600},
    "lightning": {"name": "Молния",     "price": 800},
    "stars":     {"name": "Звёзды",     "price": 400},
    "explosion": {"name": "Взрыв",      "price": 1000},
}

# 30 аватаров: комбинации (градиент, эмоджи)
AVATAR_CATALOG = [
    {"id": f"a{i}", "emoji": e, "gradient": g, "price": 100 if i > 3 else 0}
    for i, (e, g) in enumerate([
        ("🦊", "linear-gradient(135deg,#ff7a29,#ffb86b)"),
        ("🐉", "linear-gradient(135deg,#26d67f,#7fe3b0)"),
        ("🦅", "linear-gradient(135deg,#7dd3fc,#a78bfa)"),
        ("🐺", "linear-gradient(135deg,#64748b,#1e293b)"),
        ("🦁", "linear-gradient(135deg,#fbbf24,#ea580c)"),
        ("🐸", "linear-gradient(135deg,#86efac,#16a34a)"),
        ("🦈", "linear-gradient(135deg,#0ea5e9,#1e40af)"),
        ("🐙", "linear-gradient(135deg,#a855f7,#c026d3)"),
        ("🦂", "linear-gradient(135deg,#991b1b,#450a0a)"),
        ("🐢", "linear-gradient(135deg,#22c55e,#14532d)"),
        ("🦇", "linear-gradient(135deg,#1e293b,#030712)"),
        ("🐧", "linear-gradient(135deg,#0f172a,#475569)"),
        ("🦉", "linear-gradient(135deg,#78350f,#fbbf24)"),
        ("🦜", "linear-gradient(135deg,#e11d48,#facc15)"),
        ("🦩", "linear-gradient(135deg,#f472b6,#fb7185)"),
        ("🐻", "linear-gradient(135deg,#92400e,#451a03)"),
        ("🐼", "linear-gradient(135deg,#e5e7eb,#1f2937)"),
        ("🦄", "linear-gradient(135deg,#c084fc,#f9a8d4)"),
        ("⚡", "linear-gradient(135deg,#fde047,#f59e0b)"),
        ("🔥", "linear-gradient(135deg,#ef4444,#f97316)"),
        ("💎", "linear-gradient(135deg,#06b6d4,#a78bfa)"),
        ("⭐", "linear-gradient(135deg,#fbbf24,#fef3c7)"),
        ("👾", "linear-gradient(135deg,#10b981,#059669)"),
        ("🤖", "linear-gradient(135deg,#6b7280,#1f2937)"),
        ("👻", "linear-gradient(135deg,#e0e7ff,#6366f1)"),
        ("🎭", "linear-gradient(135deg,#9333ea,#1e3a8a)"),
        ("🗡️", "linear-gradient(135deg,#78716c,#000000)"),
        ("🛡️", "linear-gradient(135deg,#3b82f6,#1e40af)"),
        ("💀", "linear-gradient(135deg,#1e293b,#991b1b)"),
        ("👑", "linear-gradient(135deg,#eab308,#a16207)"),
    ])
]


def _owned_vfx(db, player_id: int):
    rows = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("vfx_owned_%"),
    ).all()
    return {r.code[len("vfx_owned_"):] for r in rows}


def _equipped_vfx(db, player_id: int):
    r = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("vfx_equipped_%"),
    ).first()
    return r.code[len("vfx_equipped_"):] if r else None


def _owned_avatars(db, player_id: int):
    rows = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("avatar_owned_%"),
    ).all()
    owned = {r.code[len("avatar_owned_"):] for r in rows}
    # Первые 4 бесплатные
    for a in AVATAR_CATALOG[:4]:
        owned.add(a["id"])
    return owned


def _equipped_avatar(db, player_id: int):
    r = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("avatar_equipped_%"),
    ).first()
    return r.code[len("avatar_equipped_"):] if r else "a0"


@router.get("/shop/vfx")
def shop_vfx(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    owned = set(); equipped = None
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            pid = payload.get("player_id")
            owned = _owned_vfx(db, pid)
            equipped = _equipped_vfx(db, pid)
    return {
        "vfx": [
            {"id": k, "name": v["name"], "price": v["price"],
             "owned": k in owned, "equipped": k == equipped}
            for k, v in VFX_CATALOG.items()
        ],
    }


@router.post("/shop/buy_vfx")
def buy_vfx(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    vid = (data or {}).get("vfx_id", "")
    vfx = VFX_CATALOG.get(vid)
    if not vfx: return {"ok": False}
    owned = _owned_vfx(db, me.id)
    if vid in owned: return {"ok": False, "msg": "Уже куплен"}
    if (me.coins or 0) < vfx["price"]:
        return {"ok": False, "msg": f"Нужно {vfx['price']} 💰"}
    me.coins -= vfx["price"]
    db.add(ReflexAchievement(player_id=me.id, code=f"vfx_owned_{vid}"))
    db.commit()
    return {"ok": True, "new_coins": me.coins}


@router.post("/shop/equip_vfx")
def equip_vfx(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    vid = (data or {}).get("vfx_id", "")
    if vid not in VFX_CATALOG: return {"ok": False}
    if vid not in _owned_vfx(db, pid): return {"ok": False, "msg": "Не куплен"}
    db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("vfx_equipped_%"),
    ).delete(synchronize_session=False)
    db.add(ReflexAchievement(player_id=pid, code=f"vfx_equipped_{vid}"))
    db.commit()
    return {"ok": True}


@router.get("/shop/avatars")
def shop_avatars(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    owned = {a["id"] for a in AVATAR_CATALOG[:4]}; equipped = "a0"
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            pid = payload.get("player_id")
            owned = _owned_avatars(db, pid)
            equipped = _equipped_avatar(db, pid)
    return {
        "avatars": [
            {**a, "owned": a["id"] in owned, "equipped": a["id"] == equipped}
            for a in AVATAR_CATALOG
        ],
    }


@router.post("/shop/buy_avatar")
def buy_avatar(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    aid = (data or {}).get("avatar_id", "")
    av = next((a for a in AVATAR_CATALOG if a["id"] == aid), None)
    if not av: return {"ok": False}
    owned = _owned_avatars(db, me.id)
    if aid in owned: return {"ok": False, "msg": "Уже куплен"}
    if (me.coins or 0) < av["price"]:
        return {"ok": False, "msg": f"Нужно {av['price']} 💰"}
    me.coins -= av["price"]
    db.add(ReflexAchievement(player_id=me.id, code=f"avatar_owned_{aid}"))
    db.commit()
    return {"ok": True, "new_coins": me.coins}


@router.post("/shop/equip_avatar")
def equip_avatar(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    aid = (data or {}).get("avatar_id", "")
    if not any(a["id"] == aid for a in AVATAR_CATALOG): return {"ok": False}
    if aid not in _owned_avatars(db, pid): return {"ok": False, "msg": "Не куплен"}
    db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("avatar_equipped_%"),
    ).delete(synchronize_session=False)
    db.add(ReflexAchievement(player_id=pid, code=f"avatar_equipped_{aid}"))
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════
#   Кейсы + Коллекция предметов
# ═══════════════════════════════════════════════════════

# Типы предметов по категориям: 3 типа × 10 штук = 30 предметов в кейсе
# Редкости: common(50%), uncommon(25%), rare(15%), epic(8%), legendary(2%)
RARITIES = [
    ("common", 50, "⬜"), ("uncommon", 25, "🟩"), ("rare", 15, "🟦"),
    ("epic", 8, "🟪"), ("legendary", 2, "🟨"),
]

CASE_CATALOG = {
    "reaction": {
        "name": "Кейс Рефлексов", "icon": "🔴", "price": 150,
        "types": [
            {"type": "gloves", "name_prefix": "Перчатки", "icon": "🧤"},
            {"type": "energy", "name_prefix": "Энергетик", "icon": "⚡"},
            {"type": "lens",   "name_prefix": "Линзы",     "icon": "👁️"},
        ],
    },
    "logic": {
        "name": "Кейс Логики", "icon": "🟡", "price": 150,
        "types": [
            {"type": "calculator", "name_prefix": "Калькулятор", "icon": "🧮"},
            {"type": "glasses",    "name_prefix": "Очки",        "icon": "👓"},
            {"type": "book",       "name_prefix": "Книга",       "icon": "📕"},
        ],
    },
    "memory": {
        "name": "Кейс Памяти", "icon": "🟢", "price": 150,
        "types": [
            {"type": "notebook", "name_prefix": "Ноутбук",  "icon": "💻"},
            {"type": "diary",    "name_prefix": "Дневник",   "icon": "📓"},
            {"type": "crystal",  "name_prefix": "Кристалл",  "icon": "🔮"},
        ],
    },
    "coordination": {
        "name": "Кейс Координации", "icon": "🔵", "price": 150,
        "types": [
            {"type": "joystick", "name_prefix": "Джойстик", "icon": "🕹️"},
            {"type": "stylus",   "name_prefix": "Стилус",    "icon": "✒️"},
            {"type": "watch",    "name_prefix": "Часы",       "icon": "⌚"},
        ],
    },
    "trivia": {
        "name": "Кейс Эрудиции", "icon": "🟣", "price": 150,
        "types": [
            {"type": "encyclopedia", "name_prefix": "Энциклопедия", "icon": "📖"},
            {"type": "globe",        "name_prefix": "Глобус",       "icon": "🌐"},
            {"type": "owl",          "name_prefix": "Сова",          "icon": "🦉"},
        ],
    },
}

RARITY_NAMES = {
    "common": "Обычный", "uncommon": "Необычный", "rare": "Редкий",
    "epic": "Эпический", "legendary": "Легендарный",
}

# Все 150 предметов (5 кейсов × 3 типа × 10 уровней/вариантов)
def _build_full_item_catalog():
    items = {}
    for cat_id, cat in CASE_CATALOG.items():
        for t in cat["types"]:
            for level in range(1, 11):
                # Редкость по уровню: 1-4 common, 5-6 uncommon, 7-8 rare, 9 epic, 10 legendary
                if level <= 4: rarity = "common"
                elif level <= 6: rarity = "uncommon"
                elif level <= 8: rarity = "rare"
                elif level == 9: rarity = "epic"
                else: rarity = "legendary"
                item_id = f"{cat_id}_{t['type']}_{level}"
                items[item_id] = {
                    "id": item_id,
                    "category": cat_id,
                    "type": t["type"],
                    "type_icon": t["icon"],
                    "name": f"{t['name_prefix']} ур.{level}",
                    "rarity": rarity,
                    "rarity_name": RARITY_NAMES[rarity],
                    "level": level,
                    "boost_pct": level,  # +1% per level, legendary = +10%
                }
    return items

FULL_ITEM_CATALOG = _build_full_item_catalog()


@router.get("/cases")
def reflex_cases(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Список доступных кейсов."""
    return {
        "cases": [
            {"id": k, "name": v["name"], "icon": v["icon"], "price": v["price"],
             "types": [{"type": t["type"], "name": t["name_prefix"], "icon": t["icon"]} for t in v["types"]]}
            for k, v in CASE_CATALOG.items()
        ],
    }


@router.post("/cases/open")
def open_case(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    case_id = (data or {}).get("case_id", "")
    case_def = CASE_CATALOG.get(case_id)
    if not case_def: return {"ok": False, "msg": "Кейс не найден"}
    if (me.coins or 0) < case_def["price"]:
        return {"ok": False, "msg": f"Нужно {case_def['price']} 💰"}
    me.coins -= case_def["price"]

    # Дроп: выбираем предмет по вероятности
    roll = _rnd.random() * 100
    cumul = 0
    chosen_rarity = "common"
    for rarity, pct, _ in RARITIES:
        cumul += pct
        if roll < cumul:
            chosen_rarity = rarity; break

    # Выбираем случайный предмет этой редкости из этого кейса
    pool = [i for i in FULL_ITEM_CATALOG.values()
            if i["category"] == case_id and i["rarity"] == chosen_rarity]
    if not pool:
        pool = [i for i in FULL_ITEM_CATALOG.values() if i["category"] == case_id]
    item = _rnd.choice(pool) if pool else None
    if not item:
        db.commit()
        return {"ok": False, "msg": "Нет предметов в кейсе"}

    # Сохраняем в коллекцию игрока (через ReflexAchievement c кодом item_owned_<item_id>)
    code = f"item_owned_{item['id']}"
    exists = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == me.id,
        ReflexAchievement.code == code,
    ).first()
    is_dupe = bool(exists)
    if not exists:
        db.add(ReflexAchievement(player_id=me.id, code=code))
    else:
        # Дубликат — конвертируем в монеты (по формуле)
        dupe_coins = {"common": 10, "uncommon": 25, "rare": 60, "epic": 150, "legendary": 400}
        me.coins = (me.coins or 0) + dupe_coins.get(item["rarity"], 10)

    db.commit()

    rarity_icon = {r[0]: r[2] for r in RARITIES}.get(item["rarity"], "⬜")
    return {
        "ok": True,
        "item": item,
        "rarity_icon": rarity_icon,
        "is_dupe": is_dupe,
        "new_coins": me.coins,
    }


@router.get("/collection")
def get_collection(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Все предметы игрока из коллекции."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    owned_codes = {r.code[len("item_owned_"):] for r in db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("item_owned_%"),
    ).all()}
    equipped_code = None
    eq = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("item_equipped_%"),
    ).first()
    if eq:
        equipped_code = eq.code[len("item_equipped_"):]
    # Группировка по категориям
    by_cat = {}
    for item_id, item in FULL_ITEM_CATALOG.items():
        cat = item["category"]
        if cat not in by_cat: by_cat[cat] = []
        by_cat[cat].append({
            **item,
            "owned": item_id in owned_codes,
            "equipped": item_id == equipped_code,
        })
    return {
        "authenticated": True,
        "total_items": len(FULL_ITEM_CATALOG),
        "owned_count": len(owned_codes),
        "categories": by_cat,
        "equipped": equipped_code,
    }


@router.post("/collection/equip")
def equip_item(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    item_id = (data or {}).get("item_id", "")
    if item_id not in FULL_ITEM_CATALOG: return {"ok": False}
    # Проверяем владение
    code = f"item_owned_{item_id}"
    if not db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid, ReflexAchievement.code == code,
    ).first():
        return {"ok": False, "msg": "Нет в коллекции"}
    db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("item_equipped_%"),
    ).delete(synchronize_session=False)
    db.add(ReflexAchievement(player_id=pid, code=f"item_equipped_{item_id}"))
    db.commit()
    return {"ok": True}


# ─── Онбординг: отметить как пройденный ───
@router.post("/onboarded")
def mark_onboarded(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    me = db.query(Player).filter(Player.id == payload.get("player_id")).with_for_update().first()
    if not me: return {"ok": False}
    me.reflex_onboarded = True
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════
#   Daily Login Streak
# ═══════════════════════════════════════════════════════

# Награды по дням streak (0-indexed: день 1 = STREAK_REWARDS[0])
STREAK_REWARDS = [10, 20, 30, 50, 100, 150, 200]  # дальше по 200


def _streak_reward_for(day: int) -> int:
    if day <= 0:
        return 0
    if day <= len(STREAK_REWARDS):
        return STREAK_REWARDS[day - 1]
    return 200


def _tick_streak(db: Session, player_id: int) -> ReflexLoginStreak:
    """Обновляет streak при заходе. Если вчера был login — +1. Если 2+ пропущено — сброс в 1."""
    today = _today_str()
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    yesterday = (_dt.now(_tz.utc) - _td(days=1)).strftime("%Y-%m-%d")

    row = db.query(ReflexLoginStreak).filter(
        ReflexLoginStreak.player_id == player_id
    ).with_for_update().first()

    if not row:
        row = ReflexLoginStreak(
            player_id=player_id,
            current_streak=1,
            max_streak=1,
            last_login_date=today,
            total_days_logged=1,
        )
        db.add(row)
        db.commit()
        return row

    if row.last_login_date == today:
        return row  # уже считали сегодня

    if row.last_login_date == yesterday:
        row.current_streak = (row.current_streak or 0) + 1
    else:
        row.current_streak = 1
    row.max_streak = max(row.max_streak or 0, row.current_streak)
    row.last_login_date = today
    row.total_days_logged = (row.total_days_logged or 0) + 1
    db.commit()
    return row


@router.get("/streak")
def streak_info(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    pid = payload.get("player_id")
    today = _today_str()
    row = _tick_streak(db, pid)
    already_claimed = row.last_claimed_date == today
    current = row.current_streak or 0
    return {
        "authenticated": True,
        "current_streak": current,
        "max_streak": row.max_streak or 0,
        "total_days": row.total_days_logged or 0,
        "already_claimed_today": already_claimed,
        "today_reward": _streak_reward_for(current),
        "next_reward_tomorrow": _streak_reward_for(current + 1),
    }


@router.post("/streak/claim")
def streak_claim(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    pid = payload.get("player_id")
    today = _today_str()
    row = _tick_streak(db, pid)
    if row.last_claimed_date == today:
        return {"ok": False, "msg": "Уже получено сегодня"}
    reward = _streak_reward_for(row.current_streak or 1)
    p = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if p:
        p.coins = (p.coins or 0) + reward
    row.last_claimed_date = today
    db.commit()
    return {"ok": True, "reward": reward, "new_coins": p.coins if p else 0, "streak": row.current_streak}


# ═══════════════════════════════════════════════════════
#   Event logging (простая аналитика)
# ═══════════════════════════════════════════════════════

@router.post("/event")
def log_event(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Клиент шлёт события: { type: "match_start", payload: {...} }"""
    pid = None
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            pid = payload.get("player_id")
    ev_type = (data or {}).get("type", "")[:64]
    if not ev_type:
        return {"ok": False}
    try:
        db.add(ReflexEvent(
            player_id=pid,
            event_type=ev_type,
            payload=(data or {}).get("payload") or {},
        ))
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True}


@router.get("/stats/basic")
def stats_basic(db: Session = Depends(get_db)):
    """Простая агрегация за сегодня/неделю (без auth — для внутреннего просмотра)."""
    from sqlalchemy import func as sqlfunc
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = _dt.now(_tz.utc)
    day_ago = now - _td(days=1)
    week_ago = now - _td(days=7)

    total_players = db.query(sqlfunc.count(Player.id)).scalar() or 0
    guests = db.query(sqlfunc.count(Player.id)).filter(Player.is_guest == True).scalar() or 0
    registered = total_players - guests

    matches_total = db.query(sqlfunc.count(ReflexMatch.id)).filter(
        ReflexMatch.status == "finished"
    ).scalar() or 0

    matches_24h = db.query(sqlfunc.count(ReflexMatch.id)).filter(
        ReflexMatch.status == "finished",
        ReflexMatch.finished_at >= day_ago,
    ).scalar() or 0

    matches_7d = db.query(sqlfunc.count(ReflexMatch.id)).filter(
        ReflexMatch.status == "finished",
        ReflexMatch.finished_at >= week_ago,
    ).scalar() or 0

    events_24h = db.query(sqlfunc.count(ReflexEvent.id)).filter(
        ReflexEvent.created_at >= day_ago
    ).scalar() or 0

    # DAU за 24 часа = уникальные player_id в ReflexEvent
    dau = db.query(sqlfunc.count(sqlfunc.distinct(ReflexEvent.player_id))).filter(
        ReflexEvent.created_at >= day_ago,
        ReflexEvent.player_id != None,
    ).scalar() or 0

    return {
        "players": {"total": total_players, "guests": guests, "registered": registered},
        "matches": {"total": matches_total, "last_24h": matches_24h, "last_7d": matches_7d},
        "events": {"last_24h": events_24h},
        "dau_24h": dau,
    }


# ═══════════════════════════════════════════════════════
#   Push subscriptions
# ═══════════════════════════════════════════════════════

import os as _os

# VAPID public key (для subscribe). Секретный ключ — в env.
VAPID_PUBLIC_KEY = _os.environ.get("VAPID_PUBLIC_KEY", "")


@router.get("/push/vapid_key")
def push_vapid_key():
    return {"key": VAPID_PUBLIC_KEY}


@router.post("/push/subscribe")
def push_subscribe(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    pid = payload.get("player_id")
    endpoint = (data or {}).get("endpoint", "")
    keys = (data or {}).get("keys", {})
    if not endpoint or not keys.get("auth") or not keys.get("p256dh"):
        return {"ok": False, "msg": "Некорректная подписка"}
    # Upsert по endpoint
    existing = db.query(ReflexPushSubscription).filter(
        ReflexPushSubscription.endpoint == endpoint
    ).first()
    if existing:
        existing.player_id = pid
        existing.keys_json = keys
    else:
        db.add(ReflexPushSubscription(
            player_id=pid, endpoint=endpoint, keys_json=keys,
        ))
    db.commit()
    return {"ok": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    endpoint = (data or {}).get("endpoint", "")
    if not endpoint:
        return {"ok": False}
    db.query(ReflexPushSubscription).filter(
        ReflexPushSubscription.endpoint == endpoint
    ).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
#   БУСТЫ: итемы + стрик + события + сеты
# ═══════════════════════════════════════════════════════════════

# Кап общего буста чтобы не было pay-to-win
MAX_BOOST_PCT = 50

# Категория недели: определяется детерминированно по номеру ISO-недели
WEEKLY_EVENT_CATEGORIES = ["reaction", "logic", "memory", "coordination", "trivia"]
WEEKLY_EVENT_BONUS_PCT = 25  # если в матче была игра «категории недели»

STREAK_COIN_BONUS_CAP = 10  # до +10% coins от стрика (1% за день до 10)
SET_BONUS_PCT = 5            # +5% если собран сет (3 типа одного уровня в категории)

RETURN_BONUS_DAYS = 3         # через N дней без захода — даётся бонус
RETURN_BONUS_COINS = 300      # и сколько монет


def _current_weekly_event() -> dict:
    """Категория недели меняется раз в 7 дней детерминированно."""
    from datetime import datetime as _dt, timezone as _tz
    iso_week = _dt.now(_tz.utc).isocalendar().week
    cat = WEEKLY_EVENT_CATEGORIES[iso_week % len(WEEKLY_EVENT_CATEGORIES)]
    # Начало и конец текущей недели
    now = _dt.now(_tz.utc)
    # Понедельник = weekday 0
    from datetime import timedelta as _td
    monday = now - _td(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = monday + _td(days=7)
    return {
        "category": cat,
        "bonus_pct": WEEKLY_EVENT_BONUS_PCT,
        "week_num": iso_week,
        "starts_at": monday.isoformat(),
        "ends_at": sunday_end.isoformat(),
    }


CATEGORY_NAMES_RU = {
    "reaction": "Реакция",
    "logic": "Логика",
    "memory": "Память",
    "coordination": "Координация",
    "trivia": "Эрудиция",
}


def _player_equipped_item(db: Session, player_id: int) -> Optional[dict]:
    eq = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("item_equipped_%"),
    ).first()
    if not eq:
        return None
    iid = eq.code[len("item_equipped_"):]
    return FULL_ITEM_CATALOG.get(iid)


def _player_owned_items(db: Session, player_id: int) -> set:
    return {r.code[len("item_owned_"):] for r in db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code.like("item_owned_%"),
    ).all()}


def _player_sets(owned_ids: set) -> dict:
    """Возвращает {category: [levels_сет_собран]}."""
    # Сет = 3 типа одной категории одного уровня
    type_levels = {}  # (cat, level) -> set(type)
    for iid in owned_ids:
        it = FULL_ITEM_CATALOG.get(iid)
        if not it: continue
        k = (it["category"], it["level"])
        type_levels.setdefault(k, set()).add(it["type"])
    sets = {}
    for (cat, lvl), types in type_levels.items():
        if len(types) >= 3:  # все 3 типа собраны
            sets.setdefault(cat, []).append(lvl)
    return sets


def _streak_current(db: Session, player_id: int) -> int:
    row = db.query(ReflexLoginStreak).filter(
        ReflexLoginStreak.player_id == player_id
    ).first()
    return row.current_streak or 0 if row else 0


def compute_boost_info(db: Session, player_id: int, categories_in_match: list) -> dict:
    """
    Возвращает dict:
    {
      item: {name, pct, applies},
      streak: {days, pct},
      set: {pct, categories_active},
      event: {category, pct, applies},
      total_pct (capped), multiplier (1 + total/100),
    }
    """
    # 1. Item boost
    item = _player_equipped_item(db, player_id)
    item_pct = 0
    item_info = None
    if item:
        item_applies = (not categories_in_match) or (item["category"] in categories_in_match)
        # Если надетый не попал в матч — даём половину
        item_pct = item["boost_pct"] if item_applies else max(1, item["boost_pct"] // 2)
        item_info = {
            "id": item["id"], "name": item["name"], "category": item["category"],
            "boost_pct": item["boost_pct"], "applied_pct": item_pct,
            "applies_fully": item_applies,
        }

    # 2. Streak bonus
    streak_days = _streak_current(db, player_id)
    streak_pct = min(STREAK_COIN_BONUS_CAP, streak_days)

    # 3. Sets bonus (если в матче была категория где собран сет)
    owned = _player_owned_items(db, player_id)
    sets = _player_sets(owned)
    set_pct = 0
    set_cats_active = []
    for cat in categories_in_match:
        if cat in sets:
            set_pct += SET_BONUS_PCT
            set_cats_active.append(cat)

    # 4. Event bonus
    ev = _current_weekly_event()
    event_pct = 0
    if ev["category"] in categories_in_match:
        event_pct = ev["bonus_pct"]

    total = item_pct + streak_pct + set_pct + event_pct
    total_capped = min(MAX_BOOST_PCT, total)
    return {
        "item": item_info,
        "streak": {"days": streak_days, "pct": streak_pct},
        "set": {"pct": set_pct, "categories_active": set_cats_active, "all_sets": {k: sorted(v) for k, v in sets.items()}},
        "event": {"category": ev["category"], "category_name": CATEGORY_NAMES_RU.get(ev["category"], ev["category"]), "pct": event_pct, "active": event_pct > 0},
        "total_pct": total_capped,
        "raw_total_pct": total,
        "multiplier": 1 + total_capped / 100.0,
    }


def apply_coin_boost(db: Session, player_id: int, base_coins: int, categories_in_match: list) -> tuple:
    """Возвращает (final_coins, boost_info). Не коммитит — просто считает."""
    if base_coins <= 0:
        return base_coins, None
    info = compute_boost_info(db, player_id, categories_in_match or [])
    final = int(round(base_coins * info["multiplier"]))
    return final, info


@router.get("/boost")
def get_boost_info(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Показываем игроку: какие бусты активны сейчас (для UI "мои бусты")."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    pid = payload.get("player_id")
    # Полный расчёт «если бы матч был во всех 5 категориях»
    info = compute_boost_info(db, pid, WEEKLY_EVENT_CATEGORIES)
    ev = _current_weekly_event()
    return {
        "authenticated": True,
        "boost": info,
        "event": ev,
        "cap_pct": MAX_BOOST_PCT,
    }


# ═══════════════════════════════════════════════════════════════
#   WEEKLY EVENT: публичный endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/weekly_event")
def weekly_event(db: Session = Depends(get_db)):
    ev = _current_weekly_event()
    return {
        **ev,
        "category_name": CATEGORY_NAMES_RU.get(ev["category"], ev["category"]),
    }


# ═══════════════════════════════════════════════════════════════
#   RETURN BONUS: если >= RETURN_BONUS_DAYS не заходил — даём монеты
# ═══════════════════════════════════════════════════════════════

@router.get("/return_bonus")
def return_bonus_info(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    pid = payload.get("player_id")
    row = db.query(ReflexLoginStreak).filter(
        ReflexLoginStreak.player_id == pid
    ).first()
    today = _today_str()
    if not row or not row.last_login_date:
        return {"authenticated": True, "available": False, "days_away": 0}
    # Уже забрал сегодня?
    taken = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code == f"return_bonus_{today}",
    ).first()
    if taken:
        return {"authenticated": True, "available": False, "days_away": 0, "claimed_today": True}
    from datetime import datetime as _dt
    try:
        last = _dt.strptime(row.last_login_date, "%Y-%m-%d")
        now = _dt.strptime(today, "%Y-%m-%d")
        days_away = (now - last).days
    except Exception:
        days_away = 0
    return {
        "authenticated": True,
        "available": days_away >= RETURN_BONUS_DAYS,
        "days_away": days_away,
        "reward_coins": RETURN_BONUS_COINS,
        "threshold_days": RETURN_BONUS_DAYS,
    }


@router.post("/return_bonus/claim")
def return_bonus_claim(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    pid = payload.get("player_id")
    # Те же проверки, атомарно
    row = db.query(ReflexLoginStreak).filter(
        ReflexLoginStreak.player_id == pid
    ).first()
    today = _today_str()
    taken = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code == f"return_bonus_{today}",
    ).first()
    if taken:
        return {"ok": False, "msg": "Уже получено сегодня"}
    from datetime import datetime as _dt
    days_away = 0
    try:
        last = _dt.strptime(row.last_login_date, "%Y-%m-%d") if row and row.last_login_date else None
        if last:
            days_away = (_dt.strptime(today, "%Y-%m-%d") - last).days
    except Exception:
        pass
    if days_away < RETURN_BONUS_DAYS:
        return {"ok": False, "msg": f"Нужно не заходить {RETURN_BONUS_DAYS}+ дней"}
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if not me:
        return {"ok": False}
    me.coins = (me.coins or 0) + RETURN_BONUS_COINS
    db.add(ReflexAchievement(player_id=pid, code=f"return_bonus_{today}"))
    db.commit()
    return {"ok": True, "coins_awarded": RETURN_BONUS_COINS, "new_coins": me.coins}


# ═══════════════════════════════════════════════════════════════
#   TITLES: получены за достижения, игрок выбирает активный
# ═══════════════════════════════════════════════════════════════

TITLES_CATALOG = {
    # code → {name, condition_type, threshold, icon}
    "t_first_blood":  {"name": "Первая кровь",       "requires": "first_win",  "icon": "🩸"},
    "t_veteran":      {"name": "Ветеран",            "requires": "win_10",      "icon": "🎖️"},
    "t_perfectionist":{"name": "Перфекционист",      "requires": "perfect",     "icon": "💯"},
    "t_master_jack":  {"name": "Мастер на все руки", "requires": "diverse",     "icon": "🎭"},
    "t_comeback_kid": {"name": "Камбекер",           "requires": "comeback",    "icon": "🔥"},
    # Титулы за наборы предметов
    "t_reaction_lord":     {"name": "Повелитель реакции",   "requires_set_cat": "reaction",     "icon": "🔴"},
    "t_logic_archon":      {"name": "Архон Логики",         "requires_set_cat": "logic",        "icon": "🟡"},
    "t_memory_sage":       {"name": "Мудрец Памяти",        "requires_set_cat": "memory",       "icon": "🟢"},
    "t_coord_maestro":     {"name": "Маэстро Координации",  "requires_set_cat": "coordination", "icon": "🔵"},
    "t_trivia_oracle":     {"name": "Оракул Эрудиции",      "requires_set_cat": "trivia",       "icon": "🟣"},
    # Титулы за стрик
    "t_streak_7":  {"name": "Неделя без остановки", "requires_streak": 7,  "icon": "🔥"},
    "t_streak_30": {"name": "Месячник",              "requires_streak": 30, "icon": "☄️"},
}


def _titles_owned(db: Session, player_id: int) -> set:
    owned = set()
    # Достижения
    ach_codes = {r.code for r in db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
    ).all()}
    # Сеты
    owned_items = {c[len("item_owned_"):] for c in ach_codes if c.startswith("item_owned_")}
    sets = _player_sets(owned_items)
    # Стрик
    row = db.query(ReflexLoginStreak).filter(
        ReflexLoginStreak.player_id == player_id
    ).first()
    max_streak = (row.max_streak if row else 0) or 0
    for tcode, t in TITLES_CATALOG.items():
        if "requires" in t and t["requires"] in ach_codes:
            owned.add(tcode)
        if "requires_set_cat" in t and t["requires_set_cat"] in sets:
            owned.add(tcode)
        if "requires_streak" in t and max_streak >= t["requires_streak"]:
            owned.add(tcode)
    return owned


@router.get("/titles")
def titles_list(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"authenticated": False}
    pid = payload.get("player_id")
    owned = _titles_owned(db, pid)
    eq = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("title_equipped_%"),
    ).first()
    active = eq.code[len("title_equipped_"):] if eq else None
    return {
        "authenticated": True,
        "titles": [
            {"id": k, "name": v["name"], "icon": v["icon"],
             "owned": k in owned, "active": k == active}
            for k, v in TITLES_CATALOG.items()
        ],
        "active": active,
    }


@router.post("/titles/equip")
def titles_equip(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload:
        return {"ok": False}
    pid = payload.get("player_id")
    tid = (data or {}).get("title_id", "")
    if tid and tid not in TITLES_CATALOG:
        return {"ok": False, "msg": "Нет такого титула"}
    if tid:
        owned = _titles_owned(db, pid)
        if tid not in owned:
            return {"ok": False, "msg": "Не открыт"}
    db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == pid,
        ReflexAchievement.code.like("title_equipped_%"),
    ).delete(synchronize_session=False)
    if tid:
        db.add(ReflexAchievement(player_id=pid, code=f"title_equipped_{tid}"))
    db.commit()
    return {"ok": True, "active": tid or None}


# ═══════════════════════════════════════════════════════════════
#   ГЕМЫ 💎: премиум-валюта
# ═══════════════════════════════════════════════════════════════

GEM_SHOP = {
    # gem_id → {name, price_gems, description, action}
    "guaranteed_rare": {"name": "Гарантированный Rare+", "price": 100, "desc": "Открыть кейс с гарантией Rare или выше", "icon": "🎁"},
    "case_bundle_3":   {"name": "3 кейса любой категории","price": 250, "desc": "3 обычных кейса на выбор", "icon": "📦"},
    "bp_premium":      {"name": "Battle Pass Premium",     "price": 299, "desc": "Разблокировать премиум-трек Battle Pass", "icon": "⚔️"},
    "double_xp_24h":   {"name": "×2 XP на 24 часа",         "price": 80,  "desc": "Удваивает XP за все матчи следующие 24 часа", "icon": "⚡"},
    "coins_5000":      {"name": "5000 монет",               "price": 150, "desc": "Быстрый пакет монет", "icon": "💰"},
}

# Пакеты гемов за реальные деньги (Telegram Stars XTR: 1★ ≈ $0.013)
GEM_PACKS = {
    "gems_100":  {"gems": 100,  "price_stars": 70,   "price_rub": 99,    "name": "Малый сундук"},
    "gems_500":  {"gems": 500,  "price_stars": 330,  "price_rub": 499,   "name": "Сундук", "bonus_pct": 10},
    "gems_1200": {"gems": 1200, "price_stars": 650,  "price_rub": 999,   "name": "Большой сундук", "bonus_pct": 20},
    "gems_3000": {"gems": 3000, "price_stars": 1500, "price_rub": 2299,  "name": "Сокровище", "bonus_pct": 25},
}


def _grant_gems(db: Session, player_id: int, amount: int, reason: str = ""):
    """Выдать гемы + залогировать событие."""
    if amount <= 0: return
    me = db.query(Player).filter(Player.id == player_id).with_for_update().first()
    if not me: return
    me.gems = (me.gems or 0) + amount
    try:
        db.add(ReflexEvent(player_id=player_id, event_type="gems_granted",
                           payload={"amount": amount, "reason": reason, "new_balance": me.gems}))
    except Exception:
        pass
    db.commit()


@router.get("/gems/shop")
def gems_shop(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    return {
        "spend_items": [
            {"id": k, **v} for k, v in GEM_SHOP.items()
        ],
        "buy_packs": [
            {"id": k, **v} for k, v in GEM_PACKS.items()
        ],
    }


@router.post("/gems/spend")
def gems_spend(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    item_id = (data or {}).get("item_id", "")
    item = GEM_SHOP.get(item_id)
    if not item: return {"ok": False, "msg": "Нет в магазине"}
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if not me: return {"ok": False}
    if (me.gems or 0) < item["price"]:
        return {"ok": False, "msg": f"Нужно {item['price']} 💎"}
    me.gems -= item["price"]
    awarded = {}
    try:
        if item_id == "coins_5000":
            me.coins = (me.coins or 0) + 5000
            awarded["coins"] = 5000
        elif item_id == "bp_premium":
            # Помечаем премиум для активного сезона
            season = _ensure_active_season(db)
            prog = db.query(ReflexPassProgress).filter(
                ReflexPassProgress.player_id == pid,
                ReflexPassProgress.season_id == season.id,
            ).with_for_update().first()
            if not prog:
                prog = ReflexPassProgress(player_id=pid, season_id=season.id, xp=0, level=0, premium=True)
                db.add(prog)
            else:
                prog.premium = True
            awarded["bp_premium"] = True
        elif item_id == "double_xp_24h":
            # Помечаем флагом через ach code с TTL (используем дату окончания)
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            until = (_dt.now(_tz.utc) + _td(hours=24)).strftime("%Y-%m-%dT%H:%M")
            db.add(ReflexAchievement(player_id=pid, code=f"booster_xp_until_{until}"))
            awarded["xp_boost_until"] = until
        elif item_id == "guaranteed_rare":
            # Выдаём флаг на следующий open_case
            db.add(ReflexAchievement(player_id=pid, code="booster_next_case_rare"))
            awarded["next_case_rare"] = True
        elif item_id == "case_bundle_3":
            # Выдаём 3 "жетона" на бесплатное открытие
            for i in range(3):
                db.add(ReflexAchievement(player_id=pid, code=f"case_token_{i}_{_rnd.randint(1000, 99999)}"))
            awarded["case_tokens"] = 3
    except Exception as e:
        print(f"[gems_spend] award error: {e}")
    try:
        db.add(ReflexEvent(player_id=pid, event_type="gems_spent",
                           payload={"item_id": item_id, "gems": item["price"], "awarded": awarded}))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "new_gems": me.gems, "new_coins": me.coins, "awarded": awarded}


# ═══════════════════════════════════════════════════════════════
#   TG STARS: создание invoice + webhook
# ═══════════════════════════════════════════════════════════════

import os as _os

TG_BOT_TOKEN = _os.environ.get("TG_BOT_TOKEN", "")
TG_STARS_ENABLED = bool(TG_BOT_TOKEN)


@router.post("/payments/create_invoice")
def create_invoice(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Создаёт invoice через Telegram Bot API (или dev stub)."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    pack_id = (data or {}).get("pack_id", "")
    pack = GEM_PACKS.get(pack_id)
    if not pack: return {"ok": False, "msg": "Нет такого пакета"}
    # Создаём pending-payment в БД
    pay = ReflexPayment(
        player_id=pid,
        provider="tg_stars" if TG_STARS_ENABLED else "dev",
        product_id=pack_id,
        amount_minor=pack["price_stars"],
        currency="XTR",
        status="pending",
        gems_granted=0,
    )
    db.add(pay)
    db.commit()
    if not TG_STARS_ENABLED:
        return {
            "ok": True, "mode": "dev",
            "payment_id": pay.id,
            "msg": "Dev-режим: оплата через TG Stars не активна. Для активации задать TG_BOT_TOKEN.",
        }
    # В проде — бот создаёт invoice через createInvoiceLink
    import urllib.request, urllib.parse, json as _json
    title = pack["name"]
    description = f"{pack['gems']} 💎 для Reflex Arena"
    prices = [{"label": title, "amount": pack["price_stars"]}]
    api_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createInvoiceLink"
    body = urllib.parse.urlencode({
        "title": title,
        "description": description,
        "payload": f"gems:{pay.id}:{pack_id}:{pid}",
        "currency": "XTR",
        "prices": _json.dumps(prices),
    }).encode("utf-8")
    try:
        req = urllib.request.Request(api_url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ans = _json.loads(resp.read().decode("utf-8"))
        if ans.get("ok") and ans.get("result"):
            pay.external_id = ans["result"]
            db.commit()
            return {"ok": True, "mode": "tg_stars", "invoice_url": ans["result"], "payment_id": pay.id}
    except Exception as e:
        print(f"[tg stars invoice] {e}")
    return {"ok": False, "msg": "Не удалось создать invoice"}


@router.post("/payments/webhook/tg")
def payments_webhook_tg(data: dict, db: Session = Depends(get_db)):
    """Webhook от Telegram — successful_payment. Для prod."""
    sp = (data or {}).get("successful_payment") or {}
    if not sp:
        return {"ok": True}
    invoice_payload = sp.get("invoice_payload", "")
    # Формат: gems:<payment_id>:<pack_id>:<player_id>
    try:
        parts = invoice_payload.split(":")
        payment_id = int(parts[1]); pack_id = parts[2]; pid = int(parts[3])
    except Exception:
        return {"ok": False}
    pay = db.query(ReflexPayment).filter(ReflexPayment.id == payment_id).with_for_update().first()
    if not pay or pay.status == "completed":
        return {"ok": True}
    pack = GEM_PACKS.get(pack_id)
    if not pack: return {"ok": False}
    pay.status = "completed"
    from datetime import datetime as _dt, timezone as _tz
    pay.completed_at = _dt.now(_tz.utc)
    pay.payload = sp
    total_gems = pack["gems"] + int(pack["gems"] * (pack.get("bonus_pct", 0) / 100))
    pay.gems_granted = total_gems
    db.commit()
    _grant_gems(db, pid, total_gems, reason=f"tg_stars_{pack_id}")
    return {"ok": True}


@router.post("/payments/dev_complete")
def payments_dev_complete(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """DEV-режим: завершает платёж без реальной оплаты (для тестирования)."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    if TG_STARS_ENABLED:
        # В проде запрещаем
        return {"ok": False, "msg": "Только dev-режим"}
    payment_id = (data or {}).get("payment_id")
    pay = db.query(ReflexPayment).filter(
        ReflexPayment.id == payment_id,
        ReflexPayment.player_id == pid,
    ).with_for_update().first()
    if not pay or pay.status == "completed":
        return {"ok": False, "msg": "Платёж не найден или уже завершён"}
    pack = GEM_PACKS.get(pay.product_id)
    if not pack: return {"ok": False}
    pay.status = "completed"
    from datetime import datetime as _dt, timezone as _tz
    pay.completed_at = _dt.now(_tz.utc)
    total_gems = pack["gems"] + int(pack["gems"] * (pack.get("bonus_pct", 0) / 100))
    pay.gems_granted = total_gems
    db.commit()
    _grant_gems(db, pid, total_gems, reason=f"dev_{pay.product_id}")
    me = db.query(Player).filter(Player.id == pid).first()
    return {"ok": True, "new_gems": me.gems if me else 0, "gems_granted": total_gems}


# ═══════════════════════════════════════════════════════════════
#   КЛУБЫ
# ═══════════════════════════════════════════════════════════════

CLUB_CREATE_PRICE_COINS = 2000
CLUB_MAX_MEMBERS = 20


@router.post("/clubs/create")
def clubs_create(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    name = (data or {}).get("name", "").strip()
    tag = (data or {}).get("tag", "").strip().upper()
    desc = (data or {}).get("description", "").strip()[:200]
    icon = (data or {}).get("icon", "🏰")
    if not name or len(name) < 3 or len(name) > 30: return {"ok": False, "msg": "Имя 3-30 символов"}
    if not tag or len(tag) < 2 or len(tag) > 5: return {"ok": False, "msg": "Тег 2-5 символов"}
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if not me: return {"ok": False}
    if (me.coins or 0) < CLUB_CREATE_PRICE_COINS:
        return {"ok": False, "msg": f"Нужно {CLUB_CREATE_PRICE_COINS} 💰"}
    # Проверка членства
    if db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first():
        return {"ok": False, "msg": "Ты уже в клубе"}
    # Уникальность
    if db.query(ReflexClub).filter(ReflexClub.name == name).first():
        return {"ok": False, "msg": "Имя занято"}
    if db.query(ReflexClub).filter(ReflexClub.tag == tag).first():
        return {"ok": False, "msg": "Тег занят"}
    me.coins -= CLUB_CREATE_PRICE_COINS
    club = ReflexClub(name=name, tag=tag, owner_id=pid, description=desc, icon=icon, member_count=1)
    db.add(club); db.flush()
    db.add(ReflexClubMember(club_id=club.id, player_id=pid, role="owner"))
    db.commit()
    return {"ok": True, "club": {"id": club.id, "name": name, "tag": tag, "icon": icon}, "new_coins": me.coins}


@router.get("/clubs/list")
def clubs_list(limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_db)):
    rows = db.query(ReflexClub).order_by(desc(ReflexClub.rating)).limit(limit).all()
    return {
        "clubs": [
            {"id": c.id, "name": c.name, "tag": c.tag, "icon": c.icon,
             "member_count": c.member_count or 0, "rating": round(c.rating or 0, 1),
             "total_wins": c.total_wins or 0, "total_matches": c.total_matches or 0}
            for c in rows
        ],
    }


@router.get("/clubs/my")
def clubs_my(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem:
        return {"authenticated": True, "in_club": False}
    club = db.query(ReflexClub).filter(ReflexClub.id == mem.club_id).first()
    if not club:
        return {"authenticated": True, "in_club": False}
    members = db.query(ReflexClubMember, Player).join(Player, Player.id == ReflexClubMember.player_id).filter(
        ReflexClubMember.club_id == club.id
    ).order_by(desc(ReflexClubMember.contribution)).limit(30).all()
    return {
        "authenticated": True,
        "in_club": True,
        "club": {
            "id": club.id, "name": club.name, "tag": club.tag, "icon": club.icon,
            "description": club.description or "",
            "member_count": club.member_count or 0, "rating": round(club.rating or 0, 1),
            "total_wins": club.total_wins or 0, "total_matches": club.total_matches or 0,
            "owner_id": club.owner_id,
        },
        "my_role": mem.role,
        "my_contribution": mem.contribution or 0,
        "members": [
            {"player_id": p.id, "nickname": p.nickname, "role": m.role,
             "contribution": m.contribution or 0, "elo": round(p.reflex_elo or 1000, 0)}
            for m, p in members
        ],
    }


@router.post("/clubs/join")
def clubs_join(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    club_id = (data or {}).get("club_id")
    if not club_id: return {"ok": False}
    if db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first():
        return {"ok": False, "msg": "Ты уже в клубе"}
    club = db.query(ReflexClub).filter(ReflexClub.id == club_id).with_for_update().first()
    if not club: return {"ok": False, "msg": "Клуб не найден"}
    if (club.member_count or 0) >= CLUB_MAX_MEMBERS:
        return {"ok": False, "msg": "Клуб полон"}
    db.add(ReflexClubMember(club_id=club.id, player_id=pid, role="member"))
    club.member_count = (club.member_count or 0) + 1
    db.commit()
    return {"ok": True, "club_id": club.id}


@router.post("/clubs/leave")
def clubs_leave(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem: return {"ok": False, "msg": "Ты не в клубе"}
    club = db.query(ReflexClub).filter(ReflexClub.id == mem.club_id).with_for_update().first()
    if club and club.owner_id == pid:
        # Если владелец уходит — передаём старшему члену либо удаляем
        other = db.query(ReflexClubMember).filter(
            ReflexClubMember.club_id == club.id,
            ReflexClubMember.player_id != pid,
        ).order_by(desc(ReflexClubMember.contribution)).first()
        if other:
            club.owner_id = other.player_id
            other.role = "owner"
        else:
            db.delete(club)
    if club:
        club.member_count = max(0, (club.member_count or 0) - 1)
    db.delete(mem)
    db.commit()
    return {"ok": True}


def _bump_club_stats(db: Session, player_id: int, is_win: bool):
    """Обновляет статы клуба игрока при завершении матча."""
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == player_id).first()
    if not mem: return
    club = db.query(ReflexClub).filter(ReflexClub.id == mem.club_id).with_for_update().first()
    if not club: return
    club.total_matches = (club.total_matches or 0) + 1
    if is_win:
        club.total_wins = (club.total_wins or 0) + 1
        mem.contribution = (mem.contribution or 0) + 3
    else:
        mem.contribution = (mem.contribution or 0) + 1
    # Рейтинг: winrate * matches (нормализация)
    if (club.total_matches or 0) > 0:
        club.rating = round(((club.total_wins or 0) / club.total_matches) * 100 + (club.total_matches or 0) * 0.5, 2)


# ═══════════════════════════════════════════════════════════════
#   ТУРНИРЫ
# ═══════════════════════════════════════════════════════════════

TOURNAMENT_ENTRY_COINS = 100
TOURNAMENT_PRIZES = {1: {"coins": 2000, "gems": 50}, 2: {"coins": 1000, "gems": 25}, 3: {"coins": 500, "gems": 10}}


def _current_tournament_key() -> str:
    from datetime import datetime as _dt, timezone as _tz
    y, w, _ = _dt.now(_tz.utc).isocalendar()
    return f"{y}-W{w:02d}"


def _ensure_current_tournament(db: Session) -> ReflexTournament:
    key = _current_tournament_key()
    t = db.query(ReflexTournament).filter(ReflexTournament.week_key == key).first()
    if not t:
        t = ReflexTournament(week_key=key, status="open")
        db.add(t); db.commit(); db.refresh(t)
    return t


@router.get("/tournament/current")
def tournament_current(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    t = _ensure_current_tournament(db)
    signups_count = db.query(ReflexTournamentSignup).filter(ReflexTournamentSignup.tournament_id == t.id).count()
    my_signup = None
    if authorization and authorization.startswith("Bearer "):
        p = verify_token(authorization[7:])
        if p:
            pid = p.get("player_id")
            ms = db.query(ReflexTournamentSignup).filter(
                ReflexTournamentSignup.tournament_id == t.id,
                ReflexTournamentSignup.player_id == pid,
            ).first()
            if ms:
                my_signup = {"seed": ms.seed, "eliminated_at_round": ms.eliminated_at_round, "final_rank": ms.final_rank}
    return {
        "id": t.id,
        "week_key": t.week_key,
        "status": t.status,
        "signups": signups_count,
        "entry_coins": TOURNAMENT_ENTRY_COINS,
        "prizes": TOURNAMENT_PRIZES,
        "my_signup": my_signup,
        "bracket": t.bracket,
        "winner_id": t.winner_id,
    }


@router.post("/tournament/signup")
def tournament_signup(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    t = _ensure_current_tournament(db)
    if t.status != "open":
        return {"ok": False, "msg": "Регистрация закрыта"}
    existing = db.query(ReflexTournamentSignup).filter(
        ReflexTournamentSignup.tournament_id == t.id,
        ReflexTournamentSignup.player_id == pid,
    ).first()
    if existing:
        return {"ok": False, "msg": "Уже зарегистрирован"}
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if not me: return {"ok": False}
    if (me.coins or 0) < TOURNAMENT_ENTRY_COINS:
        return {"ok": False, "msg": f"Нужно {TOURNAMENT_ENTRY_COINS} 💰"}
    me.coins -= TOURNAMENT_ENTRY_COINS
    db.add(ReflexTournamentSignup(tournament_id=t.id, player_id=pid))
    db.commit()
    return {"ok": True, "new_coins": me.coins}


@router.get("/tournament/leaderboard")
def tournament_leaderboard(db: Session = Depends(get_db)):
    t = _ensure_current_tournament(db)
    rows = db.query(ReflexTournamentSignup, Player).join(Player, Player.id == ReflexTournamentSignup.player_id).filter(
        ReflexTournamentSignup.tournament_id == t.id,
    ).order_by(desc(Player.reflex_elo)).limit(50).all()
    return {
        "tournament_id": t.id,
        "week_key": t.week_key,
        "entries": [
            {"rank": i + 1, "nickname": p.nickname, "elo": round(p.reflex_elo or 1000, 0),
             "player_id": p.id, "final_rank": s.final_rank}
            for i, (s, p) in enumerate(rows)
        ],
    }


# ═══════════════════════════════════════════════════════════════
#   RANKED TIER endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/ranked/me")
def ranked_me(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    me = db.query(Player).filter(Player.id == pid).first()
    if not me: return {"authenticated": False}
    total = (me.reflex_wins or 0) + (me.reflex_losses or 0)
    placement_done = total >= PLACEMENT_MATCHES
    tier = compute_tier(me.reflex_elo or 1000)
    # Категорийные тиры
    cat_tiers = {
        "reaction": compute_tier(me.elo_reaction or 1000),
        "logic": compute_tier(me.elo_logic or 1000),
        "memory": compute_tier(me.elo_memory or 1000),
        "coordination": compute_tier(me.elo_coordination or 1000),
        "trivia": compute_tier(me.elo_trivia or 1000),
    }
    return {
        "authenticated": True,
        "elo": round(me.reflex_elo or 1000, 1),
        "tier": tier,
        "categorical_tiers": cat_tiers,
        "placement_done": placement_done,
        "placement_remaining": max(0, PLACEMENT_MATCHES - total),
        "total_matches": total,
    }


@router.get("/ranked/tiers")
def ranked_tiers_list():
    """Справочник тиров — для frontend."""
    return {
        "tiers": [
            {"name": t[0], "name_ru": t[1], "icon": t[2], "color": t[3], "min_elo": t[4]}
            for t in TIERS
        ],
    }


# ═══════════════════════════════════════════════════════════════
#   SEASONAL EVENT endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/seasonal_event")
def seasonal_event_info():
    ev = current_seasonal_event()
    return {"event": ev}


# ═══════════════════════════════════════════════════════════════
#   ANTI-CHEAT: report, suspicious-pattern tracking
# ═══════════════════════════════════════════════════════════════

NICKNAME_BLACKLIST = {
    "admin", "moderator", "root", "support", "anthropic", "claude",
    "system", "bot", "null", "undefined", "official",
}


def nickname_is_safe(nick: str) -> bool:
    if not nick: return False
    n = nick.strip().lower()
    if len(n) < 2 or len(n) > 24: return False
    if n in NICKNAME_BLACKLIST: return False
    # Простая фильтрация мата (минимум)
    bad_stems = {"хуй", "бляд", "пизд", "ебан", "fuck", "shit", "nazi", "hitler"}
    if any(b in n for b in bad_stems):
        return False
    return True


@router.post("/report_player")
def report_player(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    target_nick = (data or {}).get("nickname", "").strip()
    match_id = (data or {}).get("match_id")
    reason = (data or {}).get("reason", "")[:200]
    if not target_nick: return {"ok": False, "msg": "Нет ника"}
    target = db.query(Player).filter(Player.nickname == target_nick).first()
    if not target: return {"ok": False, "msg": "Игрок не найден"}
    try:
        db.add(ReflexEvent(
            player_id=pid,
            event_type="report",
            payload={"reported_player_id": target.id, "reported_nick": target_nick,
                     "match_id": match_id, "reason": reason},
        ))
        db.commit()
    except Exception: pass
    return {"ok": True}


@router.get("/flags/sus_count/{player_id}")
def sus_count(player_id: int, db: Session = Depends(get_db)):
    """Сколько раз жаловались на игрока."""
    cnt = db.query(ReflexEvent).filter(
        ReflexEvent.event_type == "report",
    ).count()
    return {"reports_total": cnt}


# ═══════════════════════════════════════════════════════════════
#   КЛУБНЫЕ ВОЙНЫ
# ═══════════════════════════════════════════════════════════════

@router.post("/clubs/challenge")
def clubs_challenge(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Клуб-капитан вызывает другой клуб на войну. Простая MVP-реализация:
    создаётся запись о войне, оба клуба должны накидать победы в PvP в течение 24ч,
    по истечении — побеждает тот клуб у кого больше побед за этот период."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem or mem.role not in ("owner", "officer"):
        return {"ok": False, "msg": "Только владелец может объявлять войну"}
    target_tag = (data or {}).get("tag", "").strip().upper()
    if not target_tag: return {"ok": False}
    target_club = db.query(ReflexClub).filter(ReflexClub.tag == target_tag).first()
    if not target_club or target_club.id == mem.club_id:
        return {"ok": False, "msg": "Клуб не найден"}
    # Упрощение: запись через ReflexEvent
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    ends_at = (_dt.now(_tz.utc) + _td(hours=24)).isoformat()
    db.add(ReflexEvent(
        player_id=pid, event_type="club_war_challenge",
        payload={"attacker_club_id": mem.club_id, "defender_club_id": target_club.id,
                 "ends_at": ends_at, "status": "active"},
    ))
    db.commit()
    return {"ok": True, "ends_at": ends_at, "defender": {"tag": target_club.tag, "name": target_club.name}}


@router.get("/clubs/wars")
def clubs_wars(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Активные войны клуба игрока."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem: return {"authenticated": True, "in_club": False}
    rows = db.query(ReflexEvent).filter(
        ReflexEvent.event_type == "club_war_challenge",
    ).order_by(desc(ReflexEvent.created_at)).limit(20).all()
    wars = []
    for r in rows:
        p = r.payload or {}
        if p.get("attacker_club_id") == mem.club_id or p.get("defender_club_id") == mem.club_id:
            wars.append({
                "id": r.id,
                "attacker_club_id": p.get("attacker_club_id"),
                "defender_club_id": p.get("defender_club_id"),
                "ends_at": p.get("ends_at"),
                "status": p.get("status"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
    return {"authenticated": True, "in_club": True, "wars": wars}


# Простой club chat через ReflexEvent (message_type='club_chat')
@router.post("/clubs/chat/send")
def clubs_chat_send(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem: return {"ok": False, "msg": "Ты не в клубе"}
    text = (data or {}).get("text", "").strip()[:500]
    if not text: return {"ok": False}
    me = db.query(Player).filter(Player.id == pid).first()
    db.add(ReflexEvent(
        player_id=pid, event_type="club_chat",
        payload={"club_id": mem.club_id, "nickname": me.nickname if me else "?", "text": text},
    ))
    db.commit()
    return {"ok": True}


@router.get("/clubs/chat")
def clubs_chat_get(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    mem = db.query(ReflexClubMember).filter(ReflexClubMember.player_id == pid).first()
    if not mem: return {"authenticated": True, "in_club": False}
    rows = db.query(ReflexEvent).filter(
        ReflexEvent.event_type == "club_chat",
    ).order_by(desc(ReflexEvent.created_at)).limit(50).all()
    msgs = []
    for r in rows:
        p = r.payload or {}
        if p.get("club_id") == mem.club_id:
            msgs.append({
                "nickname": p.get("nickname"),
                "text": p.get("text"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
    msgs.reverse()
    return {"authenticated": True, "in_club": True, "messages": msgs}


# ═══════════════════════════════════════════════════════════════
#   АДМИН-ПАНЕЛЬ: dashboard + player search
# ═══════════════════════════════════════════════════════════════

ADMIN_TOKEN = _os.environ.get("ADMIN_TOKEN", "")


def _is_admin(authorization: Optional[str]) -> bool:
    if not authorization: return False
    if not authorization.startswith("Bearer "): return False
    tok = authorization[7:]
    if not ADMIN_TOKEN: return False
    return tok == ADMIN_TOKEN


@router.get("/admin/dashboard")
def admin_dashboard(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not _is_admin(authorization):
        return {"ok": False, "msg": "forbidden"}
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = _dt.now(_tz.utc)
    d1 = now - _td(days=1)
    d7 = now - _td(days=7)
    d30 = now - _td(days=30)

    total_players = db.query(Player).count()
    guests = db.query(Player).filter(Player.is_guest == True).count()
    total_matches = db.query(ReflexMatch).filter(ReflexMatch.status == "finished").count()

    # DAU/WAU/MAU по событиям
    dau = db.query(ReflexEvent.player_id).filter(
        ReflexEvent.created_at >= d1, ReflexEvent.player_id.isnot(None)
    ).distinct().count()
    wau = db.query(ReflexEvent.player_id).filter(
        ReflexEvent.created_at >= d7, ReflexEvent.player_id.isnot(None)
    ).distinct().count()
    mau = db.query(ReflexEvent.player_id).filter(
        ReflexEvent.created_at >= d30, ReflexEvent.player_id.isnot(None)
    ).distinct().count()

    # Revenue (платежи completed)
    rev = db.query(ReflexPayment).filter(ReflexPayment.status == "completed").all()
    rev_stars = sum((p.amount_minor or 0) for p in rev if (p.currency or "XTR") == "XTR")
    rev_payments = len(rev)

    # Топ event-types за неделю
    from sqlalchemy import func as _fn
    top_ev = db.query(ReflexEvent.event_type, _fn.count(ReflexEvent.id)).filter(
        ReflexEvent.created_at >= d7,
    ).group_by(ReflexEvent.event_type).order_by(desc(_fn.count(ReflexEvent.id))).limit(15).all()

    return {
        "ok": True,
        "total_players": total_players,
        "guests": guests,
        "registered": total_players - guests,
        "total_matches": total_matches,
        "dau": dau, "wau": wau, "mau": mau,
        "revenue_stars_completed": rev_stars,
        "revenue_payments_count": rev_payments,
        "top_events_7d": [{"event": e, "count": c} for e, c in top_ev],
    }


@router.get("/admin/player/{query}")
def admin_player_find(query: str, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not _is_admin(authorization):
        return {"ok": False, "msg": "forbidden"}
    pl = None
    if query.isdigit():
        pl = db.query(Player).filter(Player.id == int(query)).first()
    if not pl:
        pl = db.query(Player).filter(Player.nickname.ilike(f"%{query}%")).first()
    if not pl: return {"ok": False, "msg": "not found"}
    reports = db.query(ReflexEvent).filter(
        ReflexEvent.event_type == "report",
    ).all()
    my_reports = [r for r in reports if (r.payload or {}).get("reported_player_id") == pl.id]
    return {
        "ok": True,
        "player": {
            "id": pl.id, "nickname": pl.nickname,
            "coins": pl.coins or 0, "gems": pl.gems or 0, "xp": pl.xp or 0,
            "elo": round(pl.reflex_elo or 1000, 1),
            "wins": pl.reflex_wins or 0, "losses": pl.reflex_losses or 0,
            "is_guest": bool(pl.is_guest),
            "created_at": pl.created_at.isoformat() if pl.created_at else None,
        },
        "tier": compute_tier(pl.reflex_elo or 1000),
        "reports_against": len(my_reports),
    }


@router.post("/admin/broadcast")
def admin_broadcast(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not _is_admin(authorization):
        return {"ok": False, "msg": "forbidden"}
    text = (data or {}).get("text", "")
    if not text: return {"ok": False}
    # Логируем — WS push / email уйдёт позже через background job
    db.add(ReflexEvent(player_id=None, event_type="admin_broadcast",
                       payload={"text": text, "queued_at": datetime.utcnow().isoformat()}))
    db.commit()
    return {"ok": True, "msg": "queued"}


# ═══════════════════════════════════════════════════════════════
#   MATCH REPLAY
# ═══════════════════════════════════════════════════════════════

@router.get("/replay/{match_id}")
def replay_match(match_id: int, db: Session = Depends(get_db)):
    m = db.query(ReflexMatch).filter(ReflexMatch.id == match_id).first()
    if not m or m.status != "finished":
        return {"ok": False, "msg": "не найден или не завершён"}
    p1 = db.query(Player).filter(Player.id == m.p1_id).first()
    p2 = db.query(Player).filter(Player.id == m.p2_id).first()
    return {
        "ok": True,
        "match": {
            "id": m.id,
            "p1": {"id": m.p1_id, "nickname": p1.nickname if p1 else "?"},
            "p2": {"id": m.p2_id, "nickname": p2.nickname if p2 else "?"},
            "rounds_p1": m.rounds_p1 or 0,
            "rounds_p2": m.rounds_p2 or 0,
            "winner_id": m.winner_id,
            "rounds_log": m.rounds_log or [],
            "elo_change_p1": m.elo_change_p1 or 0,
            "elo_change_p2": m.elo_change_p2 or 0,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
        },
    }


# ═══════════════════════════════════════════════════════════════
#   ADS: watch-ad for reward (stub, до интеграции AdSense)
# ═══════════════════════════════════════════════════════════════

AD_DAILY_LIMIT = 5
AD_REWARD_COINS = 30


@router.get("/ads/status")
def ads_status(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"authenticated": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"authenticated": False}
    pid = payload.get("player_id")
    today = _today_str()
    viewed = db.query(ReflexEvent).filter(
        ReflexEvent.player_id == pid,
        ReflexEvent.event_type == "ad_reward_granted",
    ).all()
    today_count = sum(1 for e in viewed if e.created_at and e.created_at.strftime("%Y-%m-%d") == today)
    return {
        "authenticated": True,
        "views_today": today_count,
        "remaining": max(0, AD_DAILY_LIMIT - today_count),
        "daily_limit": AD_DAILY_LIMIT,
        "reward_coins": AD_REWARD_COINS,
    }


@router.post("/ads/reward")
def ads_reward(data: dict, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Вызывается после успешного просмотра рекламы.
    Клиент должен подтвердить ad_token (когда будет SDK) — пока stub."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"ok": False}
    payload = verify_token(authorization[7:])
    if not payload: return {"ok": False}
    pid = payload.get("player_id")
    today = _today_str()
    from datetime import datetime as _dt, timezone as _tz
    # Считаем сколько сегодня
    viewed = db.query(ReflexEvent).filter(
        ReflexEvent.player_id == pid,
        ReflexEvent.event_type == "ad_reward_granted",
    ).all()
    today_count = sum(1 for e in viewed if e.created_at and e.created_at.strftime("%Y-%m-%d") == today)
    if today_count >= AD_DAILY_LIMIT:
        return {"ok": False, "msg": "Дневной лимит исчерпан"}
    me = db.query(Player).filter(Player.id == pid).with_for_update().first()
    if not me: return {"ok": False}
    me.coins = (me.coins or 0) + AD_REWARD_COINS
    db.add(ReflexEvent(player_id=pid, event_type="ad_reward_granted",
                       payload={"coins": AD_REWARD_COINS, "views_today": today_count + 1}))
    db.commit()
    return {"ok": True, "coins_awarded": AD_REWARD_COINS, "new_coins": me.coins,
            "remaining": max(0, AD_DAILY_LIMIT - today_count - 1)}


# ═══════════════════════════════════════════════════════════════
#   TELEGRAM MINI APP: auto-auth через initData
# ═══════════════════════════════════════════════════════════════

@router.post("/auth/telegram")
def auth_telegram(data: dict, db: Session = Depends(get_db)):
    """Авторизация через Telegram Mini App initData.
    initData — строка от Telegram.WebApp.initData, содержит подпись.
    Упрощённая проверка: hash-подпись HMAC-SHA256 с secret = HMAC-SHA256(bot_token, "WebAppData").
    """
    init_data = (data or {}).get("init_data", "")
    if not init_data:
        return {"ok": False, "msg": "Нет initData"}
    if not TG_BOT_TOKEN:
        # Dev-режим: создаём гостя с ником из tg_user
        tg_user = (data or {}).get("tg_user", {}) or {}
        tg_id = tg_user.get("id")
        tg_nick = tg_user.get("username") or tg_user.get("first_name") or f"TG_{tg_id or _rnd.randint(1000, 999999)}"
        if not tg_id:
            return {"ok": False, "msg": "Dev: нужно tg_user.id"}
        return _upsert_tg_player(db, tg_id, tg_nick, first_name=tg_user.get("first_name", ""))
    # Валидация подписи
    try:
        import urllib.parse, hmac, hashlib, json as _json
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_val = parsed.pop("hash", "")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", TG_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if calc != hash_val:
            return {"ok": False, "msg": "Неверная подпись"}
        tg_user = _json.loads(parsed.get("user", "{}"))
        tg_id = tg_user.get("id")
        tg_nick = tg_user.get("username") or tg_user.get("first_name") or f"TG_{tg_id}"
        if not tg_id:
            return {"ok": False, "msg": "Нет user.id"}
        return _upsert_tg_player(db, tg_id, tg_nick, first_name=tg_user.get("first_name", ""))
    except Exception as e:
        return {"ok": False, "msg": f"error: {str(e)[:200]}"}


def _upsert_tg_player(db: Session, tg_id: int, tg_nick: str, first_name: str = ""):
    from core.auth import create_access_token
    # Ищем по ач-коду tg_id
    existing = db.query(ReflexAchievement).filter(
        ReflexAchievement.code == f"tg_id_{tg_id}",
    ).first()
    pl = None
    if existing:
        pl = db.query(Player).filter(Player.id == existing.player_id).first()
    if not pl:
        # Генерируем уникальный ник
        base_nick = tg_nick[:24]
        if not nickname_is_safe(base_nick):
            base_nick = f"TG_{tg_id}"
        nick = base_nick
        i = 0
        while db.query(Player).filter(Player.nickname == nick).first():
            i += 1
            nick = f"{base_nick}_{i}"
            if i > 99:
                nick = f"TG_{tg_id}_{_rnd.randint(1000,9999)}"
                break
        pl = Player(nickname=nick, coins=50, xp=0, is_guest=False)
        db.add(pl); db.flush()
        db.add(ReflexAchievement(player_id=pl.id, code=f"tg_id_{tg_id}"))
        db.commit()
    token = create_access_token({"player_id": pl.id, "nickname": pl.nickname})
    return {
        "ok": True,
        "token": token,
        "player_id": pl.id,
        "nickname": pl.nickname,
    }
