from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    property_base_url: AnyHttpUrl
    property_username: str = Field(min_length=1)
    property_password: SecretStr
    property_room_name: str = Field(
        default="麒麟科创园-7号楼-805",
        min_length=1,
    )
    property_device_name: str = Field(
        default="7号楼/8F/805电表",
        min_length=1,
    )
    database_path: Path
    session_secret: SecretStr = Field(min_length=32)
    session_max_age_seconds: int = Field(default=1800, ge=300, le=3600)
    openid_hmac_key: SecretStr = Field(min_length=32)
    wechat_app_id: str = Field(pattern=r"^wx[0-9A-Za-z]{16}$")
    wechat_app_secret: SecretStr = Field(min_length=16)
    wechat_message_token: SecretStr = Field(min_length=16)
    public_base_url: AnyHttpUrl
    poll_minutes: int = Field(default=30, ge=30, le=30)
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    stale_after_minutes: int = Field(default=90, ge=90, le=90)

    @field_validator("property_base_url", "public_base_url")
    @classmethod
    def require_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("HTTPS is required")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
