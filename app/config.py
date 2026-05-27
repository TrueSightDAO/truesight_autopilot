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
        "dapp", "dapp_beta", "tokenomics", "truesight_me", "truesight_me_prod",
        "agroverse_shop", "agroverse_shop_prod", "dao_client",
        "market_research", "go_to_market", "sentiment_importer", "truesight_autopilot",
        "agentic_ai_context", "dao_protocol",
        "capoeira", "program-template", "butterfly-effect-club",
    ]

    # Gmail
    gmail_token_json: str = os.getenv("GMAIL_TOKEN_JSON", "")

    # LLM — DeepSeek only (dropped Kimi + Claude for cost)
    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("DEEPSEEK_SDK", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_max_tokens: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "16384"))
    deepseek_temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.3"))

    # BigModel (ZhipuAI / GLM)
    bigmodel_api_key: str = Field(default="", validation_alias="BIGMODEL_CN_API")
    bigmodel_base_url: str = Field(default="https://open.bigmodel.cn/api/paas/v4", validation_alias="BIGMODEL_BASE_URL")
    bigmodel_model: str = Field(default="glm-4.5", validation_alias="BIGMODEL_MODEL")

    # Grok (xAI) — vision analysis for uploaded images
    grok_api_key: str = os.getenv("GROK_API_KEY", "")

    # Tavily — web search / page extraction for the chat agent
    tavily_api_key: str = Field(default="", validation_alias="TAVILY_API")

    # Telegram — private single-user chat bot in front of /chat-blocking
    telegram_bot_api_key: str = Field(default="", validation_alias="TELEGRAM_BOT_API_KEY")
    # Comma-separated numeric Telegram user IDs allowed to talk to the bot.
    # Empty = bootstrap mode: the bot replies with the sender's own ID so you can pin it.
    telegram_allowed_user_ids: str = Field(default="", validation_alias="TELEGRAM_ALLOWED_USER_IDS")
    # Which governor identity the bot speaks as (resolved to a public key from the registry).
    telegram_governor_name: str = Field(default="Gary Teh", validation_alias="TELEGRAM_GOVERNOR_NAME")
    # Where the FastAPI chat service is reachable from the adapter process.
    autopilot_chat_url: str = Field(default="http://localhost:8001", validation_alias="AUTOPILOT_CHAT_URL")

    # Edgar
    email: str = os.getenv("EMAIL", "")
    public_key: str = os.getenv("PUBLIC_KEY", "")
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # AWS
    aws_access_key_id: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")

    # Bugsnag — autopilot self-reports crashes + ERROR-level logs to Bugsnag.
    # The same Bugsnag project then emits 'New error in autopilot' emails which
    # email_poller's bugsnag_error classifier picks up, closing the
    # self-improvement loop. Disabled when bugsnag_api_key is empty.
    # Env var name BUG_SNAG_API matches the existing autopilot/.env convention.
    bugsnag_api_key: str = os.getenv("BUG_SNAG_API", "") or os.getenv("BUGSNAG_API_KEY", "")
    bugsnag_release_stage: str = os.getenv("BUGSNAG_RELEASE_STAGE", "production")

    # Bugsnag-project-name -> github-repo mapping for the inbound bugsnag_error
    # handler in email_poller.py. JSON dict in env, e.g.:
    #   BUGSNAG_PROJECT_REPOS='{"autopilot": "truesight_autopilot", "Krake Publisher": "krake_local"}'
    # Project name is the bracketed prefix in the Bugsnag email subject
    # (e.g. '[Krake Publisher] HTTPError ...' -> key 'Krake Publisher').
    # Unmapped projects log a warning and the handler returns None
    # (no auto-PR — preserves the v0 stub behavior for projects Gary
    # hasn't yet vouched for autopilot to fix).
    bugsnag_project_repos_raw: str = os.getenv("BUGSNAG_PROJECT_REPOS", "")

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
