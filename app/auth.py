import hashlib
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from .config import get_settings


@dataclass(frozen=True)
class AuthenticatedApiKey:
    name: str
    fingerprint: str


def validate_api_key(x_api_key: str | None) -> str:
    settings = get_settings()
    if not settings.valid_api_keys or x_api_key not in settings.valid_api_keys:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return x_api_key


def api_key_fingerprint(api_key: str) -> str:
    return f"sha256:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def identify_api_key(api_key: str) -> AuthenticatedApiKey:
    settings = get_settings()
    return AuthenticatedApiKey(
        name=settings.api_clients_by_key[api_key],
        fingerprint=api_key_fingerprint(api_key),
    )


def require_read_api_key(x_api_key: str | None = Header(default=None)) -> AuthenticatedApiKey:
    return identify_api_key(validate_api_key(x_api_key))


def require_write_api_key(
    x_api_key: str | None = Header(default=None),
) -> AuthenticatedApiKey:
    key = validate_api_key(x_api_key)
    if get_settings().api_clients_by_key[key] in get_settings().read_only_clients:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have write permission",
        )
    return identify_api_key(key)


def require_admin_api_key(
    x_api_key: str | None = Header(default=None),
) -> AuthenticatedApiKey:
    key = validate_api_key(x_api_key)
    if get_settings().api_clients_by_key[key] not in get_settings().admin_clients:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have account administration permission",
        )
    return identify_api_key(key)
