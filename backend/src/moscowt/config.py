from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOSCOWT_")
    state_dir: Path = PROJECT_ROOT / "artifacts"
    dataset_dir: Path = PROJECT_ROOT / "dataset"
    source_dir: Path = PROJECT_ROOT.parent / "dataset"
    data_root: Path | None = None
    frontend_dir: Path = PROJECT_ROOT / "frontend/dist"
    worker_enabled: bool = True
    log_requests: bool = False
    request_threads: int = Field(default=4, ge=1, le=32)
    model_threads: int = Field(default=2, ge=1, le=64)
    max_forecast_hours: int = Field(default=366 * 24, ge=24, le=5 * 366 * 24)
    queue_limit: int = Field(default=32, ge=1)
    cache_entries: int = Field(default=64, ge=1)
    cache_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @model_validator(mode="after")
    def resolve_paths(self):
        for name in ("state_dir", "dataset_dir", "source_dir", "frontend_dir"):
            setattr(self, name, getattr(self, name).expanduser().resolve())
        self.data_root = (self.data_root or self.state_dir.with_name(self.state_dir.name + "-data")).resolve()
        return self

    def input_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Expected a relative input path")
        source = self.source_dir / path
        return source if source.exists() else self.dataset_dir / path
