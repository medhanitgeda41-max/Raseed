"""Application settings and environment configuration."""
from pathlib import Path
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM execution mode
    llm_mode: Literal["demo", "local"] = "demo"

    # API Keys
    groq_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None

    # Local Ollama URL
    ollama_base_url: str = "http://localhost:11434/v1"

    # Governance & Approval
    app_password: Optional[str] = None
    approval_threshold_aed: float = 10000.0

    # Local Storage Paths
    database_path: Path = Path("data/raseed.db")
    upload_dir: Path = Path("data/uploads")

    # Cloud Storage & Database (Cloudflare R2 + Neon Postgres)
    storage_backend: Literal["local", "r2"] = "local"
    r2_endpoint: Optional[str] = None
    r2_access_key_id: Optional[str] = None
    r2_secret_access_key: Optional[str] = None
    r2_bucket: Optional[str] = None
    database_url: Optional[str] = None
    signup_code: Optional[str] = None

    # Timeouts & safety caps
    request_timeout_seconds: int = 30
    max_llm_retries: int = 1
    max_steps_per_turn: int = 5
    max_replans: int = 2


settings = Settings()
