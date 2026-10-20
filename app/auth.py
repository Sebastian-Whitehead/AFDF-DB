from fastapi import Header, HTTPException, status

from .config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    settings = get_settings()
    if not settings.valid_api_keys or x_api_key not in settings.valid_api_keys:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return x_api_key
