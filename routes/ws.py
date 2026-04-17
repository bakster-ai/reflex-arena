"""
Reflex Arena — браузерная 1v1-арена на мини-играх.

Matchmaking flow (без параллельных receive на одном ws):
  1. Клиент шлёт {type:queue, stake_coins}.
  2. Под lock'ом смотрим очередь: если есть подходящий → pop'аем его,
     создаём ReflexRoom, сигналим event'ом его waiter'а, сами сразу идём в serve_role(p2).
  3. Если не нашли — встаём в очередь с asyncio.Event, ждём либо event (матч),
     либо cancel/disconnect (через recv_task), либо таймаут.
  4. Когда event срабатывает — знаем свой room/role, идём в serve_role(p1).
  5. ReflexRoom.serve_role первый раз запускает room.start() (matched-broadcast + 1-й раунд).

Anti-cheat (minimal):
  - Серверный round_start_ts → score=0 если пришёл результат раньше min_elapsed_ms.
  - Score ограничен max_score для каждой мини-игры.
"""
import asyncio
import json as _json
import random
import string
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.auth import verify_token
from datetime import datetime, timezone
from models.models import Player, ReflexMatch, ReflexAchievement, ReflexDailyTask


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _tick_daily_tasks(db, player_id: int, events: dict):
    """Обновляет прогресс дневных задач по событиям.
    events — dict: {"win": bool, "stake_win": bool, "vs_ai_win": bool,
                    "play": bool, "perfect": bool, "elo_delta": float, "max_round_score": int,
                    "unique_games_played": int}
    """
    # Импорт каталога лениво чтобы избежать циклов
    from routes.api import DAILY_TASK_CATALOG
    today = _today_str()
    # Создаём задачи если их нет
    existing_count = db.query(ReflexDailyTask).filter(
        ReflexDailyTask.player_id == player_id,
        ReflexDailyTask.date == today,
    ).count()
    if existing_count < 3:
        need = 3 - existing_count
        used_codes = {r.code for r in db.query(ReflexDailyTask).filter(
            ReflexDailyTask.player_id == player_id, ReflexDailyTask.date == today,
        ).all()}
        avail = [t for t in DAILY_TASK_CATALOG if t["code"] not in used_codes]
        random.shuffle(avail)
        for t in avail[:need]:
            db.add(ReflexDailyTask(
                player_id=player_id, date=today,
                code=t["code"], progress=0, target=t["target"],
                reward_coins=t["reward"], claimed=False,
            ))
        db.flush()
    rows = db.query(ReflexDailyTask).filter(
        ReflexDailyTask.player_id == player_id,
        ReflexDailyTask.date == today,
    ).all()
    for r in rows:
        if r.progress >= r.target:
            continue
        inc = 0
        if r.code == "win_3" and events.get("win"):
            inc = 1
        elif r.code == "play_5" and events.get("play"):
            inc = 1
        elif r.code == "stake_win" and events.get("stake_win"):
            inc = 1
        elif r.code == "ai_3" and events.get("vs_ai_win"):
            inc = 1
        elif r.code == "perfect" and events.get("perfect"):
            inc = 1
        elif r.code == "games_4":
            # Прогресс = max(progress, unique_games_played за сегодня)
            ug = events.get("unique_games_played", 0)
            if ug > r.progress:
                r.progress = min(r.target, ug)
            continue
        elif r.code == "elo_up":
            d = int(events.get("elo_delta", 0))
            if d > 0:
                inc = d
        elif r.code == "score_3k":
            mr = events.get("max_round_score", 0)
            if mr >= 3000:
                inc = 3000  # сразу засчитываем весь target
        if inc > 0:
            r.progress = min(r.target, r.progress + inc)


# ─── Achievements catalog ───
ACHIEVEMENTS = {
    "first_win":    {"name": "Первая кровь",    "desc": "Выиграть первый матч", "reward_coins": 50},
    "win_3":        {"name": "Серия",           "desc": "Выиграть 3 матча подряд", "reward_coins": 100},
    "win_10":       {"name": "Ветеран",         "desc": "Выиграть 10 матчей", "reward_coins": 150},
    "perfect":      {"name": "Тотальное превосходство", "desc": "Выиграть матч 3:0", "reward_coins": 75},
    "stake_winner": {"name": "Жадный",          "desc": "Выиграть матч со ставкой", "reward_coins": 50},
    "comeback":     {"name": "Камбек",          "desc": "Выиграть проигрывая 0:2", "reward_coins": 100},
    "diverse":      {"name": "Мастер на все руки", "desc": "Поиграть во все мини-игры", "reward_coins": 200},
}


def _grant_achievement(db, player_id: int, code: str) -> bool:
    """Пытается разблокировать достижение. Возвращает True если действительно новое."""
    if code not in ACHIEVEMENTS:
        return False
    exists = db.query(ReflexAchievement).filter(
        ReflexAchievement.player_id == player_id,
        ReflexAchievement.code == code,
    ).first()
    if exists:
        return False
    db.add(ReflexAchievement(player_id=player_id, code=code))
    # Выдаём награду
    p = db.query(Player).filter(Player.id == player_id).with_for_update().first()
    if p:
        p.coins = (p.coins or 0) + ACHIEVEMENTS[code]["reward_coins"]
    return True

router = APIRouter()


# ─── Constants ───
BEST_OF = 5
BEST_OF_DEATHMATCH = 15
K_FACTOR_DEATHMATCH = 48  # усиленный ELO в DM
DEATHMATCH_MIN_STAKE = 200
MAX_STAKE = 1000
MIN_STAKE = 0
MAX_QUEUE_WAIT = 60
RECONNECT_GRACE_SEC = 30.0       # даём 30 секунд на реконнект (мобилки после unbackground долго восстанавливают WS)
AI_OFFER_AFTER_SEC = 20          # если нет соперника столько секунд — предлагаем AI

# Счёт внутри мини-игр = человеческие единицы (правильных ответов / попаданий / длина / пары).
# За ошибки может быть штраф (-1 балл), поэтому score может быть отрицательным.
# max_score ограничивает сверху — защита от cheat'еров. min_score = -max_score.
GAMES = {
    "react": {
        "name": "Реакция",
        "desc": "Кликни максимально быстро, когда экран станет зелёным (5 попыток). Фальстарт -1.",
        "max_score": 20,   # 5 попыток × до 2 очков + margin
        "min_elapsed_ms": 2000,
        "max_elapsed_ms": 60000,
    },
    "aim": {
        "name": "Снайпер",
        "desc": "За 15 секунд попади в максимум мишеней.",
        "max_score": 100,
        "min_elapsed_ms": 14500,
        "max_elapsed_ms": 16500,
    },
    "sequence": {
        "name": "Последовательность",
        "desc": "Каждый раунд — новая последовательность, но на 1 длиннее. Повтори её.",
        "max_score": 30,
        "min_elapsed_ms": 1500,
        "max_elapsed_ms": 60000,
    },
    "math": {
        "name": "Устный счёт",
        "desc": "За 20 секунд реши максимум примеров. Правильный = +1, ошибка = −1.",
        "max_score": 50,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "odd": {
        "name": "Найди отличие",
        "desc": "В сетке один квадрат слегка другого цвета. Найди +1, ошибка −1. 20 сек.",
        "max_score": 50,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "typing": {
        "name": "Скорость печати",
        "desc": "Печатай слова как можно быстрее. Засчитывается за точность. 20 сек.",
        "max_score": 50,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "stroop": {
        "name": "Строп-тест",
        "desc": "Кликай по ЦВЕТУ слова (не по значению!). Правильный +1, ошибка −1.",
        "max_score": 50,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "memory": {
        "name": "Память",
        "desc": "Найди максимум пар за 30 секунд.",
        "max_score": 30,
        "min_elapsed_ms": 3000,
        "max_elapsed_ms": 32000,
    },
    "spatial": {
        "name": "Пространственная память",
        "desc": "Запомни подсвеченные клетки. Каждый уровень = +1 балл.",
        "max_score": 30,
        "min_elapsed_ms": 2000,
        "max_elapsed_ms": 60000,
    },
    "audio": {
        "name": "Слух",
        "desc": "Кликай когда услышишь сигнал (3 попытки). Фальстарт −1.",
        "max_score": 10,
        "min_elapsed_ms": 4000,
        "max_elapsed_ms": 30000,
    },
    "rhythm": {
        "name": "Ритм",
        "desc": "Тапай в такт метронома. Точно = +1, мимо = 0.",
        "max_score": 50,
        "min_elapsed_ms": 15000,
        "max_elapsed_ms": 22000,
    },
    "balance": {
        "name": "Баланс",
        "desc": "Удерживай шарик в центре кольца. Очко = секунда в центре.",
        "max_score": 30,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "quick_draw": {
        "name": "Быстрый штрих",
        "desc": "Соединяй точки по порядку 1→2→3. Каждая точка = +1 балл.",
        "max_score": 25,
        "min_elapsed_ms": 3000,
        "max_elapsed_ms": 30000,
    },
    "number_chain": {
        "name": "Числовая цепочка",
        "desc": "Найди закономерность: 2, 4, 8, ? — правильный +1, ошибка −1.",
        "max_score": 50,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "visual_memory": {
        "name": "Визуальная память",
        "desc": "Запомни эмодзи за 2 сек. Правильный +1, ошибка −1. 10 раундов.",
        "max_score": 12,
        "min_elapsed_ms": 10000,
        "max_elapsed_ms": 60000,
    },
    "word_memory": {
        "name": "Слова",
        "desc": "Было ли это слово в списке? Правильный +1, ошибка −1.",
        "max_score": 20,
        "min_elapsed_ms": 10000,
        "max_elapsed_ms": 60000,
    },
    "trivia_geo": {
        "name": "География",
        "desc": "Столицы, флаги, достопримечательности. +1/−1. 20 сек.",
        "max_score": 30,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "trivia_science": {
        "name": "Наука",
        "desc": "Физика, химия, биология, космос. +1/−1. 20 сек.",
        "max_score": 30,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "trivia_history": {
        "name": "История",
        "desc": "Даты, события, личности. +1/−1. 20 сек.",
        "max_score": 30,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
    "trivia_pop": {
        "name": "Поп-культура",
        "desc": "Кино, музыка, интернет, мемы. +1/−1. 20 сек.",
        "max_score": 30,
        "min_elapsed_ms": 19500,
        "max_elapsed_ms": 21000,
    },
}
GAME_POOL = list(GAMES.keys())
K_FACTOR = 32
BASE_ELO = 1000.0

# Категории игр → для категорийного ELO
GAME_CATEGORIES = {
    # 🔴 Реакция
    "react": "reaction", "audio": "reaction", "aim": "reaction", "odd": "reaction",
    # 🟡 Логика
    "math": "logic", "sequence": "logic", "stroop": "logic", "number_chain": "logic",
    # 🟢 Память
    "memory": "memory", "spatial": "memory", "visual_memory": "memory", "word_memory": "memory",
    # 🔵 Координация
    "balance": "coordination", "rhythm": "coordination", "quick_draw": "coordination", "typing": "coordination",
    # 🟣 Эрудиция
    "trivia_geo": "trivia", "trivia_science": "trivia", "trivia_history": "trivia", "trivia_pop": "trivia",
}

CATEGORY_ELO_FIELD = {
    "reaction": "elo_reaction",
    "logic": "elo_logic",
    "memory": "elo_memory",
    "coordination": "elo_coordination",
    "trivia": "elo_trivia",
}

# AI-противник: для каждой мини-игры и сложности — среднее score + вариация
# Фактическая формула: random(score - var, score + var)
AI_PROFILES = {
    # score в единицах (balls, правильных ответов, попаданий, уровней).
    # (среднее, разброс) для каждой мини-игры.
    "easy": {
        "react": (3, 2), "aim": (10, 4), "sequence": (3, 1),
        "math": (6, 3), "odd": (5, 2),
        "typing": (4, 2), "stroop": (5, 2),
        "memory": (3, 2), "spatial": (2, 1),
        "audio": (2, 1), "rhythm": (10, 3),
        "balance": (3, 2), "quick_draw": (5, 2),
        "number_chain": (5, 2), "visual_memory": (3, 2), "word_memory": (4, 2),
        "trivia_geo": (5, 2), "trivia_science": (4, 2),
        "trivia_history": (4, 2), "trivia_pop": (5, 2),
        "elo": 800, "nickname": "🤖 AI Новичок",
        "elapsed_factor": 0.9,
    },
    "medium": {
        "react": (6, 2), "aim": (22, 4), "sequence": (5, 1),
        "math": (12, 3), "odd": (12, 3),
        "typing": (9, 2), "stroop": (12, 3),
        "memory": (7, 2), "spatial": (5, 1),
        "audio": (4, 1), "rhythm": (22, 4),
        "balance": (8, 2), "quick_draw": (12, 3),
        "number_chain": (12, 3), "visual_memory": (6, 2), "word_memory": (10, 3),
        "trivia_geo": (12, 3), "trivia_science": (10, 3),
        "trivia_history": (10, 3), "trivia_pop": (12, 3),
        "elo": 1000, "nickname": "🤖 AI Средний",
        "elapsed_factor": 1.0,
    },
    "hard": {
        "react": (10, 2), "aim": (35, 5), "sequence": (8, 1),
        "math": (20, 3), "odd": (22, 3),
        "typing": (15, 3), "stroop": (20, 3),
        "memory": (12, 2), "spatial": (10, 2),
        "audio": (6, 1), "rhythm": (35, 4),
        "balance": (14, 2), "quick_draw": (18, 3),
        "number_chain": (22, 3), "visual_memory": (9, 1), "word_memory": (14, 2),
        "trivia_geo": (20, 3), "trivia_science": (18, 3),
        "trivia_history": (18, 3), "trivia_pop": (20, 3),
        "elo": 1300, "nickname": "🤖 AI Мастер",
        "elapsed_factor": 1.0,
    },
}


# ─── Queue entry ───
class _QueueEntry:
    __slots__ = ("ws", "player_id", "stake", "mode", "joined", "event", "room_holder")

    def __init__(self, ws, player_id, stake, mode="normal"):
        self.ws = ws
        self.player_id = player_id
        self.stake = stake
        self.mode = mode
        self.joined = time.time()
        self.event = asyncio.Event()
        # Когда нас матчат, сюда записывается {"room": ..., "role": ...}
        self.room_holder: dict = {}


_queue: list[_QueueEntry] = []
_queue_lock = asyncio.Lock()
_rooms: dict[str, "ReflexRoom"] = {}
# Индекс для реконнекта: player_id → активная комната
_active_match_by_player: dict[int, "ReflexRoom"] = {}
# Pending custom rooms: invite_code → {"creator_id", "ws", "event", "room_holder", "created_at"}
_custom_rooms: dict[str, dict] = {}
_custom_rooms_lock = asyncio.Lock()


def _gen_room() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        r = ''.join(random.choices(chars, k=10))
        if r not in _rooms:
            return r


def _player_info(player_id: int) -> Optional[dict]:
    db = SessionLocal()
    try:
        p = db.query(Player).filter(Player.id == player_id).first()
        if not p:
            return None
        return {
            "id": p.id,
            "nickname": p.nickname,
            "elo": round(p.reflex_elo or BASE_ELO, 1),
            "wins": p.reflex_wins or 0,
            "losses": p.reflex_losses or 0,
        }
    finally:
        db.close()


def _pick_game_pool(total: int = BEST_OF) -> list[str]:
    pool = GAME_POOL.copy()
    random.shuffle(pool)
    result = []
    while len(result) < total:
        if not pool:
            pool = GAME_POOL.copy()
            random.shuffle(pool)
        result.append(pool.pop())
    return result


async def _safe_send(ws: Optional[WebSocket], msg: dict) -> bool:
    if ws is None:
        return False
    try:
        await ws.send_text(_json.dumps(msg))
        return True
    except Exception:
        return False


def _ai_info(difficulty: str) -> dict:
    prof = AI_PROFILES.get(difficulty, AI_PROFILES["medium"])
    return {
        "id": None, "is_ai": True,
        "nickname": prof["nickname"], "elo": prof["elo"],
        "wins": 0, "losses": 0,
    }


def _ai_score_for(game_id: str, difficulty: str) -> int:
    """Возвращает случайный score в диапазоне профиля AI."""
    prof = AI_PROFILES.get(difficulty, AI_PROFILES["medium"])
    base, var = prof.get(game_id, (1000, 300))
    val = int(base + (random.random() * 2 - 1) * var)
    return max(0, min(val, GAMES.get(game_id, {}).get("max_score", 10000)))


def _ai_elapsed_for(game_id: str, difficulty: str) -> int:
    """Возвращает симулированный elapsed_ms для AI."""
    cfg = GAMES.get(game_id, {})
    prof = AI_PROFILES.get(difficulty, AI_PROFILES["medium"])
    min_e = cfg.get("min_elapsed_ms", 3000)
    max_e = cfg.get("max_elapsed_ms", 60000)
    factor = prof.get("elapsed_factor", 1.0)
    # Игры с фиксированной длительностью: играем всю её
    if game_id in ("aim", "math", "odd", "typing", "stroop", "memory", "rhythm", "balance"):
        return int(min(max_e, max(min_e, max_e * 0.97 * factor)))
    # react/sequence/spatial/audio/quick_draw: может быть короче
    return int(min_e + random.random() * 500)


class ReflexRoom:
    def __init__(self, room_id: str, p1_id: int, p2_id: int, stake: int,
                 ws1: WebSocket, ws2: Optional[WebSocket],
                 ai_role: Optional[str] = None, ai_difficulty: str = "medium",
                 mode: str = "normal"):
        self.room_id = room_id
        self.stake = stake
        self.mode = mode  # normal | deathmatch
        self.best_of = BEST_OF_DEATHMATCH if mode == "deathmatch" else BEST_OF
        self.k_factor = K_FACTOR_DEATHMATCH if mode == "deathmatch" else K_FACTOR
        self.player_ids = {"p1": p1_id, "p2": p2_id}
        self.ws = {"p1": ws1, "p2": ws2}
        self.connected = {"p1": True, "p2": True}
        # AI-режим: p1 или p2 может быть AI (player_id=0, ws=None)
        self.ai_role = ai_role  # 'p1' | 'p2' | None
        self.ai_difficulty = ai_difficulty if ai_role else None
        if ai_role:
            self.info = {
                "p1": _player_info(p1_id) if ai_role != "p1" else _ai_info(ai_difficulty),
                "p2": _player_info(p2_id) if ai_role != "p2" else _ai_info(ai_difficulty),
            }
        else:
            self.info = {
                "p1": _player_info(p1_id),
                "p2": _player_info(p2_id),
            }
        self.games = _pick_game_pool(self.best_of)
        self.round_num = 0
        self.round_start_ts: float = 0.0
        self.round_results: dict = {"p1": None, "p2": None}
        self.rounds_won = {"p1": 0, "p2": 0}
        self.rounds_log: list = []
        self.match_db_id: Optional[int] = None
        self.finished = False
        self._start_lock = asyncio.Lock()
        self._started = False
        self._round_lock = asyncio.Lock()
        # Для реконнекта: event выставляется когда игрок вернулся
        self._reconnect_events = {"p1": asyncio.Event(), "p2": asyncio.Event()}
        # Регистрируемся в индексе активных матчей (только реальных игроков)
        if ai_role != "p1" and p1_id:
            _active_match_by_player[p1_id] = self
        if ai_role != "p2" and p2_id:
            _active_match_by_player[p2_id] = self

    async def ensure_started(self):
        async with self._start_lock:
            if self._started or self.finished:
                return
            self._started = True
            asyncio.create_task(self._run_match_flow())

    async def _run_match_flow(self):
        """Создаёт DB запись, шлёт matched, запускает раунды."""
        # В AI-режиме не создаём DB запись (нет оппонента-игрока)
        if not self.ai_role:
            db = SessionLocal()
            try:
                m = ReflexMatch(
                    p1_id=self.player_ids["p1"],
                    p2_id=self.player_ids["p2"],
                    stake_coins=self.stake,
                    status="active",
                )
                db.add(m)
                if self.stake > 0:
                    for role in ("p1", "p2"):
                        p = db.query(Player).filter(Player.id == self.player_ids[role]).with_for_update().first()
                        if p:
                            p.coins = max(0, (p.coins or 0) - self.stake)
                db.commit()
                db.refresh(m)
                self.match_db_id = m.id
            except Exception as e:
                print(f"[ReflexRoom] DB error on start: {e}")
                db.rollback()
            finally:
                db.close()

        # matched сообщение обоим
        for role in ("p1", "p2"):
            other = "p2" if role == "p1" else "p1"
            await _safe_send(self.ws[role], {
                "type": "matched",
                "room": self.room_id,
                "role": role,
                "opponent": self.info[other],
                "you": self.info[role],
                "stake": self.stake,
                "best_of": self.best_of,
                "games_preview": [GAMES[g]["name"] for g in self.games],
            })
        await asyncio.sleep(2.0)
        await self._start_round()

    async def _start_round(self):
        if self.finished:
            return
        if self.round_num >= self.best_of:
            await self._finish_match()
            return
        game_id = self.games[self.round_num]
        self.round_results = {"p1": None, "p2": None}
        self.round_start_ts = time.time()
        for role in ("p1", "p2"):
            await _safe_send(self.ws[role], {
                "type": "round_start",
                "round_num": self.round_num + 1,
                "total_rounds": self.best_of,
                "game": game_id,
                "game_name": GAMES[game_id]["name"],
                "game_desc": GAMES[game_id]["desc"],
                "rounds_won": self.rounds_won,
                "start_in_ms": 3000,
            })
        # Если один из игроков AI — планируем его "ход"
        if self.ai_role:
            asyncio.create_task(self._ai_play_round(self.ai_role, game_id))

    async def _ai_play_round(self, role: str, game_id: str):
        """Симулирует игру бота: ждёт elapsed_ms + 3-секундный countdown, потом шлёт результат."""
        # Клиент после round_start ждёт 3 сек (countdown), затем играет
        elapsed = _ai_elapsed_for(game_id, self.ai_difficulty or "medium")
        total_wait = (3000 + elapsed + random.randint(200, 800)) / 1000.0
        await asyncio.sleep(total_wait)
        if self.finished or self.round_num >= self.best_of:
            return
        # Проверяем что раунд ещё тот же
        if self.games[self.round_num] != game_id:
            return
        # Bypass anti-cheat: AI всегда шлёт валидные значения (нам не надо проверять)
        score = _ai_score_for(game_id, self.ai_difficulty or "medium")
        self.round_results[role] = {"score": score, "elapsed_ms": elapsed}
        other = "p2" if role == "p1" else "p1"
        await _safe_send(self.ws[other], {
            "type": "opponent_finished",
            "round_num": self.round_num + 1,
            "opponent_score": score,
        })
        if self.round_results["p1"] is not None and self.round_results["p2"] is not None:
            await self._resolve_round()

    async def handle_result(self, role: str, score, elapsed_ms):
        async with self._round_lock:
            if self.finished or self.round_num >= self.best_of:
                return
            if self.round_results[role] is not None:
                return
            game_id = self.games[self.round_num]
            cfg = GAMES[game_id]
            try:
                score = int(score)
            except Exception:
                score = 0
            try:
                elapsed_ms = int(elapsed_ms)
            except Exception:
                elapsed_ms = 0
            # Допускаем отрицательные score (штраф за ошибки), но не больше max_score по модулю
            max_s = cfg["max_score"]
            if score > max_s: score = max_s
            if score < -max_s: score = -max_s
            if elapsed_ms < cfg["min_elapsed_ms"]:
                score = 0
            if elapsed_ms > cfg["max_elapsed_ms"]:
                elapsed_ms = cfg["max_elapsed_ms"]
            server_elapsed = (time.time() - self.round_start_ts) * 1000
            if server_elapsed < cfg["min_elapsed_ms"] - 500:
                score = 0

            self.round_results[role] = {"score": score, "elapsed_ms": elapsed_ms}

            other = "p2" if role == "p1" else "p1"
            await _safe_send(self.ws[other], {
                "type": "opponent_finished",
                "round_num": self.round_num + 1,
                "opponent_score": score,
            })

            if self.round_results["p1"] is not None and self.round_results["p2"] is not None:
                # Выходим из лока, resolve сам возьмёт если нужно
                pass
        if self.round_results["p1"] is not None and self.round_results["p2"] is not None:
            await self._resolve_round()

    async def _resolve_round(self):
        p1s = self.round_results["p1"]["score"]
        p2s = self.round_results["p2"]["score"]
        if p1s > p2s:
            winner = "p1"; self.rounds_won["p1"] += 1
        elif p2s > p1s:
            winner = "p2"; self.rounds_won["p2"] += 1
        else:
            winner = "draw"
            self.rounds_won["p1"] += 1
            self.rounds_won["p2"] += 1
        self.rounds_log.append({
            "round": self.round_num + 1,
            "game": self.games[self.round_num],
            "p1_score": p1s,
            "p2_score": p2s,
            "winner": winner,
        })
        for role in ("p1", "p2"):
            await _safe_send(self.ws[role], {
                "type": "round_result",
                "round_num": self.round_num + 1,
                "game": self.games[self.round_num],
                "p1_score": p1s,
                "p2_score": p2s,
                "winner": winner,
                "rounds_won": self.rounds_won,
            })
        self.round_num += 1

        needed = self.best_of // 2 + 1
        if self.rounds_won["p1"] >= needed or self.rounds_won["p2"] >= needed:
            await asyncio.sleep(3.0)
            await self._finish_match()
            return
        if self.round_num >= self.best_of:
            await asyncio.sleep(3.0)
            await self._finish_match()
            return
        await asyncio.sleep(3.5)
        await self._start_round()

    async def _finish_match(self):
        if self.finished:
            return
        self.finished = True

        winner_role = None
        if self.rounds_won["p1"] > self.rounds_won["p2"]:
            winner_role = "p1"
        elif self.rounds_won["p2"] > self.rounds_won["p1"]:
            winner_role = "p2"

        final_info = None
        # В AI-режиме — не трогаем БД, только шлём результат клиенту
        if self.ai_role:
            human_role = "p2" if self.ai_role == "p1" else "p1"
            # Небольшой XP бонус за игру с ботом (не ELO!)
            try:
                db = SessionLocal()
                try:
                    p = db.query(Player).filter(Player.id == self.player_ids[human_role]).with_for_update().first()
                    if p:
                        p.xp = (p.xp or 0) + (5 if winner_role == human_role else 2)
                        if winner_role == human_role:
                            p.coins = (p.coins or 0) + {"easy": 5, "medium": 10, "hard": 20}.get(self.ai_difficulty or "medium", 10)
                        # Дневные задачи: vs_ai_win + play + уникальные игры
                        max_rs = max(
                            (r.get("p1_score" if human_role == "p1" else "p2_score", 0) for r in self.rounds_log),
                            default=0,
                        )
                        unique_games = len({r.get("game") for r in self.rounds_log if r.get("game")})
                        _tick_daily_tasks(db, p.id, {
                            "play": True,
                            "win": winner_role == human_role,
                            "vs_ai_win": winner_role == human_role,
                            "perfect": (winner_role == human_role
                                        and self.rounds_won[human_role] == 3
                                        and self.rounds_won["p1" if human_role == "p2" else "p2"] == 0),
                            "max_round_score": max_rs,
                            "unique_games_played": unique_games,
                        })
                        db.commit()
                        final_info = {human_role: {"coins": p.coins, "elo": round(p.reflex_elo or BASE_ELO, 1), "elo_delta": 0.0}}
                finally:
                    db.close()
            except Exception as e:
                print(f"[ReflexRoom AI] xp error: {e}")
            # Шлём match_finished только человеку (у AI ws=None)
            for role in ("p1", "p2"):
                if self.ws[role] is None:
                    continue
                await _safe_send(self.ws[role], {
                    "type": "match_finished",
                    "winner": winner_role,
                    "you_won": winner_role == role,
                    "you": (final_info or {}).get(role),
                    "rounds_won": self.rounds_won,
                    "rounds_log": self.rounds_log,
                    "pot": 0,
                    "vs_ai": True,
                })
            _rooms.pop(self.room_id, None)
            for pid in self.player_ids.values():
                if pid and _active_match_by_player.get(pid) is self:
                    _active_match_by_player.pop(pid, None)
            return

        db = SessionLocal()
        try:
            m = db.query(ReflexMatch).filter(ReflexMatch.id == self.match_db_id).with_for_update().first()
            p1 = db.query(Player).filter(Player.id == self.player_ids["p1"]).with_for_update().first()
            p2 = db.query(Player).filter(Player.id == self.player_ids["p2"]).with_for_update().first()
            if m:
                m.rounds_p1 = self.rounds_won["p1"]
                m.rounds_p2 = self.rounds_won["p2"]
                m.rounds_log = self.rounds_log
                m.status = "finished"
                from datetime import datetime
                m.finished_at = datetime.utcnow()

            if p1 and p2 and m:
                r1 = p1.reflex_elo or BASE_ELO
                r2 = p2.reflex_elo or BASE_ELO
                e1 = 1 / (1 + 10 ** ((r2 - r1) / 400))
                e2 = 1 - e1
                if winner_role == "p1":
                    s1, s2 = 1.0, 0.0
                elif winner_role == "p2":
                    s1, s2 = 0.0, 1.0
                else:
                    s1 = s2 = 0.5
                d1 = self.k_factor * (s1 - e1)
                d2 = self.k_factor * (s2 - e2)
                p1.reflex_elo = round(r1 + d1, 2)
                p2.reflex_elo = round(r2 + d2, 2)
                m.elo_change_p1 = round(d1, 2)
                m.elo_change_p2 = round(d2, 2)
                if winner_role == "p1":
                    p1.reflex_wins = (p1.reflex_wins or 0) + 1
                    p2.reflex_losses = (p2.reflex_losses or 0) + 1
                    m.winner_id = p1.id
                elif winner_role == "p2":
                    p2.reflex_wins = (p2.reflex_wins or 0) + 1
                    p1.reflex_losses = (p1.reflex_losses or 0) + 1
                    m.winner_id = p2.id

                pot = self.stake * 2
                if winner_role == "p1":
                    p1.coins = (p1.coins or 0) + pot + 20
                    p1.xp = (p1.xp or 0) + 15
                elif winner_role == "p2":
                    p2.coins = (p2.coins or 0) + pot + 20
                    p2.xp = (p2.xp or 0) + 15
                else:
                    p1.coins = (p1.coins or 0) + self.stake
                    p2.coins = (p2.coins or 0) + self.stake

                # Дневные задачи — для обоих игроков
                unique_games = len({r.get("game") for r in self.rounds_log if r.get("game")})
                for role, player in (("p1", p1), ("p2", p2)):
                    if not player:
                        continue
                    is_win = winner_role == role
                    is_perfect = (is_win and self.rounds_won[role] == 3 and
                                  self.rounds_won["p1" if role == "p2" else "p2"] == 0)
                    max_rs = max(
                        (r.get("p1_score" if role == "p1" else "p2_score", 0)
                         for r in self.rounds_log),
                        default=0,
                    )
                    elo_d = m.elo_change_p1 if role == "p1" else m.elo_change_p2
                    _tick_daily_tasks(db, player.id, {
                        "play": True,
                        "win": is_win,
                        "stake_win": is_win and self.stake > 0,
                        "perfect": is_perfect,
                        "elo_delta": float(elo_d or 0),
                        "max_round_score": max_rs,
                        "unique_games_played": unique_games,
                    })

                # Достижения
                winner_player = p1 if winner_role == "p1" else (p2 if winner_role == "p2" else None)
                if winner_player:
                    wp_role = winner_role
                    # first_win
                    if (winner_player.reflex_wins or 0) == 1:
                        _grant_achievement(db, winner_player.id, "first_win")
                    if (winner_player.reflex_wins or 0) >= 10:
                        _grant_achievement(db, winner_player.id, "win_10")
                    # perfect 3:0
                    if self.rounds_won[wp_role] == 3 and self.rounds_won["p1" if wp_role == "p2" else "p2"] == 0:
                        _grant_achievement(db, winner_player.id, "perfect")
                    # stake_winner
                    if self.stake > 0:
                        _grant_achievement(db, winner_player.id, "stake_winner")

                # Категорийный ELO: обновляем по категориям игр в этом матче
                try:
                    cat_counts = {}
                    for rl in self.rounds_log:
                        c = GAME_CATEGORIES.get(rl.get("game"))
                        if c: cat_counts[c] = cat_counts.get(c, 0) + 1
                    for cat, count in cat_counts.items():
                        field = CATEGORY_ELO_FIELD.get(cat)
                        if not field: continue
                        r1_cat = getattr(p1, field, 1000.0) or 1000.0
                        r2_cat = getattr(p2, field, 1000.0) or 1000.0
                        e1c = 1 / (1 + 10 ** ((r2_cat - r1_cat) / 400))
                        cat_k = 16  # меньше K для категорий — более стабильный
                        d1c = cat_k * (s1 - e1c)
                        d2c = cat_k * (s2 - (1 - e1c))
                        setattr(p1, field, round(r1_cat + d1c, 2))
                        setattr(p2, field, round(r2_cat + d2c, 2))
                except Exception as e:
                    print(f"[cat elo error] {e}")

                # Battle Pass XP: матч = 10, победа = +20, perfect = +10 сверху
                try:
                    from routes.api import _grant_pass_xp
                    for role, pl in (("p1", p1), ("p2", p2)):
                        if not pl: continue
                        xp = 10
                        if winner_role == role: xp += 20
                        if winner_role == role and self.rounds_won[role] == 3 and \
                           self.rounds_won["p1" if role == "p2" else "p2"] == 0:
                            xp += 10
                        _grant_pass_xp(db, pl.id, xp)
                except Exception as e:
                    print(f"[pass xp error] {e}")

            db.commit()
            if p1 and p2 and m:
                final_info = {
                    "p1": {"coins": p1.coins, "elo": round(p1.reflex_elo, 1),
                           "elo_delta": round(m.elo_change_p1, 1)},
                    "p2": {"coins": p2.coins, "elo": round(p2.reflex_elo, 1),
                           "elo_delta": round(m.elo_change_p2, 1)},
                }
        except Exception as e:
            print(f"[ReflexRoom] DB error on finish: {e}")
            db.rollback()
        finally:
            db.close()

        for role in ("p1", "p2"):
            await _safe_send(self.ws[role], {
                "type": "match_finished",
                "winner": winner_role,
                "you_won": winner_role == role,
                "you": final_info[role] if final_info else None,
                "rounds_won": self.rounds_won,
                "rounds_log": self.rounds_log,
                "pot": self.stake * 2,
            })
        _rooms.pop(self.room_id, None)
        # Снимаем из индекса активных матчей
        for pid in self.player_ids.values():
            if _active_match_by_player.get(pid) is self:
                _active_match_by_player.pop(pid, None)

    async def handle_disconnect(self, role: str):
        if self.finished:
            return
        self.connected[role] = False
        other = "p2" if role == "p1" else "p1"
        # Уведомляем оппонента что соперник отвалился
        await _safe_send(self.ws[other], {
            "type": "opponent_disconnected",
            "grace_seconds": RECONNECT_GRACE_SEC,
        })
        # Даём grace-период на реконнект
        try:
            await asyncio.wait_for(self._reconnect_events[role].wait(), timeout=RECONNECT_GRACE_SEC)
            # Успели зареконнектиться!
            self.connected[role] = True
            await _safe_send(self.ws[other], {"type": "opponent_reconnected"})
            return
        except asyncio.TimeoutError:
            pass
        if self.finished:
            return
        # Не дождались — technical loss
        needed = self.best_of // 2 + 1
        while self.rounds_won[other] < needed and self.round_num < self.best_of:
            self.rounds_won[other] += 1
            self.round_num += 1
        await self._finish_match()

    async def handle_reconnect(self, role: str, new_ws: WebSocket):
        """Переподключает игрока к комнате. Возвращает True если успешно."""
        if self.finished:
            return False
        # Закрываем старый ws (если ещё открыт)
        old_ws = self.ws.get(role)
        if old_ws is not None and old_ws is not new_ws:
            try: await old_ws.close()
            except Exception: pass
        self.ws[role] = new_ws
        self.connected[role] = True
        # Сигналим handle_disconnect что мы вернулись
        ev = self._reconnect_events.get(role)
        if ev:
            ev.set()
            self._reconnect_events[role] = asyncio.Event()  # reset для будущих disconnect'ов
        # Если раунд уже идёт и клиент ещё не прислал result — перезапускаем раунд для него
        game_id = self.games[self.round_num] if self.round_num < self.best_of else None
        if game_id and self.round_results.get(role) is None:
            # Раунд активен, клиент отвалился до отправки score — пересылаем round_start
            self.round_start_ts = time.time()  # сбрасываем timestamp (anti-cheat будет считать от нового времени)
            await _safe_send(new_ws, {
                "type": "round_start",
                "round_num": self.round_num + 1,
                "total_rounds": self.best_of,
                "game": game_id,
                "game_name": GAMES[game_id]["name"],
                "game_desc": GAMES[game_id]["desc"],
                "rounds_won": self.rounds_won,
                "start_in_ms": 3000,
            })
        else:
            # Раунд завершён или клиент уже отправил result — шлём обзор
            await _safe_send(new_ws, {
                "type": "rejoined",
                "round_num": self.round_num + 1,
                "total_rounds": self.best_of,
                "rounds_won": self.rounds_won,
                "game": game_id,
                "game_name": GAMES[game_id]["name"] if game_id else None,
                "game_desc": GAMES[game_id]["desc"] if game_id else None,
            })
        return True

    async def serve_role(self, role: str):
        """Запускается в ws_queue после того как игрок вошёл в матч. Первый вызов стартует матч."""
        await self.ensure_started()
        ws = self.ws[role]
        try:
            while not self.finished:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=120.0)
                except asyncio.TimeoutError:
                    if self.finished:
                        return
                    continue
                try:
                    msg = _json.loads(raw)
                except Exception:
                    continue
                mt = msg.get("type")
                if mt == "result":
                    await self.handle_result(role, msg.get("score", 0), msg.get("elapsed_ms", 0))
                elif mt == "emote":
                    code = str(msg.get("code", ""))[:20]
                    if code:
                        other = "p2" if role == "p1" else "p1"
                        await _safe_send(self.ws[other], {
                            "type": "emote", "from": role, "code": code,
                        })
        except WebSocketDisconnect:
            await self.handle_disconnect(role)
        except Exception as e:
            print(f"[ReflexRoom {self.room_id}] serve_role({role}) error: {e}")
            await self.handle_disconnect(role)


# ─── Queue cleanup helper ───
def _cleanup_expired():
    now = time.time()
    _queue[:] = [e for e in _queue if now - e.joined < MAX_QUEUE_WAIT]


# ─── WebSocket endpoint ───

@router.websocket("/ws/queue")
async def ws_queue(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token") if hasattr(ws, "query_params") else None
    player_id = None
    if token:
        payload = verify_token(token)
        if payload:
            player_id = payload.get("player_id")
    if not player_id:
        await _safe_send(ws, {"type": "error", "msg": "Требуется авторизация"})
        try: await ws.close()
        except Exception: pass
        return

    try:
        msg_raw = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
    except (asyncio.TimeoutError, WebSocketDisconnect, Exception):
        try: await ws.close()
        except Exception: pass
        return
    try:
        msg = _json.loads(msg_raw)
    except Exception:
        await _safe_send(ws, {"type": "error", "msg": "Некорректное сообщение"})
        try: await ws.close()
        except Exception: pass
        return
    # Реконнект в активный матч
    if msg.get("type") == "rejoin":
        room = _active_match_by_player.get(player_id)
        if not room or room.finished:
            await _safe_send(ws, {"type": "no_active_match"})
            try: await ws.close()
            except Exception: pass
            return
        # Определяем роль и переподключаем
        role = "p1" if room.player_ids["p1"] == player_id else "p2"
        ok = await room.handle_reconnect(role, ws)
        if not ok:
            try: await ws.close()
            except Exception: pass
            return
        # Обслуживаем дальше
        await room.serve_role(role)
        try: await ws.close()
        except Exception: pass
        return

    # Создать комнату по приглашению (custom room)
    if msg.get("type") == "create_room":
        # Генерим уникальный код
        async with _custom_rooms_lock:
            # Чистим устаревшие (>5 минут)
            now = time.time()
            for k, v in list(_custom_rooms.items()):
                if now - v.get("created_at", now) > 300:
                    _custom_rooms.pop(k, None)
            code = None
            for _ in range(10):
                c = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                if c not in _custom_rooms:
                    code = c; break
            if not code:
                await _safe_send(ws, {"type": "error", "msg": "Не удалось создать комнату"})
                try: await ws.close()
                except Exception: pass
                return
            entry = {
                "creator_id": player_id,
                "ws": ws,
                "event": asyncio.Event(),
                "room_holder": {},
                "created_at": now,
            }
            _custom_rooms[code] = entry
        await _safe_send(ws, {"type": "room_created", "code": code})

        # Просто ждём event (без параллельного recv_task - убрали race condition).
        # Клиент "cancel" делает через закрытие WebSocket — это поймается в serve_role
        # как WebSocketDisconnect, либо через timeout.
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=300)
        except asyncio.TimeoutError:
            async with _custom_rooms_lock:
                _custom_rooms.pop(code, None)
            await _safe_send(ws, {"type": "room_expired"})
            try: await ws.close()
            except Exception: pass
            return
        except asyncio.CancelledError:
            async with _custom_rooms_lock:
                _custom_rooms.pop(code, None)
            return

        # Event сработал — партнёр присоединился. Забираем комнату и идём играть.
        room = entry["room_holder"].get("room")
        role = entry["room_holder"].get("role")
        async with _custom_rooms_lock:
            _custom_rooms.pop(code, None)
        if room and role:
            await room.serve_role(role)
            try: await ws.close()
            except Exception: pass
            return
        # На всякий случай — если room_holder пуст (не должно случаться)
        await _safe_send(ws, {"type": "error", "msg": "Ошибка создания комнаты"})
        try: await ws.close()
        except Exception: pass
        return

    # Присоединиться к комнате по коду
    if msg.get("type") == "join_room":
        code = (msg.get("code") or "").strip().upper()
        async with _custom_rooms_lock:
            entry = _custom_rooms.get(code)
            if not entry or entry["creator_id"] == player_id:
                await _safe_send(ws, {"type": "error", "msg": "Комната не найдена или это твой код"})
                try: await ws.close()
                except Exception: pass
                return
            # Создаём Room
            room_id = _gen_room()
            room = ReflexRoom(room_id, entry["creator_id"], player_id, 0,
                              entry["ws"], ws)
            _rooms[room_id] = room
            entry["room_holder"]["room"] = room
            entry["room_holder"]["role"] = "p1"
            entry["event"].set()
        await room.serve_role("p2")
        try: await ws.close()
        except Exception: pass
        return

    # Играть с ботом (без очереди, сразу старт)
    if msg.get("type") == "vs_ai":
        difficulty = msg.get("difficulty", "medium")
        if difficulty not in AI_PROFILES:
            difficulty = "medium"
        room_id = _gen_room()
        # Человек = p1, бот = p2
        room = ReflexRoom(room_id, player_id, 0, 0, ws, None,
                          ai_role="p2", ai_difficulty=difficulty)
        _rooms[room_id] = room
        await room.serve_role("p1")
        try: await ws.close()
        except Exception: pass
        return

    if msg.get("type") != "queue":
        await _safe_send(ws, {"type": "error", "msg": "Ожидалось type=queue"})
        try: await ws.close()
        except Exception: pass
        return
    # Если уже в матче — предлагаем rejoin вместо новой очереди
    if player_id in _active_match_by_player and not _active_match_by_player[player_id].finished:
        await _safe_send(ws, {"type": "already_in_match", "msg": "Ты уже в матче. Переподключись через rejoin."})
        try: await ws.close()
        except Exception: pass
        return

    try:
        stake = int(msg.get("stake_coins", 0))
    except Exception:
        stake = 0
    stake = max(MIN_STAKE, min(MAX_STAKE, stake))
    mode = msg.get("mode", "normal")
    if mode not in ("normal", "deathmatch"):
        mode = "normal"
    if mode == "deathmatch" and stake < DEATHMATCH_MIN_STAKE:
        await _safe_send(ws, {"type": "error", "msg": f"Deathmatch: минимальная ставка {DEATHMATCH_MIN_STAKE} 💰"})
        try: await ws.close()
        except Exception: pass
        return

    db = SessionLocal()
    try:
        me = db.query(Player).filter(Player.id == player_id).first()
        if not me:
            await _safe_send(ws, {"type": "error", "msg": "Игрок не найден"})
            try: await ws.close()
            except Exception: pass
            return
        if (me.coins or 0) < stake:
            await _safe_send(ws, {"type": "error", "msg": f"Недостаточно монет (нужно {stake})"})
            try: await ws.close()
            except Exception: pass
            return
    finally:
        db.close()

    await _safe_send(ws, {"type": "queued", "stake": stake})

    # ── Атомарная попытка найти оппонента ──
    matched_room: Optional[ReflexRoom] = None
    matched_role: Optional[str] = None
    my_entry: Optional[_QueueEntry] = None

    async with _queue_lock:
        _cleanup_expired()
        opponent_entry = None
        for i, e in enumerate(_queue):
            if e.stake == stake and e.mode == mode and e.player_id != player_id:
                opponent_entry = _queue.pop(i)
                break
        if opponent_entry is not None:
            # Я — p2. Создаю комнату, сигналю p1.
            room_id = _gen_room()
            room = ReflexRoom(room_id, opponent_entry.player_id, player_id, stake,
                              opponent_entry.ws, ws, mode=mode)
            _rooms[room_id] = room
            # Передаём p1'у информацию о комнате
            opponent_entry.room_holder["room"] = room
            opponent_entry.room_holder["role"] = "p1"
            opponent_entry.event.set()
            matched_room = room
            matched_role = "p2"
        else:
            # Я — p1. Встаю в очередь.
            my_entry = _QueueEntry(ws, player_id, stake, mode=mode)
            _queue.append(my_entry)

    if matched_room is None:
        # ── Ждём, пока матч-мейкер сигналит event, либо cancel/disconnect, либо таймаут ──
        async def wait_client_event():
            """Слушает cancel или disconnect от клиента."""
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        data = _json.loads(raw)
                    except Exception:
                        continue
                    if data.get("type") == "cancel":
                        return "cancel"
            except WebSocketDisconnect:
                return "disconnect"
            except Exception:
                return "disconnect"

        recv_task = asyncio.create_task(wait_client_event())
        event_task = asyncio.create_task(my_entry.event.wait())

        done, pending = await asyncio.wait(
            [recv_task, event_task],
            timeout=MAX_QUEUE_WAIT,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Отменяем висящие задачи и дожидаемся их завершения (чтобы освободить ws)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if event_task in done and my_entry.event.is_set():
            matched_room = my_entry.room_holder.get("room")
            matched_role = my_entry.room_holder.get("role")
        elif recv_task in done:
            result = None
            try:
                result = recv_task.result()
            except Exception:
                result = "disconnect"
            # Удаляем себя из очереди
            async with _queue_lock:
                _queue[:] = [e for e in _queue if e is not my_entry]
            if result == "cancel":
                await _safe_send(ws, {"type": "cancelled"})
            try: await ws.close()
            except Exception: pass
            return
        else:
            # Таймаут
            async with _queue_lock:
                _queue[:] = [e for e in _queue if e is not my_entry]
            await _safe_send(ws, {"type": "timeout"})
            try: await ws.close()
            except Exception: pass
            return

    # ── Мы в матче. Обслуживаем свою сторону. ──
    if matched_room and matched_role:
        await matched_room.serve_role(matched_role)
    # Закрываем ws после окончания матча
    try:
        await ws.close()
    except Exception:
        pass
