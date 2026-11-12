"""Application settings (environment variables / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    data_mode: str = "SIMULATION"                 # SIMULATION | PLANT (FR-018)
    api_prefix: str = "/api/v1"

    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'oilintel.db').as_posix()}"
    # simulator ground truth lives in a separate database that inference code never receives
    truth_database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'sim_truth.db').as_posix()}"

    bus: str = "inproc"                           # inproc | mqtt
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    embedded_broker: bool = False                 # start an amqtt broker in-process (dev only)

    simulator: str = "local"                      # local (in-process) | remote (MQTT simulator service)
    sim_speed: float = 10.0
    sim_autostart: bool = True

    opcua_enabled: bool = False
    opcua_endpoint: str = "opc.tcp://0.0.0.0:4840/oilintelligence/"

    auth_required: bool = True
    # open access (AUTH_REQUIRED=false): no sign-in page, e.g. an internal tool on the company network.
    # Every request acts as this identity, which is what the audit log records.
    open_access_user: str = "control-room"
    open_access_role: str = "admin"
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 720
    seed_admin_password: str | None = None
    seed_engineer_password: str | None = None
    seed_operator_password: str | None = None
    seed_viewer_password: str | None = None

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    rate_limit_per_minute: int = 600

    monitor_interval_s: float = 300.0             # simulated / plant seconds between balance evaluations
    monitor_window_s: float = 1800.0
    alarm_confirmations: int = 2                  # successive abnormal evaluations before an event
    alarm_min_span_s: float | None = None         # ...spanning this much data time (None: instrument error memory)
    # adaptive layer (drift detection -> self-calibration -> retrain gate)
    adaptive_enabled: bool = True
    adaptive_every: int = 6                       # monitoring evaluations per adaptive cycle
    adaptive_state_path: str = str(REPO_ROOT / "data" / "adaptive_state.json")
    demo_history_path: str = str(REPO_ROOT / "data" / "demo_history.json")
    auto_investigate: bool = True
    telemetry_buffer_hours: float = 14.0

    models_dir: str = str(REPO_ROOT / "models")
    plant_config: str | None = None

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
