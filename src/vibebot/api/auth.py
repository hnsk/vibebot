"""Token-bearer auth for the HTTP API."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_scheme = HTTPBearer(auto_error=False)
_bearer = Depends(_scheme)


def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer,
) -> str:
    tokens: set[str] = set(request.app.state.api_tokens)
    token: str | None = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        query_token = request.query_params.get("token")
        if query_token:
            token = query_token
    if token is None or token not in tokens:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid API token.")
    return token
