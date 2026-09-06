"""
ASES - Configuration
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # API
    APP_NAME: str = "ASES Agent Service"
    DEBUG: bool = False

    # OpenAI
    OPENAI_API_KEY: Optional[str] = None

    # GitHub
    GITHUB_TOKEN: Optional[str] = None

    # Vercel
    VERCEL_TOKEN: Optional[str] = None

    # Sandbox
    SANDBOX_BASE_DIR: str = "/tmp/ases-sandboxes"
    SANDBOX_MAX_AGE_MINUTES: int = 10

    # Database (for production)
    DATABASE_URL: Optional[str] = None
    REDIS_URL: Optional[str] = None

    class Config:
        env_file = ".env"


settings = Settings()
