"""Gateway runtime settings — loaded from environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_secret: str = Field(default="dev-secret-change-me")
    app_data_dir: Path = Path("./data")
    app_db_url: str = "sqlite:///./qbacktest.db"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    log_level: str = "INFO"
    log_format: str = "text"

    # Broker config (optional)
    upstox_client_id: str = ""
    upstox_client_secret: str = ""
    upstox_redirect_uri: str = "http://127.0.0.1:8000/auth/upstox/callback"
    # Headless login (Playwright + TOTP). Requires TOTP-based 2FA enabled on Upstox.
    upstox_mobile: str = ""
    upstox_pin: str = ""
    upstox_totp_secret: str = ""

    fyers_client_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = "http://127.0.0.1:8000/auth/fyers/callback"

    # AngelOne SmartAPI (headless TOTP) — recommended for autonomous use
    angelone_client_code: str = ""
    angelone_password: str = ""
    angelone_api_key: str = ""
    angelone_totp_secret: str = ""

    rfr_source: str = "nse_gsec"


def load_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
