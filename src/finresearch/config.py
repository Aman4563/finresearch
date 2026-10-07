"""Settings. Values come from environment variables prefixed FINRESEARCH_ or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from finresearch.bridge.types import ModelClass, Tier

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINRESEARCH_", env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- tier order / enablement
    tier_order: list[Tier] = Field(default=[Tier.CLAUDE_MAX, Tier.CLAUDE_API, Tier.LOCAL])
    claude_api_enabled: bool = False  # no API key yet; flip to true when ANTHROPIC key is added
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # ---- Claude Code CLI
    claude_bin: str = "claude"
    claude_models: dict[ModelClass, str] = Field(
        default={ModelClass.DEEP: "opus", ModelClass.STANDARD: "sonnet", ModelClass.FAST: "haiku"}
    )
    # Pre-flight guard: skip the Max tier when the 5-hour window is above this utilisation
    max_five_hour_ceiling: float = 0.92
    max_seven_day_ceiling: float = 0.97
    # Circuit breaker for transient errors
    breaker_failures: int = 3
    breaker_cooldown_s: int = 600
    # Run workers hold a macOS `caffeinate -is -w <pid>` assertion so an idle Mac does not sleep mid-run
    # (FINRESEARCH_KEEP_AWAKE=false turns it off; no effect on other systems or without caffeinate)
    keep_awake: bool = True
    # The API's monitor restarts a run paused by transient errors whose worker has exited, once resume_after passes
    auto_resume_transient: bool = True

    # ---- API tier guardrails (only used when claude_api_enabled)
    api_budget_per_task_usd: float = 5.0

    # ---- Local tier (Ollama)
    ollama_url: str = "http://127.0.0.1:11434"
    local_models: dict[ModelClass, str] = Field(
        default={
            ModelClass.DEEP: "qwen3.5:9b",
            ModelClass.STANDARD: "qwen3.5:9b",
            ModelClass.FAST: "qwen3.5:4b",
        }
    )
    local_low_memory_model: str = "qwen3.5:4b"  # used when free RAM is too low for the 9B model
    local_min_free_gb_for_9b: float = 7.0  # qwen3.5:9b needs ~6.6 GB at 32k ctx
    local_embed_model: str = "qwen3-embedding:0.6b"
    local_ocr_model: str = "glm-ocr"
    local_num_ctx: int = 32768
    local_keep_alive: str = "10m"
    local_timeout_s: float = 900
    local_schema_repair_attempts: int = 2
    local_max_prompt_chars: int = 90_000  # ~24k tokens; beyond this the local tier refuses (LONG_CONTEXT)

    # ---- F&O risk notice (finresearch.signals.fno.RISK_NOTICE); set when SEBI publishes a newer study
    fno_risk_notice: str | None = None

    # ---- storage
    database_url: str = "postgresql+psycopg://localhost/finresearch"
    test_database_url: str = "postgresql+psycopg://localhost/finresearch_test"

    # ---- secrets (finresearch.secrets): auto = the macOS Keychain, except a *_test database or off macOS (a 0600
    # file under state_dir); "memory" for tests
    secrets_backend: str = "auto"
    # the local API token (finresearch.api.auth). Normally bootstrapped by `finresearch serve` into the Keychain and
    # state_dir/api_token; this setting overrides both (tests)
    api_token: SecretStr | None = None

    # ---- paths
    state_dir: Path = REPO_ROOT / "data" / "state"
    runs_dir: Path = REPO_ROOT / "data" / "runs"
    docs_dir: Path = REPO_ROOT / "data" / "docs"
    reports_dir: Path = (
        REPO_ROOT / "data" / "reports"
    )  # rendered research packs (<company>/run-<id>/...)  # raw/<sha[:2]>/<sha>.pdf and derived/<sha>/...
    # personal portfolio imports (CAS PDFs as uploaded, still password-protected; broker CSVs). Gitignored like all
    # of data/; never sent to an LLM.
    portfolio_dir: Path = REPO_ROOT / "data" / "portfolio"


@lru_cache
def get_settings() -> Settings:
    return Settings()
