from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["openai", "replicate"]
    enabled: bool = False
    kinds: list[Literal["concepts", "storyboard", "image", "video", "tts"]]
    key_env: str = Field(pattern=r"^MJ_[A-Z0-9_]{1,80}$")
    max_cost_micros: int | dict[str, int] = 0
    models: dict[str, str] = Field(default_factory=dict)
    max_tokens: int = Field(default=8192, ge=100, le=16384)
    image_size: Literal["1024x1024", "1024x1536", "1536x1024", "auto"] = "1024x1536"
    version: str = ""
    durations: list[int] = Field(default_factory=list, max_length=20)
    input_template: dict = Field(default_factory=dict)

ROOT = Path(__file__).resolve().parent.parent

@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MJ_DATA_DIR", "./data")).resolve())
    database_url: str = field(default_factory=lambda: os.getenv("MJ_DATABASE_URL", ""))
    dev_mode: bool = field(default_factory=lambda: os.getenv("MJ_DEV_MODE", "0") == "1")
    paid_enabled: bool = field(default_factory=lambda: os.getenv("MJ_PAID_ENABLED", "0") == "1")
    secure_cookie: bool = field(default_factory=lambda: os.getenv("MJ_SECURE_COOKIE", "0") == "1")
    max_upload_bytes: int = 100 * 1024 * 1024
    session_seconds: int = 43200
    lease_seconds: int = 90
    providers_file: Path = field(default_factory=lambda: Path(os.getenv("MJ_PROVIDERS_FILE", "config/providers.json")))

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.database_url:
            if not self.dev_mode:
                raise ValueError("Set MJ_DATABASE_URL to PostgreSQL, or explicitly enable MJ_DEV_MODE=1 for local use.")
            self.database_url = f"sqlite:///{self.data_dir / 'mj.sqlite'}"
        if self.database_url.startswith("sqlite") and not self.dev_mode:
            raise ValueError("SQLite is local/test-only; production requires PostgreSQL.")

    def providers(self) -> dict:
        data = json.loads(self.providers_file.read_text()) if self.providers_file.exists() else {}
        data = {name: ProviderConfig.model_validate(cfg).model_dump() for name, cfg in data.items()
                if name != "mock"}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or name == "local" for name in data):
            raise ValueError("供应商名称无效。")
        data["mock"] = {"type": "mock", "enabled": True, "max_cost_micros": 0,
                        "kinds": ["concepts", "storyboard", "image", "video", "tts"]}
        return data
