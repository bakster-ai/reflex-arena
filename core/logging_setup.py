"""
Structured JSON logging с correlation_id.
Каждый HTTP-запрос получает уникальный request_id, который прокидывается через все логи.
"""
import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Optional

# Context: request_id берётся из middleware при входе, доступен любому логу внутри запроса
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
player_id_var: ContextVar[Optional[int]] = ContextVar("player_id", default=None)


class JsonFormatter(logging.Formatter):
    """Формат: одна строка JSON на запись лога."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        pid = player_id_var.get()
        if pid:
            payload["player_id"] = pid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Любые extra-поля (log.info("msg", extra={"key": "val"}))
        for k, v in (record.__dict__ or {}).items():
            if k in ("args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
                     "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
                     "name", "pathname", "process", "processName", "relativeCreated",
                     "stack_info", "thread", "threadName", "taskName"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: Optional[str] = None) -> None:
    """Настраивает root-логгер на JSON-формат. Вызвать один раз при старте."""
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    lvl = getattr(logging, level_name, logging.INFO)

    # Сносим дефолтный handler uvicorn — он форматирует свою строку
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(lvl)

    # Приглушим спам от библиотек
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request_id(rid: str) -> None:
    request_id_var.set(rid)


def bind_player_id(pid: Optional[int]) -> None:
    player_id_var.set(pid)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
