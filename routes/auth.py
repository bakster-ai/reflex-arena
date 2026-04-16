"""
Auth — логин/регистрация/гость.
Эндпоинты:
  POST /api/auth/register
  POST /api/auth/login
  POST /api/auth/guest
  POST /api/auth/claim_guest
  GET  /api/auth/me
"""
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.auth import hash_password, verify_password, make_token, verify_token
from models.models import Player, PlayerPassword

router = APIRouter(prefix="/api/auth", tags=["auth"])

_NICK_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
GUEST_CLAIM_BONUS = 100


def _current_player(authorization: Optional[str], db: Session) -> Optional[Player]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    payload = verify_token(authorization[7:])
    if not payload:
        return None
    return db.query(Player).filter(Player.id == payload.get("player_id")).first()


class RegisterRequest(BaseModel):
    nickname: str
    password: str


class LoginRequest(BaseModel):
    nickname: str
    password: str


class ClaimGuestRequest(BaseModel):
    nickname: str
    password: str


@router.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    nick = (data.nickname or "").strip()
    if not _NICK_RE.match(nick):
        raise HTTPException(400, "Ник: 3-24 символа (латиница/цифры/_-)")
    if len(data.password or "") < 4:
        raise HTTPException(400, "Пароль минимум 4 символа")
    if db.query(Player).filter(Player.nickname == nick).first():
        raise HTTPException(400, "Ник уже занят")
    p = Player(nickname=nick, is_guest=False)
    db.add(p); db.flush()
    db.add(PlayerPassword(player_id=p.id, password_hash=hash_password(data.password)))
    db.commit(); db.refresh(p)
    return {
        "token": make_token(p.id, p.nickname),
        "player_id": p.id,
        "nickname": p.nickname,
    }


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    nick = (data.nickname or "").strip()
    player = db.query(Player).filter(Player.nickname == nick).first()
    if not player:
        raise HTTPException(401, "Неверный никнейм или пароль")
    pwd_row = db.query(PlayerPassword).filter(PlayerPassword.player_id == player.id).first()
    if not pwd_row or not verify_password(data.password, pwd_row.password_hash):
        raise HTTPException(401, "Неверный никнейм или пароль")
    return {
        "token": make_token(player.id, player.nickname),
        "player_id": player.id,
        "nickname": player.nickname,
    }


@router.post("/guest")
def guest_login(db: Session = Depends(get_db)):
    """Создаёт временного гостевого игрока."""
    while True:
        nick = f"guest_{secrets.token_hex(3)}"
        if not db.query(Player).filter(Player.nickname == nick).first():
            break
    p = Player(nickname=nick, is_guest=True)
    db.add(p); db.commit(); db.refresh(p)
    return {
        "token": make_token(p.id, p.nickname),
        "player_id": p.id,
        "nickname": p.nickname,
        "is_guest": True,
    }


@router.post("/claim_guest")
def claim_guest(
    data: ClaimGuestRequest,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """Превращает гостя в полноценного игрока."""
    me = _current_player(authorization, db)
    if not me:
        return {"ok": False, "msg": "Не авторизован"}
    if not me.is_guest:
        return {"ok": False, "msg": "Можно только из гостевого аккаунта"}
    nick = (data.nickname or "").strip()
    if not _NICK_RE.match(nick):
        return {"ok": False, "msg": "Ник: 3-24 символа (латиница/цифры/_-)"}
    if len(data.password or "") < 4:
        return {"ok": False, "msg": "Пароль минимум 4 символа"}
    if db.query(Player).filter(Player.nickname == nick, Player.id != me.id).first():
        return {"ok": False, "msg": "Ник уже занят"}
    me.nickname = nick
    me.is_guest = False
    pw = db.query(PlayerPassword).filter(PlayerPassword.player_id == me.id).first()
    if pw:
        pw.password_hash = hash_password(data.password)
    else:
        db.add(PlayerPassword(player_id=me.id, password_hash=hash_password(data.password)))
    me.coins = (me.coins or 0) + GUEST_CLAIM_BONUS
    db.commit()
    return {
        "ok": True,
        "token": make_token(me.id, me.nickname),
        "nickname": me.nickname,
        "bonus": GUEST_CLAIM_BONUS,
    }


@router.get("/me")
def me(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    player = _current_player(authorization, db)
    if not player:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "player_id": player.id,
        "nickname": player.nickname,
        "is_guest": bool(player.is_guest),
    }
