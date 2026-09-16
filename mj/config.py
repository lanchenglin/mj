from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = "sqlite:///./data/mj-demo.db"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    mode: str = "demo"
    admin_password: str = ""
    secure_cookie: bool = False
    external_enabled: bool = False
    comfyui_enabled: bool = False
    provider_file: str = ""
    public_origin: str = ""
    session_seconds: int = 28800
    max_upload_bytes: int = 200 * 1024 * 1024
    render_timeout: int = 1800
    lease_seconds: int = 90

    def validate(self):
        self.data_dir = self.data_dir.resolve()
        if self.mode not in ("demo", "production", "test"):
            raise ValueError("MJ_MODE must be demo, production or test")
        if self.mode == "production" and not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("Production requires PostgreSQL; SQLite is offline demo/test only")
        if self.mode != "production" and self.external_enabled:
            raise ValueError("External services are disabled in demo/test mode")
        if self.mode != "production" and self.comfyui_enabled:
            raise ValueError("ComfyUI remote processing requires production mode; demo/test remains offline")
        if len(self.admin_password) < 14:
            raise ValueError("MJ_ADMIN_PASSWORD must contain at least 14 characters; run python -m mj.cli init")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def env(cls):
        return cls(
            database_url=os.getenv("MJ_DATABASE_URL", "sqlite:///./data/mj-demo.db"),
            data_dir=Path(os.getenv("MJ_DATA_DIR", "data")),
            mode=os.getenv("MJ_MODE", "demo"),
            admin_password=os.getenv("MJ_ADMIN_PASSWORD", ""),
            secure_cookie=os.getenv("MJ_SECURE_COOKIE", "false").lower() == "true",
            external_enabled=os.getenv("MJ_EXTERNAL_ENABLED", "false").lower() == "true",
            provider_file=os.getenv("MJ_PROVIDERS_FILE", ""),
            comfyui_enabled=os.getenv("MJ_COMFYUI_ENABLED", "false").lower() == "true",
            public_origin=os.getenv("MJ_PUBLIC_ORIGIN", "").rstrip("/"),
        ).validate()

    def providers(self):
        if not self.provider_file:
            return {}
        data = json.loads(Path(self.provider_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Providers must be a JSON object keyed by provider ID")
        # Administrator-managed file; credentials are environment references only.
        for name, cfg in data.items():
            if cfg.get("type") not in ("openai_compatible", "fal_queue", "comfyui"):
                raise ValueError(f"Unsupported provider type: {name}")
            if "api_key" in cfg:
                raise ValueError("Never put raw API keys in providers.json; use key_env")
            if cfg["type"] == "comfyui":
                from .comfy_workflows import load_config
                data[name] = load_config(cfg, Path(self.provider_file).resolve().parent)
        return data
