"""Configuration for truesight_autopilot (merged governor chat + autopilot)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")
    # Service
    port: int = Field(default=8001, validation_alias="PORT")
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    dry_run: bool = Field(default=False, validation_alias="DRY_RUN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    max_pr_per_day: int = Field(default=5, validation_alias="MAX_PR_PER_DAY")

    # CORS
    cors_origins: list[str] = ["*"]  # TODO: restrict to dapp.truesight.me in production

    # Security
    jwt_secret: str = Field(default="change-me-in-production", validation_alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 30
    nonce_ttl_seconds: int = 300
    timestamp_skew_seconds: int = 600
    disable_governor_check: bool = Field(default=False, validation_alias="DISABLE_GOVERNOR_CHECK")

    # GitHub
    github_pat: str = Field(default="", validation_alias="TRUESIGHT_DAO_AUTOPILOT")

    # Allowed repos for code modifications
    allowed_repos: list[str] = [
        "dapp", "tokenomics", "truesight_me", "truesight_me_prod",
        "agroverse_shop", "agroverse_shop_prod", "dao_client",
        "market_research", "sentiment_importer", "truesight_autopilot",
        "agentic_ai_context",
    ]

    # Gmail
    gmail_token_json: str = os.getenv("GMAIL_TOKEN_JSON", "")

    # LLM — DeepSeek only (dropped Kimi + Claude for cost)
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("DEEPSEEK_SDK", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_max_tokens: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "16384"))
    deepseek_temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.3"))

    # Grok (xAI) — vision analysis for uploaded images
    grok_api_key: str = os.getenv("GROK_API_KEY", "")

    # Gemini (Google) — vision analysis fallback for uploaded images
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")

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

    # SSH / Deploy
    ec2_host: str = os.getenv("EC2_HOST", "truesight-autopilot")
    ec2_key_path: str = os.getenv("EC2_KEY_PATH", os.path.expanduser("~/.ssh/agentic_ai_github/id_ed25519"))
    ec2_remote_dir: str = os.getenv("EC2_REMOTE_DIR", "/opt/truesight_autopilot")

    # Session logging (production: use persistent path, not /tmp)
    session_log_dir: Path = Path(os.getenv("SESSION_LOG_DIR", "/tmp/autopilot_sessions"))


settings = Settings()
