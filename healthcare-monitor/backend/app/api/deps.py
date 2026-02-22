"""Shared FastAPI dependencies."""
from fastapi import Header, HTTPException, status
from app.core.security import verify_agent_api_key


async def require_agent_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    if not verify_agent_api_key(x_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent API key")
    return x_api_key
