"""
APScheduler — фоновые задачи в том же процессе (без Redis).
Задачи: рассылка broadcast из admin, очистка старых events, расчёт клановых рейтингов.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("scheduler")
_scheduler: Optional[BackgroundScheduler] = None


def _process_broadcast_queue():
    """Каждые 30с проверяем очередь admin_broadcast и шлём push-нотификации."""
    from core.database import SessionLocal
    from models.models import ReflexEvent, ReflexPushSubscription
    db = SessionLocal()
    try:
        # Берём pending-broadcasts (payload.queued_at < now — но не processed)
        pending = db.query(ReflexEvent).filter(
            ReflexEvent.event_type == "admin_broadcast",
        ).order_by(ReflexEvent.created_at.desc()).limit(5).all()
        for ev in pending:
            p = ev.payload or {}
            if p.get("processed"):
                continue
            text = p.get("text", "")
            if not text:
                continue
            # TODO: реальный Web Push через pywebpush. Пока логируем.
            subs = db.query(ReflexPushSubscription).limit(500).all()
            log.info(f"broadcast dispatch", extra={
                "text_preview": text[:50], "subscriptions": len(subs), "event_id": ev.id,
            })
            p["processed"] = True
            p["dispatched_at"] = datetime.utcnow().isoformat()
            p["dispatched_to"] = len(subs)
            ev.payload = p
            db.commit()
    except Exception as e:
        log.error(f"broadcast queue error: {e}")
    finally:
        db.close()


def _cleanup_old_events():
    """Раз в час удаляем события старше 90 дней, чтоб не пухла таблица."""
    from core.database import SessionLocal
    from models.models import ReflexEvent
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        deleted = db.query(ReflexEvent).filter(
            ReflexEvent.created_at < cutoff,
            # Не трогаем платёжные и admin-события
            ~ReflexEvent.event_type.in_(["admin_broadcast", "gems_granted", "gems_spent"]),
        ).delete(synchronize_session=False)
        db.commit()
        if deleted:
            log.info(f"cleanup old events: deleted {deleted} rows")
    except Exception as e:
        log.error(f"cleanup error: {e}")
    finally:
        db.close()


def _recompute_club_ratings():
    """Раз в 10 минут пересчёт рейтинга клубов (если что-то разъехалось)."""
    from core.database import SessionLocal
    from models.models import ReflexClub
    db = SessionLocal()
    try:
        clubs = db.query(ReflexClub).all()
        for c in clubs:
            if (c.total_matches or 0) <= 0:
                continue
            wr = (c.total_wins or 0) / c.total_matches
            new_rating = round(wr * 100 + c.total_matches * 0.5, 2)
            if abs((c.rating or 0) - new_rating) > 0.5:
                c.rating = new_rating
        db.commit()
    except Exception as e:
        log.error(f"club ratings recompute: {e}")
    finally:
        db.close()


def start_scheduler():
    """Запустить scheduler. Вызывать один раз в main.py после init БД."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(_process_broadcast_queue, IntervalTrigger(seconds=30),
                       id="broadcast_queue", max_instances=1, coalesce=True)
    _scheduler.add_job(_cleanup_old_events, IntervalTrigger(hours=1),
                       id="cleanup_events", max_instances=1, coalesce=True)
    _scheduler.add_job(_recompute_club_ratings, IntervalTrigger(minutes=10),
                       id="club_ratings", max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("scheduler started with 3 jobs")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
