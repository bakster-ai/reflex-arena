# ⚡ Reflex Arena

Мобильная 1v1 PvP-арена на реакцию. Web + PWA.

## Features

- **20 мини-игр в 5 категориях**: Реакция, Логика, Память, Координация, Эрудиция
- **6 ELO**: общий + 5 категорийных (с лидербордами по каждой)
- **Режимы**: матчмейкинг по ставке, Deathmatch Bo15, игра с AI (3 уровня), комнаты по invite-ссылке, Daily Challenge (соло)
- **Прогрессия**: лиги Bronze→Grandmaster, достижения, дневные задачи, Battle Pass (30 уровней × 28 дней)
- **Косметика**: 5 тем интерфейса, 5 победных эффектов, 30 аватаров
- **Кейсы**: 5 кейсов × 30 предметов = 150 предметов в коллекции
- **500 вопросов эрудиции** (география, наука, история, поп-культура)
- **Социал**: друзья, публичные профили, эмоуты в матче
- **PWA**: install на телефон (iOS Safari + Android Chrome), мобайл-first вёрстка, haptic feedback
- **Надёжность**: авто-реконнект (30s grace period), rate limiting, Sentry hook

## Tech stack

- Backend: Python 3.11 + FastAPI + SQLAlchemy + WebSockets
- DB: PostgreSQL (prod) / SQLite (dev)
- Frontend: vanilla JS SPA, Canvas/SVG, CSS custom properties

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Запуск (SQLite — для разработки, создастся автоматически)
uvicorn main:app --reload --port 8000
```

Открой http://127.0.0.1:8000/ — гостевой аккаунт создастся автоматически.

## Deploy на Railway

1. В Railway: **New Project → Deploy from GitHub repo → выбрать `bakster-ai/reflex-arena`**
2. Добавить сервис **PostgreSQL** (кнопка «+ New» → Database → Postgres). `DATABASE_URL` проставится автоматически.
3. В **Settings → Variables** задать:
   - `JWT_SECRET` — случайная строка, 32+ символа (обязательно, иначе токены небезопасны)
   - `SENTRY_DSN` — опционально, для мониторинга ошибок
   - `CORS_ORIGINS` — если фронт будет на другом домене
4. **Settings → Networking → Generate Domain** — получишь публичный URL.
5. Миграции применяются автоматически при старте (`run_migrations()` в `main.py`).

## Env variables

| Name | Required | Default | Описание |
|---|---|---|---|
| `DATABASE_URL` | Да (в prod) | `sqlite:///./reflex_dev.db` | Postgres connection string |
| `JWT_SECRET` | **Да в prod** | dev default | Секрет для JWT-токенов |
| `SENTRY_DSN` | Нет | — | DSN Sentry для error tracking |
| `SENTRY_ENV` | Нет | `production` | Среда для Sentry |
| `CORS_ORIGINS` | Нет | localhost | Через запятую |
| `PORT` | Нет (Railway ставит сам) | 8000 | Порт uvicorn |

## Структура

```
reflex-arena/
├── main.py               # FastAPI app, middleware, миграции
├── core/
│   ├── database.py       # SQLAlchemy engine/session
│   └── auth.py           # JWT-токены, password hashing
├── models/
│   └── models.py         # Player + Reflex* таблицы
├── routes/
│   ├── auth.py           # /api/auth/*
│   ├── api.py            # /api/* (игровые эндпоинты) + /share/*, /profile/*
│   └── ws.py             # /ws/queue (WebSocket матчмейкинг)
├── frontend/
│   ├── index.html        # вся UI, JS, CSS в одном файле
│   └── static/
│       └── trivia_questions.js  # 500 вопросов эрудиции
├── Dockerfile
├── Procfile
├── railway.json
├── nixpacks.toml
├── requirements.txt
└── .gitignore
```

## License

Private — all rights reserved.
