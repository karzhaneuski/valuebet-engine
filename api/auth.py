"""Bearer token authentication for ValueBet Engine REST API."""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import Secrets

security = HTTPBearer(auto_error=False)


def get_expected_token() -> str:
    """Read API secret token from environment or Secrets."""
    env_token = os.getenv("API_SECRET_TOKEN") or os.getenv("API_TOKEN")
    if env_token:
        return env_token.strip()
    sec = Secrets()
    token = sec.api_secret_token or sec.api_token
    return token.strip() if token else ""


def verify_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Verify Bearer token against configured API_SECRET_TOKEN.

    All protected endpoints reject requests with missing or invalid tokens (401).
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        detail_msg = (
            "Отсутствует или некорректен заголовок авторизации: "
            "ожидается 'Authorization: Bearer <TOKEN>'"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail_msg,
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = get_expected_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_SECRET_TOKEN не настроен на сервере",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = credentials.credentials.strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный токен авторизации",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return provided
