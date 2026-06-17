from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


ADMIN_ALLOWED_IP = os.getenv("ADMIN_ALLOWED_IP", "80.64.26.253")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
DISABLE_IP_CHECK = os.getenv("DISABLE_ADMIN_IP_CHECK", "0") == "1"

basic_auth = HTTPBasic()


def _client_ip(request: Request) -> str:
    # Try to respect X-Forwarded-For if present
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        # could be comma-separated list
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def admin_guard(request: Request, creds: HTTPBasicCredentials = Depends(basic_auth)) -> None:
    ip = _client_ip(request)
    if not DISABLE_IP_CHECK and ip != ADMIN_ALLOWED_IP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Admin IP not allowed")

    if not (creds.username == ADMIN_USER and creds.password == ADMIN_PASS):
        # Ask for auth again
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
