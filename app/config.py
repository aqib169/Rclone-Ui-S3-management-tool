from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

APP_VERSION = "2.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Rclone S3 Management UI"
    data_dir: Path = Path("/app/data")
    rclone_bin: str = "rclone"
    username: str = "admin"
    password: str

    # subprocess + polling tuning
    poll_interval: int = 3
    busy_timeout_ms: int = 5000

    # retention for date-stamped archives (days to keep)
    retain_days: int = 14

    # ops: self-backup of the orchestrator's own data dir, e.g. "arc:backup/orchestrator"
    self_backup_dest: str = ""
    log_retain_days: int = 30

    # rclone transfer tuning (spec §5)
    rclone_transfers: int = 4
    stats_interval: int = 10
    # Compare files by checksum instead of size+modtime (slower, safer).
    rclone_checksum: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orchestrator.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".instance.lock"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def sqlite_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def templates_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "app" / "static"


settings = Settings()
