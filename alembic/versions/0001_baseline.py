"""Baseline: текущее состояние схемы (идемпотентно).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-18

Эта миграция соответствует состоянию после run_migrations() в main.py до настоящего момента.
Все операции идемпотентны (IF NOT EXISTS / IF EXISTS) — safe на существующей prod-БД.
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    # Player columns
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS elo_reaction DOUBLE PRECISION DEFAULT 1000.0",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS elo_logic DOUBLE PRECISION DEFAULT 1000.0",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS elo_memory DOUBLE PRECISION DEFAULT 1000.0",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS elo_coordination DOUBLE PRECISION DEFAULT 1000.0",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS elo_trivia DOUBLE PRECISION DEFAULT 1000.0",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS referred_by INTEGER",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS referral_bonus_claimed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS is_guest BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS reflex_onboarded BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS players ADD COLUMN IF NOT EXISTS gems INTEGER DEFAULT 0",
    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_players_nickname ON players(nickname)",
    "CREATE INDEX IF NOT EXISTS idx_players_reflex_elo ON players(reflex_elo DESC)",
]


def upgrade() -> None:
    conn = op.get_bind()
    from sqlalchemy import text
    for sql in UPGRADE_SQL:
        try:
            conn.execute(text(sql))
        except Exception:
            pass  # игнорим — идемпотентно


def downgrade() -> None:
    # Baseline — downgrade не имеет смысла
    pass
