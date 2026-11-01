"""Validated local configuration; credentials never enter serialization or logs."""
from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', env_file_encoding='utf-8', extra='ignore', case_sensitive=False, populate_by_name=True)
    workspace: Path = Field(default=ROOT, validation_alias='DS_WORKSPACE')
    api_key: SecretStr = SecretStr('')
    base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    main_model: str = 'qwen3.8-flash'
    analyst_model: str = 'qwen3.7-plus-2026-05-26'
    report_model: str = 'qwen3.7-plus-2026-05-26'
    main_enable_thinking: bool = False
    analyst_enable_thinking: bool = True
    report_enable_thinking: bool = False
    analyst_thinking_budget: int = 4096
    main_temperature: float | None = 0.1
    analyst_temperature: float | None = 0.1
    report_temperature: float | None = 0.1
    main_max_output_tokens: int = 4096
    analyst_max_output_tokens: int = 8192
    report_max_output_tokens: int = 8192
    model_timeout_seconds: int = Field(default=120, gt=0)
    task_timeout_seconds: int = Field(default=3600, gt=0)
    max_agent_steps: int = Field(default=1024, gt=0)
    max_tool_calls: int = Field(default=256, gt=0)
    # Independent rollout switches; old pickled Settings are read with getattr
    # at runtime so a rolling restart cannot mask the original task failure.
    perf_trace_enabled: bool = True
    auth_dedup_enabled: bool = True
    context_policy: Literal['off', 'shadow', 'enforce'] = 'enforce'
    context_summary_trigger_tokens: int = Field(default=24000, ge=4000)
    context_summary_keep_messages: int = Field(default=6, ge=2, le=40)
    workflow_fast_paths: bool = True
    model_streaming_enabled: bool = True
    sse_stream_enabled: bool = True
    worker_concurrency: int = Field(default=1, ge=1, le=2)
    worker_memory_reserve_mb: int = Field(default=1024, ge=256)
    warm_executor: bool = False
    max_upload_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    max_task_input_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    max_files_per_task: int = Field(default=10, gt=0)
    docker_image: str = 'ds-analysis:local'
    sandbox_timeout_seconds: int = Field(default=120, gt=0)
    sandbox_memory: str = '512m'
    sandbox_cpus: str = '1'
    api_url: str = 'http://127.0.0.1:8000'
    db_enabled: bool = False
    db_host: str = '127.0.0.1'
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = 'agent_conn'
    db_user: str = 'deepagent_user'
    db_password: SecretStr = SecretStr('')
    db_admin_user: str = ''
    db_admin_password: SecretStr = SecretStr('')
    db_restricted_user: str = 'seller_reader_1'
    db_restricted_password: SecretStr = SecretStr('')
    db_connect_timeout_seconds: int = Field(default=5, gt=0)
    db_statement_timeout_seconds: int = Field(default=120, gt=0)
    db_max_result_bytes: int = Field(default=1024**3, gt=0)
    db_max_result_rows: int = Field(default=1000000, gt=0)
    db_fetch_rows: int = Field(default=2000, gt=0, le=10000)
    db_dataset_dir: Path = Path('datasets/olist')

    @field_validator('main_temperature', 'analyst_temperature', 'report_temperature', mode='before')
    @classmethod
    def temperature_off_omits_the_parameter(cls, value):
        """Hosted models such as DashScope kimi-k3 reject any temperature value."""
        if isinstance(value, str) and value.strip().lower() in {'', 'off', 'none'}:
            return None
        return value

    @property
    def root(self) -> Path:
        return self.workspace.resolve()

    def ensure_directories(self) -> None:
        for name in ('input', 'script_output', 'report', 'KB', 'memory/analyst', 'memory/report_designer', 'state/sessions', 'state/tasks', 'state/indexes'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

@lru_cache
def get_settings() -> Settings:
    return Settings()
