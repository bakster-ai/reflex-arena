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
            # ── Daily streak ──
            """CREATE TABLE IF NOT EXISTS reflex_login_streaks (
                id SERIAL PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES players(id),
                current_streak INTEGER DEFAULT 0,
                max_streak INTEGER DEFAULT 0,
                last_login_date VARCHAR,
                last_claimed_date VARCHAR,
                total_days_logged INTEGER DEFAULT 0
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_streak_player ON reflex_login_streaks(player_id)",
            # ── Events analytics ──
            """CREATE TABLE IF NOT EXISTS reflex_events (
                id SERIAL PRIMARY KEY,
                player_id INTEGER REFERENCES players(id),
                event_type VARCHAR NOT NULL,
                payload JSON,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_reflex_events_type ON reflex_events(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_events_created ON reflex_events(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_events_player ON reflex_events(player_id)",
            # ── Push subscriptions ──
            """CREATE TABLE IF NOT EXISTS reflex_push_subscriptions (
                id SERIAL PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES players(id),
                endpoint VARCHAR NOT NULL UNIQUE,
                keys_json JSON NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_reflex_push_player ON reflex_push_subscriptions(player_id)",
            # ── Премиум-валюта (гемы) ──
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS gems INTEGER DEFAULT 0",
            # ── Клубы ──
            """CREATE TABLE IF NOT EXISTS reflex_clubs (
                id SERIAL PRIMARY KEY,
                name VARCHAR NOT NULL UNIQUE,
                tag VARCHAR(5) NOT NULL UNIQUE,
                owner_id INTEGER NOT NULL REFERENCES players(id),
                description VARCHAR,
                icon VARCHAR DEFAULT '🏰',
                member_count INTEGER DEFAULT 1,
                total_wins INTEGER DEFAULT 0,
                total_matches INTEGER DEFAULT 0,
                rating FLOAT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS reflex_club_members (
                id SERIAL PRIMARY KEY,
                club_id INTEGER NOT NULL REFERENCES reflex_clubs(id),
                player_id INTEGER NOT NULL REFERENCES players(id),
                role VARCHAR DEFAULT 'member',
                contribution INTEGER DEFAULT 0,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_club_member ON reflex_club_members(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_club_members_club ON reflex_club_members(club_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_clubs_rating ON reflex_clubs(rating DESC)",
            # ── Турниры ──
            """CREATE TABLE IF NOT EXISTS reflex_tournaments (
                id SERIAL PRIMARY KEY,
                week_key VARCHAR NOT NULL UNIQUE,
                status VARCHAR DEFAULT 'open',
                bracket JSON,
                winner_id INTEGER REFERENCES players(id),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS reflex_tournament_signups (
                id SERIAL PRIMARY KEY,
                tournament_id INTEGER NOT NULL REFERENCES reflex_tournaments(id),
                player_id INTEGER NOT NULL REFERENCES players(id),
                seed INTEGER,
                eliminated_at_round INTEGER,
                final_rank INTEGER,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reflex_tourney_signup ON reflex_tournament_signups(tournament_id, player_id)",
            # ── Payments (Telegram Stars / fallback) ──
            """CREATE TABLE IF NOT EXISTS reflex_payments (
                id SERIAL PRIMARY KEY,
                player_id INTEGER REFERENCES players(id),
                provider VARCHAR NOT NULL,
                external_id VARCHAR,
                product_id VARCHAR NOT NULL,
                amount_minor INTEGER DEFAULT 0,
                currency VARCHAR DEFAULT 'XTR',
                status VARCHAR DEFAULT 'pending',
                gems_granted INTEGER DEFAULT 0,
                payload JSON,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )""",
            "CREATE INDEX IF NOT EXISTS idx_reflex_payments_player ON reflex_payments(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_payments_ext ON reflex_payments(external_id)",
            # Индексы под event-фильтры (report, club_chat, club_war, ad_reward, admin_broadcast)
            "CREATE INDEX IF NOT EXISTS idx_reflex_events_player_type ON reflex_events(player_id, event_type)",
            "CREATE INDEX IF NOT EXISTS idx_reflex_events_type_created ON reflex_events(event_type, created_at DESC)",
        ]
        # Каждая миграция в своей транзакции — чтобы одна битая не аборнула остальные
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                try: print(f"[migration] skip: {str(e)[:200]}")
                except Exception: pass


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


def _render_index() -> Response:
    """Отдаёт index.html. Тема применяется фронтом из applyTheme (purchase-based)."""
    path = os.path.join(frontend_dir, "index.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return Response("index.html not found", status_code=500)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
def serve_index():
    return _render_index()


@app.get("/admin")
def serve_admin():
    """Простая админ-страница. Логин через токен в поле ввода."""
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Admin — Reflex Arena</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:#0e0f13;color:#e5e7eb;padding:16px;}
input,button{background:#1a1d26;border:1px solid #2a2f3a;color:#e5e7eb;padding:8px 12px;border-radius:6px;font-size:14px;}
.card{background:#1a1d26;border:1px solid #2a2f3a;border-radius:10px;padding:14px;margin-bottom:12px;}
h1{font-size:20px;margin:0 0 12px;}h2{font-size:16px;margin:0 0 8px;color:#ff7a29;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;}
.stat{background:#0e0f13;padding:10px;border-radius:8px;}
.stat .v{font-size:22px;font-weight:800;color:#ffb86b;}.stat .l{font-size:11px;color:#8b92a5;}
table{width:100%;border-collapse:collapse;}td,th{padding:6px;border-bottom:1px solid #2a2f3a;text-align:left;font-size:13px;}
</style></head>
<body>
<h1>🔧 Reflex Arena — Admin</h1>
<div class="card">
  <label>ADMIN_TOKEN</label>
  <input id="tok" type="password" placeholder="токен из env" style="width:260px;" />
  <button onclick="load()">Загрузить</button>
</div>
<div id="out"></div>
<div class="card">
  <h2>🔍 Поиск игрока</h2>
  <input id="q" placeholder="id или ник" /><button onclick="findP()">Найти</button>
  <div id="pout"></div>
</div>
<div class="card">
  <h2>📢 Broadcast</h2>
  <textarea id="bc" placeholder="сообщение всем игрокам" style="width:100%;min-height:60px;background:#0e0f13;color:#e5e7eb;border:1px solid #2a2f3a;border-radius:6px;padding:8px;"></textarea>
  <button onclick="bcast()">Отправить</button>
</div>
<script>
function H(){return {'Authorization':'Bearer '+document.getElementById('tok').value};}
async function load(){
  const r = await fetch('/api/admin/dashboard',{headers:H()}).then(r=>r.json());
  if(!r.ok){document.getElementById('out').innerHTML='<div style="color:#ff4d6d">'+(r.msg||'err')+'</div>';return;}
  document.getElementById('out').innerHTML = `
  <div class="card"><h2>Общая статистика</h2>
    <div class="grid">
      <div class="stat"><div class="v">${r.total_players}</div><div class="l">Всего игроков</div></div>
      <div class="stat"><div class="v">${r.registered}</div><div class="l">Registered</div></div>
      <div class="stat"><div class="v">${r.guests}</div><div class="l">Guests</div></div>
      <div class="stat"><div class="v">${r.total_matches}</div><div class="l">Матчей завершено</div></div>
      <div class="stat"><div class="v">${r.dau}</div><div class="l">DAU (24ч)</div></div>
      <div class="stat"><div class="v">${r.wau}</div><div class="l">WAU (7д)</div></div>
      <div class="stat"><div class="v">${r.mau}</div><div class="l">MAU (30д)</div></div>
      <div class="stat"><div class="v">${r.revenue_stars_completed} ⭐</div><div class="l">Revenue (XTR)</div></div>
      <div class="stat"><div class="v">${r.revenue_payments_count}</div><div class="l">Платежей</div></div>
    </div>
  </div>
  <div class="card"><h2>Топ event-type за 7 дней</h2>
    <table><thead><tr><th>Event</th><th>Count</th></tr></thead><tbody>
    ${(r.top_events_7d||[]).map(e=>`<tr><td>${e.event}</td><td>${e.count}</td></tr>`).join('')}
    </tbody></table>
  </div>
  `;
}
async function findP(){
  const q = document.getElementById('q').value;
  const r = await fetch('/api/admin/player/'+encodeURIComponent(q),{headers:H()}).then(r=>r.json());
  if(!r.ok){document.getElementById('pout').innerHTML='<div style="color:#ff4d6d">'+(r.msg||'err')+'</div>';return;}
  const p = r.player;
  document.getElementById('pout').innerHTML = `
    <div style="margin-top:10px;background:#0e0f13;padding:10px;border-radius:8px;">
    <div><b>#${p.id} ${p.nickname}</b> ${r.tier.icon} ${r.tier.tier_ru} ${r.tier.division}</div>
    <div>ELO: ${p.elo} • W/L: ${p.wins}/${p.losses} • coins: ${p.coins} • gems: ${p.gems}</div>
    <div>guest: ${p.is_guest} • создан: ${p.created_at}</div>
    <div style="color:#ff4d6d">Жалоб на игрока: ${r.reports_against}</div>
    </div>`;
}
async function bcast(){
  const text = document.getElementById('bc').value.trim();
  if(!text) return;
  const r = await fetch('/api/admin/broadcast',{method:'POST',headers:{...H(),'Content-Type':'application/json'},body:JSON.stringify({text})}).then(r=>r.json());
  alert(r.msg || (r.ok?'queued':'error'));
}
</script></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})


@app.get("/replay/{match_id}")
def serve_replay(match_id: int):
    """Replay-viewer для матча. Подтягивает /api/replay/{match_id} и воспроизводит раунды."""
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Replay — Reflex Arena</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#0e0f13;color:#e5e7eb;font-family:system-ui,-apple-system,sans-serif;padding:20px;}
.wrap{max-width:720px;margin:0 auto;}
.card{background:#1a1d26;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin-bottom:12px;}
h1{margin:0 0 8px;font-size:22px;}
.vs{display:flex;justify-content:space-between;align-items:center;}
.p{font-size:18px;font-weight:800;}.p.w{color:#26d67f;}.p.l{color:#8b92a5;}
.rnd{background:#0e0f13;border-left:3px solid #ff7a29;padding:10px;margin-bottom:8px;border-radius:0 8px 8px 0;}
a{color:#ff7a29;}
</style></head>
<body><div class="wrap" id="w">Загрузка...</div>
<script>
const mid = location.pathname.split('/').pop();
fetch('/api/replay/'+mid).then(r=>r.json()).then(r=>{
  if(!r.ok){document.getElementById('w').innerHTML='<div style="color:#ff4d6d">'+(r.msg||'err')+'</div>';return;}
  const m = r.match;
  const w = (x)=> m.winner_id === x ? 'w' : 'l';
  document.getElementById('w').innerHTML = `
    <div class="card">
      <h1>Матч #${m.id}</h1>
      <div class="vs">
        <div class="p ${w(m.p1.id)}">${m.p1.nickname} (${m.rounds_p1})</div>
        <div style="color:#8b92a5;">vs</div>
        <div class="p ${w(m.p2.id)}">(${m.rounds_p2}) ${m.p2.nickname}</div>
      </div>
      <div style="color:#8b92a5;font-size:12px;margin-top:8px;">
        ΔELO: ${m.elo_change_p1>0?'+':''}${m.elo_change_p1} / ${m.elo_change_p2>0?'+':''}${m.elo_change_p2}
        ${m.finished_at ? ' • '+new Date(m.finished_at).toLocaleString() : ''}
      </div>
    </div>
    <div class="card">
      <div style="font-size:13px;color:#8b92a5;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;">Раунды</div>
      ${(m.rounds_log||[]).map((r,i)=>`
        <div class="rnd">
          <div><b>Раунд ${i+1}</b> — ${r.game||'?'}</div>
          <div style="font-size:13px;">${m.p1.nickname}: <b>${r.p1_score||0}</b> vs <b>${r.p2_score||0}</b> :${m.p2.nickname}</div>
        </div>
      `).join('')}
    </div>
    <div style="text-align:center;margin-top:14px;"><a href="/">← На главную</a></div>
  `;
});
</script></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Всё что не совпало — отдаём index.html (SPA роутинг)."""
    path = full_path or ""
    if path.startswith("api/") or path.startswith("ws/") or path.startswith("share/") or path.startswith("profile/"):
        return Response(status_code=404)
    return _render_index()
