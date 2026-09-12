"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings; secrets are intentionally excluded from serializable artifacts."""

    model_config = SettingsConfigDict(env_prefix="COMPUTER_USE_", env_file=".env")

    environment: str = "development"
    log_level: str = "INFO"
    headless: bool = False
    artifact_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")
    allowed_origins: list[HttpUrl] = [HttpUrl("http://127.0.0.1:8001")]
    llm_model: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
