"""Pydantic models for bot config, policy.yaml, and process settings (env)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pmex_shadow.models import MATCH_ATTRIBUTES


class ConfigError(ValueError):
    """Raised for structurally or semantically invalid bot/policy config."""


def _reject_floats(obj: Any, path: str = "") -> None:
    """Decimal discipline (PRD §3): YAML floats are prohibited for money/price fields.

    A bare YAML float (e.g. `500.5`) round-trips through an imprecise binary
    representation before Decimal ever sees it. Require ints or quoted strings instead.
    """
    if isinstance(obj, float):
        raise ConfigError(
            f"float value at '{path or '<root>'}' is prohibited (§3 decimal discipline) "
            "— quote it as a string or write it as an integer"
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_floats(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")


class WalletConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    funder_env: str
    pk_env: str


@dataclass(frozen=True)
class MatchValue:
    """One rule value: either a set of literals, or a compiled pattern.

    Patterns are matched with `fullmatch`, never `search`. An unanchored `sol` would
    also match `solana-...` and `consolidated-...`, and the failure is silent and in
    the wrong direction — you'd deny markets you meant to keep, or (worse, on an
    allow rule) admit ones you meant to exclude. Requiring `sol.*` costs one
    character and can't surprise anyone.
    """

    literals: frozenset[str] | None = None
    pattern: re.Pattern[str] | None = None

    def matches(self, values: frozenset[str]) -> bool:
        if self.literals is not None:
            return bool(self.literals & values)
        assert self.pattern is not None
        return any(self.pattern.fullmatch(v) for v in values)

    def describe(self) -> str:
        if self.literals is not None:
            return "|".join(sorted(self.literals))
        assert self.pattern is not None
        return f"re:{self.pattern.pattern}"


def _match_value(key: str, raw: Any) -> MatchValue:
    if isinstance(raw, dict):
        if set(raw) != {"re"} or not isinstance(raw["re"], str):
            raise ConfigError(
                f"selector rule '{key}': a mapping value must be exactly {{re: '<pattern>'}}, got {raw!r}"
            )
        try:
            return MatchValue(pattern=re.compile(raw["re"]))
        except re.error as exc:
            # Compiled at load, not at match time: a bad pattern must stop the bot
            # from starting, not surface mid-copy as a crash on some future fill.
            raise ConfigError(f"selector rule '{key}': invalid regex {raw['re']!r} — {exc}") from exc
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    if not values:
        raise ConfigError(f"selector rule '{key}': empty value list matches nothing — remove the key instead")
    literals = set()
    for v in values:
        if not isinstance(v, (str, int)):
            raise ConfigError(f"selector rule '{key}': expected a string, list of strings, or {{re: ...}}, got {v!r}")
        literals.add(str(v).lower())
    return MatchValue(literals=frozenset(literals))


class MatchRule(RootModel[dict[str, MatchValue]]):
    """One deny/allow rule: `{attr: value}` pairs that must ALL match (AND).

    e.g. `{asset: sol}`, or `{tag: crypto-prices, duration: 5m}` for "5-minute crypto
    markets specifically". OR is expressed by writing several rules in the list.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, raw: Any) -> Any:
        if not isinstance(raw, dict) or not raw:
            raise ConfigError(f"selector rule must be a non-empty mapping of attribute to value, got {raw!r}")
        unknown = set(raw) - MATCH_ATTRIBUTES
        if unknown:
            raise ConfigError(
                f"selector rule uses unknown attribute(s) {sorted(unknown)} — "
                f"known attributes are {sorted(MATCH_ATTRIBUTES)}"
            )
        return {key: _match_value(key, value) for key, value in raw.items()}

    def describe(self) -> dict[str, str]:
        """Human-readable form of the rule, for skip_detail."""
        return {key: value.describe() for key, value in self.root.items()}


class SelectorsConfig(BaseModel):
    """Optional filters that compose with AND (FR-P-2). Absent = no constraint."""

    model_config = ConfigDict(extra="forbid")
    categories: list[str] | None = None
    min_book_liquidity_usd: Decimal | None = None
    min_target_notional_usd: Decimal | None = None
    max_time_to_resolution_days: int | None = None

    # Never implemented — decide() has never read this, so any bot that set it got no
    # filtering and no indication of that. Retained (rather than deleted) only because
    # active bot_config rows serialize it explicitly as null, and dropping the field
    # would make those rows fail validation: consumer.py's hot-reload catches that,
    # logs, and silently keeps the previous config, so a bot would quietly stop
    # honouring dashboard edits. Accepting null keeps them loading; rejecting a real
    # value keeps the original fail-open bug from outliving the field. Delete outright
    # once no stored config carries the key.
    event_ids: list[str] | None = None

    # Identity rules over MarketMeta.attrs, evaluated deny-then-allow. These exist
    # because `categories` can't separate markets inside one family (BTC/SOL/XRP 5m
    # all share a category) and `event_ids` can't span one (a 5m series mints a new
    # event every recurrence). See policy/match.py for the deny/allow asymmetry.
    deny: list[MatchRule] | None = None
    allow: list[MatchRule] | None = None

    @model_validator(mode="after")
    def _reject_nonfunctional_event_ids(self) -> "SelectorsConfig":
        if self.event_ids:
            raise ConfigError(
                "selectors.event_ids has never been implemented and silently filtered "
                "nothing — use `allow: [{event_id: [...]}]` instead. Note an event id "
                "identifies one recurrence, so for a repeating series you almost "
                "certainly want `series` or `asset` rather than either."
            )
        return self


class PolicyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope_usd: Decimal


class BotConfig(BaseModel):
    """One bots/<name>.yaml. `name` is the immutable bot_id (FR-O-6, §2.2)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    mode: Literal["watch", "paper", "live"] = "paper"
    wallet: WalletConfig
    selectors: SelectorsConfig = Field(default_factory=SelectorsConfig)
    targets: list[str]
    policy: PolicyRef
    risk: RiskConfig

    @model_validator(mode="after")
    def _name_is_valid_identifier(self) -> "BotConfig":
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(
                f"bot name '{self.name}' must be a simple identifier "
                "(letters, digits, '_', '-') — it becomes the docker service name, "
                "metrics label, and ledger key"
            )
        return self


def load_bot_config(path: Path) -> BotConfig:
    """Load and validate a bot YAML, enforcing name immutability (FR-O-6).

    The filename stem is the bot's identity of record. A YAML whose `name:` field
    doesn't match its own filename is rejected outright — that mismatch is exactly
    the "someone edited name: and forked their own history" failure FR-O-6 exists to
    prevent. (Renaming an *already-deployed* bot, i.e. detecting drift against a bot_id
    that has prior versions in the database, is enforced by the control plane when it
    loads config in Phase 6 — this is the load-time half of the guarantee.)
    """
    raw_text = path.read_text()
    raw = yaml.safe_load(raw_text)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at the top level")
    _reject_floats(raw)

    expected_bot_id = path.stem
    declared_name = raw.get("name")
    if declared_name != expected_bot_id:
        raise ConfigError(
            f"{path}: name '{declared_name}' does not match filename '{expected_bot_id}.yaml' "
            "— bot names are immutable (FR-O-6); rename the file to match, or use an "
            "explicit migrate path rather than editing name: in place"
        )

    return BotConfig.model_validate(raw)


class SizingCurvePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: int = Field(ge=0, le=100)
    mult: Decimal


class SizingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["target_size_percentile", "fixed_usd"] = "target_size_percentile"
    # base_unit_usd/curve only apply to target_size_percentile; fixed_usd only to
    # fixed_usd — both Optional here and cross-checked in the validator below rather
    # than split into two SizingConfig subclasses, so bots/*.yaml and policy.yaml keep
    # one flat "sizing:" block regardless of mode.
    base_unit_usd: Decimal | None = None
    curve: list[SizingCurvePoint] | None = None
    fixed_usd: Decimal | None = None
    min_target_size_percentile: Decimal
    min_order_usd: Decimal
    max_position_usd: Decimal
    max_concurrent_positions: int
    reserve_pct: Decimal

    @model_validator(mode="after")
    def _require_fields_for_mode(self) -> "SizingConfig":
        if self.mode == "target_size_percentile":
            if self.base_unit_usd is None or self.curve is None:
                raise ConfigError("sizing.mode 'target_size_percentile' requires base_unit_usd and curve")
        elif self.mode == "fixed_usd":
            if self.fixed_usd is None:
                raise ConfigError("sizing.mode 'fixed_usd' requires fixed_usd")
        return self


class VolatilityGuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_s: int
    max_ticks: int


class PolicyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_slippage_ticks: int
    volatility_guard: VolatilityGuardConfig
    max_fill_age_s: int
    sizing: SizingConfig


class RiskGlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    global_max_exposure_usd: Decimal
    max_orders_per_minute: int
    halt_on_reconcile_drift_usd: Decimal


class TargetsDecayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_days: int
    min_hit_rate: Decimal
    auto_pause: bool


class TargetsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decay: TargetsDecayConfig
    dormancy_days: int


class ExitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mirror_sells: bool = True
    auto_redeem: bool = True
    redeem_retry_days: int


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: dict[str, PolicyProfile]
    risk: RiskGlobalConfig
    targets: TargetsConfig
    exits: ExitsConfig


def load_policy_file(path: Path) -> PolicyFile:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at the top level")
    _reject_floats(raw)
    return PolicyFile.model_validate(raw)


class Settings(BaseSettings):
    """Process-wide settings, read from the environment (`.env` in compose).

    No blanket env_prefix: a handful of names are load-bearing exactly as spelled in
    the PRD/design doc (`POLYGON_WS_URL`, `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` —
    FR-O-5's live-mode interlock literally string-matches this one) and must not grow
    a `PMEX_` prefix. Everything pmex-shadow-specific is explicitly aliased instead.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field("postgresql://postgres:postgres@db:5432/pmex", validation_alias="PMEX_DATABASE_URL")

    control_bind_host: str = Field("127.0.0.1", validation_alias="PMEX_CONTROL_BIND_HOST")
    control_bind_port: int = Field(8080, validation_alias="PMEX_CONTROL_BIND_PORT")
    control_auth_secret: str | None = Field(None, validation_alias="PMEX_CONTROL_AUTH_SECRET")
    control_allow_writes: bool = Field(False, validation_alias="PMEX_CONTROL_ALLOW_WRITES")

    backfill_chunk_blocks: int = Field(2000, validation_alias="PMEX_BACKFILL_CHUNK_BLOCKS")
    watcher_stale_s: int = Field(30, validation_alias="PMEX_WATCHER_STALE_S")
    deadman_timeout_s: int = Field(30, validation_alias="PMEX_DEADMAN_TIMEOUT_S")

    # Unprefixed — matches PRD §8/design doc §2.1 exactly (ws_url_env: POLYGON_WS_URL).
    polygon_ws_url: str | None = None
    polygon_ws_url_fallback: str | None = None
    polygon_rpc_url: str | None = None

    # watcher.sources.{chain,dataapi} from the design doc §3.1, as env vars rather than
    # a new YAML file — §4's repo layout doesn't define one, and Settings already
    # covers every other watcher knob this way.
    sources_chain_enabled: bool = Field(True, validation_alias="PMEX_SOURCES_CHAIN_ENABLED")
    sources_dataapi_enabled: bool = Field(True, validation_alias="PMEX_SOURCES_DATAAPI_ENABLED")
    dataapi_poll_interval_s: int = Field(30, validation_alias="PMEX_DATAAPI_POLL_INTERVAL_S")

    clob_base_url: str = Field("https://clob.polymarket.com", validation_alias="PMEX_CLOB_BASE_URL")
    data_api_base_url: str = Field("https://data-api.polymarket.com", validation_alias="PMEX_DATA_API_BASE_URL")
    gamma_api_base_url: str = Field("https://gamma-api.polymarket.com", validation_alias="PMEX_GAMMA_API_BASE_URL")

    backup_dir: str = Field("/var/backups/pmex", validation_alias="PMEX_BACKUP_DIR")
    backup_max_age_hours: int = Field(8, validation_alias="PMEX_BACKUP_MAX_AGE_HOURS")

    # Unprefixed — FR-O-5 names this exact variable as one of three live-mode conditions.
    i_understand_this_trades_real_funds: str = ""
