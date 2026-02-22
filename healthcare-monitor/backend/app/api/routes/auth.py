from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import User
from app.core.security import (
    verify_password, hash_password, create_access_token, get_current_user
)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    created_at: datetime
    last_login: datetime | None


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad credentials")
    user.last_login = datetime.now(timezone.utc)
    await db.commit()
    token = create_access_token(subject=user.username, role=user.role)
    return LoginResponse(access_token=token, role=user.role, username=user.username)


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return UserOut(
        id=str(current_user.id),
        username=current_user.username,
        role=current_user.role,
        created_at=current_user.created_at,
        last_login=current_user.last_login,
    )
