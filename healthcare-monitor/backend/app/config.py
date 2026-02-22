from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    APP_TITLE: str = "Integration Full-Body Monitor"
    DEBUG: bool = False
    SECRET_KEY: str = "CHANGE-ME-IN-PRODUCTION-USE-256-BIT-RANDOM"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://monitor:monitor@localhost:5432/monitor"

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "https://monitor.hospital.local"]

    # Poller
    DEFAULT_PROBE_INTERVAL_SECONDS: int = 30
    DEFAULT_PROBE_TIMEOUT_SECONDS: int = 5

    # HL7 tracker
    ACK_SWEEP_INTERVAL_SECONDS: int = 15
    DEFAULT_ACK_TIMEOUT_SECONDS: int = 60

    # Alert engine
    ALERT_ENGINE_INTERVAL_SECONDS: int = 30
    CORRELATION_WINDOW_SECONDS: int = 300   # 5-minute lookback for root cause

    # Agent API keys (comma-separated, pre-shared per deployment)
    # In production use a secrets manager; these are read-only push keys
    AGENT_API_KEYS: str = "CHANGE-ME-AGENT-KEY-1,CHANGE-ME-AGENT-KEY-2"

    # Webhooks
    WEBHOOK_URL: str = ""        # POST JSON alert payload here (optional)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    ALERT_EMAIL_TO: str = ""

    # Data retention (days)
    PROBE_RETENTION_DAYS: int = 7
    METRICS_RETENTION_DAYS: int = 14
    HL7_RETENTION_DAYS: int = 30

    @property
    def agent_api_key_set(self) -> set:
        return {k.strip() for k in self.AGENT_API_KEYS.split(",") if k.strip()}


settings = Settings()
