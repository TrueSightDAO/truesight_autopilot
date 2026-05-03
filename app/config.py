"""Configuration for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service
    port: int = int(os.getenv("PORT", "8001"))
    host: str = os.getenv("HOST", "0.0.0.0")
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_pr_per_day: int = int(os.getenv("MAX_PR_PER_DAY", "5"))

    # CORS
    cors_origins: list[str] = ["*"]  # TODO: restrict to dapp.truesight.me in production

    # Security
    jwt_secret: str = os.getenv("JWT_SECRET", "change-me-in-production")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 30
    nonce_ttl_seconds: int = 300
    timestamp_skew_seconds: int = 120

    # GitHub
    github_pat: str = os.getenv("TRUESIGHT_DAO_AUTOPILOT", "")

    # Gmail
    gmail_token_json: str = os.getenv("GMAIL_TOKEN_JSON", "")

    # LLM — DeepSeek only (dropped Kimi + Claude for cost)
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_max_tokens: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
    deepseek_temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.3"))

    # Edgar
    email: str = os.getenv("EMAIL", "")
    public_key: str = os.getenv("PUBLIC_KEY", "")
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # AWS
    aws_access_key_id: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")

    # Context
    context_repos_dir: Path = Path(os.getenv("CONTEXT_REPOS_DIR", "/opt/truesight_autopilot/context"))
    agentic_context_repo: str = os.getenv(
        "AGENTIC_CONTEXT_REPO", "https://github.com/TrueSightDAO/agentic_ai_context.git"
    )
    static_governors_json: Path | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
