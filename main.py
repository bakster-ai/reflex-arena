"""
Reflex Arena — standalone app.
Self-contained FastAPI service. Separate DB, separate auth, no dependencies on ping-pong platform.
"""
import os
from collections import deque
import time as _time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text

from core.database import engine, Base
from models import models  # noqa — регистрация моделей
from routes import auth as auth_routes
from routes import api as api_routes
from routes import ws as ws_routes

# ── Создание таблиц ──
Base.metadata.create_all(bind=engine)


def run_migrations():
    """Идемпотентные миграции. Безопасно запускать много раз."""
    with engine.connect() as conn:
        migrations = [
            # Колонки Player (на случай если БД старая и у неё нет всех полей)
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS elo_reaction DOUBLE PRECISION DEFAULT 1000.0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS elo_logic DOUBLE PRECISION DEFAULT 1000.0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS elo_memory DOUBLE PRECISION DEFAULT 1000.0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS elo_coordination DOUBLE PRECISION DEFAULT 1000.0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS elo_trivia DOUBLE PRECISION DEFAULT 1000.0",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS referred_by INTEGER",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS referral_bonus_claimed BOOLEAN DEFAULT FALSE",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS is_guest BOOLEAN DEFAULT FALSE",
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS reflex_onboarded BOOLEAN DEFAULT FALSE",
            # Индексы производительности
            "CREATE INDEX IF NOT EXISTS idx_players_nickname ON players(nickname)",
            "CREATE INDEX IF NOT EXISTS idx_players_reflex_elo ON players(reflex_elo DESC)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_matches_p1 ON reflex_matches(p1_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_matches_p2 ON reflex_matches(p2_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_matches_finished ON reflex_matches(finished_at DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_ach_player_code ON reflex_achievements(player_id, code)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_ach_player ON reflex_achievements(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_dt_player_date ON reflex_daily_tasks(player_id, date)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_dc_player_date ON reflex_daily_challenges(player_id, date)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_dc_date_score ON reflex_daily_challenges(date, best_score DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_friends ON reflex_friends(player_id, friend_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_friends_player ON reflex_friends(player_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_seasons_status ON reflex_seasons(status)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_pass ON reflex_pass_progress(player_id, season_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_season_rewards_player ON reflex_season_rewards(player_id)",
        ]
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception:
                pass
        conn.commit()


run_migrations()


# ── FastAPI app ──
app = FastAPI(title="Reflex Arena", version="1.0.0")
app.add_middleware(GZipMiddleware, minimum_size=500)

# CORS
_default_origins = [
    "http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:8000",
]
_env_origins = os.environ.get("CORS_ORIGINS", "")
_allowed_origins = [o.strip() for o in _env_origins.split(",") if o.strip()] or _default_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Sentry (опционально) ──
_sentry_dsn = os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FastApiIntegration(), StarletteIntegration()],
            traces_sample_rate=0.05,
            profiles_sample_rate=0.05,
            environment=os.environ.get("SENTRY_ENV", "production"),
        )
        print("Sentry initialized")
    except Exception as e:
        print(f"Sentry init failed: {e}")

# ── Rate limiting (in-memory sliding window) ──
_RATE_LIMITS = {
    "/api/auth/guest":       (5, 60),
    "/api/auth/register":    (5, 60),
    "/api/auth/login":       (10, 60),
    "/api/shop/":            (30, 60),
    "/api/friends/":         (30, 60),
    "/api/daily_challenge/submit": (30, 60),
    "/api/pass/":            (30, 60),
    "/api/cases/":           (20, 60),
}
_rate_buckets: dict = {}


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    matched = None
    for prefix, limit in _RATE_LIMITS.items():
        if path.startswith(prefix):
            matched = (prefix, limit); break
    if matched is None:
        return await call_next(request)
    prefix, (max_req, window) = matched
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    if not ip: ip = "unknown"
    key = (prefix, ip)
    now = _time.time()
    bucket = _rate_buckets.get(key)
    if bucket is None:
        bucket = deque(); _rate_buckets[key] = bucket
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= max_req:
        return JSONResponse({"error": "rate_limited", "retry_after": window}, status_code=429)
    bucket.append(now)
    return await call_next(request)


# ── Routers ──
app.include_router(auth_routes.router)
app.include_router(api_routes.router)
app.include_router(api_routes.share_router)
app.include_router(ws_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Static + index ──
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
static_dir = os.path.join(frontend_dir, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/manifest.webmanifest")
def serve_manifest():
    return JSONResponse(
        {
            "name": "Reflex Arena",
            "short_name": "Reflex",
            "description": "1v1 на реакцию — browser battle arena",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0e0f13",
            "theme_color": "#0e0f13",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                {"src": "/icon-512.svg", "sizes": "any", "type": "image/svg+xml"},
            ],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/icon-512.svg")
def serve_icon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#ff7a29"/><stop offset="1" stop-color="#ffb86b"/>
</linearGradient></defs>
<rect width="512" height="512" rx="96" fill="#0e0f13"/>
<path d="M195 80 L135 280 L225 280 L185 432 L375 200 L270 200 L330 80 Z"
      fill="url(#g)" stroke="#fff4d6" stroke-width="4" stroke-linejoin="round"/>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-192.png")
@app.get("/icon-512.png")
def serve_icon_png():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/icon-512.svg", status_code=302)


@app.get("/sw.js")
def serve_sw():
    sw = """
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', () => {});
"""
    return Response(content=sw, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


STYLE_PREVIEWS = {"retro", "cyberpunk", "duolingo", "brutal"}


def _render_index(style: str = "") -> Response:
    """Отдаёт index.html с data-style атрибутом на body (для preview-тем)."""
    path = os.path.join(frontend_dir, "index.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return Response("index.html not found", status_code=500)
    if style and style in STYLE_PREVIEWS:
        # Заменяем плейсхолдер внутри body
        html = html.replace("<body><!--STYLE-PLACEHOLDER-->",
                            f'<body data-style="{style}"><!--STYLE-PLACEHOLDER-->')
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
def serve_index():
    return _render_index()


@app.get("/retro")
def serve_style_retro():
    return _render_index("retro")


@app.get("/cyberpunk")
def serve_style_cyberpunk():
    return _render_index("cyberpunk")


@app.get("/duolingo")
def serve_style_duolingo():
    return _render_index("duolingo")


@app.get("/brutal")
def serve_style_brutal():
    return _render_index("brutal")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Всё что не совпало — отдаём index.html (SPA роутинг)."""
    path = full_path or ""
    if path.startswith("api/") or path.startswith("ws/") or path.startswith("share/") or path.startswith("profile/"):
        return Response(status_code=404)
    return _render_index()
