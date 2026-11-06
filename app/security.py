import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """If API_KEY is set in .env, every data endpoint needs the header X-API-Key."""
    expected = get_settings().api_key
    if expected and not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or wrong X-API-Key")
