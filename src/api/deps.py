"""API dependencies and authentication."""

from fastapi import Header, HTTPException
from typing import Optional
import structlog

from src.config import settings

logger = structlog.get_logger()


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    """
    Verify API key if configured.

    Args:
        x_api_key: API key from request header

    Returns:
        True if authentication successful

    Raises:
        HTTPException: If authentication fails
    """
    # Skip auth if no API key is configured
    if not settings.api_key:
        return True

    if not x_api_key or x_api_key != settings.api_key:
        logger.warning("authentication_failed", provided_key=x_api_key[:8] if x_api_key else None)
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    return True
