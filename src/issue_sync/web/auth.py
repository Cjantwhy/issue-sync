"""Dashboard sign-in: HTTP Basic against DASHBOARD_USERNAME / DASHBOARD_PASSWORD.

Without both settings the dashboard does not exist (404), so a deployment that forgot
them never serves an open dashboard. Use it behind HTTPS (the tunnel or a proxy), since
Basic credentials are only encoded, not encrypted.
"""

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_basic = HTTPBasic(auto_error=False, realm="issue-sync dashboard")


def _equal(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def require_dashboard_user(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str:
    settings = request.app.state.settings
    if not settings.dashboard_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if credentials is not None:
        # Both compared every time: the response time must not reveal which one failed.
        user_ok = _equal(credentials.username, settings.dashboard_username)
        password_ok = _equal(credentials.password, settings.dashboard_password.get_secret_value())
        if user_ok and password_ok:
            return credentials.username
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "sign-in required",
        headers={"WWW-Authenticate": 'Basic realm="issue-sync dashboard", charset="UTF-8"'},
    )
