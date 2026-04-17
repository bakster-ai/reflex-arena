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
)
from datetime import timedelta

router = APIRouter(prefix="/api", tags=["reflex"])
share_router = APIRouter(tags=["reflex-share"])

REFERRAL_BONUS = 100  # coins, обоим


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
        "elo": round(player.reflex_elo or 1000.0, 1),
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
