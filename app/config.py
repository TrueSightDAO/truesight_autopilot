"""Configuration for truesight_autopilot."""
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

    # GitHub
    github_pat: str = os.getenv("TRUESIGHT_DAO_AUTOPILOT", "")

    # Gmail
    gmail_token_json: str = os.getenv("GMAIL_TOKEN_JSON", "")

    # LLM
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

    # Edgar
    email: str = os.getenv("EMAIL", "")
    public_key: str = os.getenv("PUBLIC_KEY", "")
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # AWS
    aws_access_key_id: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")

    # Context
    agentic_context_repo: str = os.getenv(
        "AGENTIC_CONTEXT_REPO", "https://github.com/TrueSightDAO/agentic_ai_context.git"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
