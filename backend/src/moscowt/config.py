from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOSCOWT_")
    state_dir: Path = Path("../artifacts")
    dataset_dir: Path = Path("../dataset")
    frontend_dir: Path = Path("../frontend/dist")
    worker_enabled: bool = True
    log_requests: bool = False
    request_threads: int = Field(default=4, ge=1, le=32)
    queue_limit: int = 32
    cache_entries: int = 64
    cache_bytes: int = 64 * 1024 * 1024
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
