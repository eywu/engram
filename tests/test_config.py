"""Config loading tests — no network, no secrets."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engram.config import EngramConfig, HITLConfig, NightlyConfig, load_nightly_config


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all env vars that config.load() reads."""
    for key in (
        "ENGRAM_SLACK_BOT_TOKEN",
        "ENGRAM_SLACK_APP_TOKEN",
        "ENGRAM_SLACK_SIGNING_SECRET",
        "ENGRAM_ANTHROPIC_API_KEY",
        "ENGRAM_MODEL",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(content))
    return p


def test_hitl_config_defaults():
    cfg = HITLConfig()

    assert cfg.enabled is True
    assert cfg.timeout_s == 300
    assert cfg.max_per_day == 1000


def test_nightly_config_defaults():
    cfg = NightlyConfig()

    assert cfg.dedup_overlap == 0.85
    assert cfg.min_evidence == 10
    assert cfg.max_tokens_per_channel == 100_000
    assert cfg.excluded_channels == ()
    assert cfg.model is None
    assert cfg.daily_cost_cap_usd == 10.0
    assert cfg.report.suppress is False


def test_load_from_yaml(tmp_path, clean_env):
    path = _write_yaml(
        tmp_path,
        {
            "slack": {
                "bot_token": "xoxb-file",
                "app_token": "xapp-file",
                "team_id": "T02G507JU",
                "team_name": "Growth Gauge",
                "workspace_url": "https://growthgauge.slack.com/",
            },
            "anthropic": {"api_key": "sk-ant-file", "model": "claude-sonnet-4-6"},
            "allowed_channels": ["C123"],
            "max_turns_per_message": 5,
            "hitl": {"enabled": True, "timeout_s": 120, "max_per_day": 2},
            "nightly": {
                "dedup_overlap": 0.9,
                "min_evidence": 4,
                "max_tokens_per_channel": 5000,
                "excluded_channels": ["C07SKIP", "C07SKIP", "C07OTHER"],
                "model": "sonnet",
                "daily_cost_cap_usd": 12.50,
                "report": {"suppress": True},
            },
        },
    )
    cfg = EngramConfig.load(path)
    assert cfg.slack.bot_token == "xoxb-file"
    assert cfg.slack.app_token == "xapp-file"
    assert cfg.slack.team_id == "T02G507JU"
    assert cfg.slack.team_name == "Growth Gauge"
    assert cfg.slack.workspace_url == "https://growthgauge.slack.com/"
    assert cfg.anthropic.api_key == "sk-ant-file"
    assert cfg.anthropic.model == "claude-sonnet-4-6"
    assert cfg.allowed_channels == ["C123"]
    assert cfg.max_turns_per_message == 5
    assert cfg.hitl.timeout_s == 120
    assert cfg.hitl.max_per_day == 2
    assert cfg.nightly.dedup_overlap == 0.9
    assert cfg.nightly.min_evidence == 4
    assert cfg.nightly.max_tokens_per_channel == 5000
    assert cfg.nightly.excluded_channels == ("C07SKIP", "C07OTHER")
    assert cfg.nightly.model == "sonnet"
    assert cfg.nightly.daily_cost_cap_usd == 12.5
    assert cfg.nightly.report.suppress is True


def test_load_nightly_config_does_not_require_runtime_secrets(tmp_path, clean_env):
    path = _write_yaml(
        tmp_path,
        {
            "nightly": {
                "dedup_overlap": 0.75,
                "min_evidence": 2,
                "max_tokens_per_channel": 12,
                "excluded_channels": ["C07SKIP"],
                "model": "opus",
                "daily_cost_cap_usd": 3,
                "report": {"suppress": True},
            },
        },
    )

    cfg = load_nightly_config(path)

    assert cfg.dedup_overlap == 0.75
    assert cfg.min_evidence == 2
    assert cfg.max_tokens_per_channel == 12
    assert cfg.excluded_channels == ("C07SKIP",)
    assert cfg.model == "opus"
    assert cfg.daily_cost_cap_usd == 3.0
    assert cfg.report.suppress is True


def test_env_fallback(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    path = tmp_path / "empty.yaml"  # doesn't exist
    cfg = EngramConfig.load(path)
    assert cfg.slack.bot_token == "xoxb-env"
    assert cfg.slack.app_token == "xapp-env"
    assert cfg.anthropic.api_key == "sk-ant-env"


def test_missing_required_raises(tmp_path, clean_env):
    path = tmp_path / "empty.yaml"
    with pytest.raises(RuntimeError, match="Missing required"):
        EngramConfig.load(path)


def test_yaml_wins_over_env(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb-file", "app_token": "xapp-file"},
            "anthropic": {"api_key": "sk-ant-file"},
        },
    )
    cfg = EngramConfig.load(path)
    assert cfg.slack.bot_token == "xoxb-file"
    assert cfg.anthropic.api_key == "sk-ant-file"


def test_budget_config_loaded_from_yaml(tmp_path, clean_env):
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb-file", "app_token": "xapp-file"},
            "anthropic": {"api_key": "sk-ant-file"},
            "budget": {
                "monthly_cap_usd": 750.25,
                "hard_cap_enabled": True,
                "warn_thresholds": [0.5, 0.9],
                "timezone": "UTC",
            },
        },
    )

    cfg = EngramConfig.load(path)

    assert str(cfg.budget.monthly_cap_usd) == "750.25"
    assert cfg.budget.hard_cap_enabled is True
    assert [str(t) for t in cfg.budget.warn_thresholds] == ["0.5", "0.9"]
    assert cfg.budget.timezone == "UTC"


def test_embeddings_config_loaded_from_yaml_and_env(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env")
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb-file", "app_token": "xapp-file"},
            "anthropic": {"api_key": "sk-ant-file"},
            "embeddings": {
                "enabled": True,
                "provider": "gemini",
                "model": "text-embedding-004",
                "dimensions": 768,
                "sample_rate_transcripts": 0.25,
                "min_transcript_tokens": 12,
                "api_timeout_s": 1.5,
            },
        },
    )

    cfg = EngramConfig.load(path)

    assert cfg.embeddings.enabled is True
    assert cfg.embeddings.api_key == "gemini-env"
    assert cfg.embeddings.sample_rate_transcripts == 0.25
    assert cfg.embeddings.min_transcript_tokens == 12
    assert cfg.embeddings.api_timeout_s == 1.5


def test_embeddings_api_key_prefers_yaml_over_env(tmp_path, clean_env, monkeypatch):
    # The setup wizard writes embeddings.api_key to config.yaml when the user
    # supplies one interactively. That YAML value must win over a stale env
    # var (common case: user exports GEMINI_API_KEY in shell, wizard stores
    # a different key in config, subsequent runs should use the config value).
    # Stub _load_env_files so a developer's personal dotenv (pointed at by
    # ENGRAM_ENV_FILE, ./.env, or ~/.engram/.env) doesn't clobber the setenv.
    monkeypatch.setattr("engram.config._load_env_files", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-stale")
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb", "app_token": "xapp"},
            "anthropic": {"api_key": "sk-ant"},
            "embeddings": {"enabled": True, "api_key": "gemini-from-yaml"},
        },
    )

    cfg = EngramConfig.load(path)

    assert cfg.embeddings.api_key == "gemini-from-yaml"


def test_engram_env_file_override_loads_from_custom_path(
    tmp_path, clean_env, monkeypatch
):
    # Users with personal secret-management conventions (e.g. a dedicated
    # ~/code/_secret/.env directory) can point Engram at it via
    # ENGRAM_ENV_FILE without patching source. Verify the override wins and
    # is applied before ./.env / ~/.engram/.env.
    custom_env = tmp_path / "mysecrets.env"
    custom_env.write_text("GEMINI_API_KEY=from-custom-env-file\n")
    monkeypatch.setenv("ENGRAM_ENV_FILE", str(custom_env))
    # clean_env already stripped GEMINI_API_KEY from the process env; the
    # only way the key can come back is via _load_env_files reading the
    # custom file we just pointed at.
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb", "app_token": "xapp"},
            "anthropic": {"api_key": "sk-ant"},
        },
    )

    cfg = EngramConfig.load(path)

    assert cfg.embeddings.api_key == "from-custom-env-file"


def test_embeddings_api_key_missing_when_neither_source_set(
    tmp_path, clean_env, monkeypatch
):
    # User skipped the Gemini step in the wizard and has no env var.
    # Config should load fine with api_key=None; embeddings.py will log
    # "embeddings.disabled reason=missing_api_key" and keyword-only FTS5
    # memory keeps working.
    #
    # NOTE: EngramConfig.load() calls _load_env_files() which reads any of
    # $ENGRAM_ENV_FILE, ./.env, or ~/.engram/.env. On a developer box those
    # files almost certainly contain GEMINI_API_KEY, which would silently
    # defeat this test. Stub it out so we exercise the real "nothing set
    # anywhere" path.
    monkeypatch.setattr("engram.config._load_env_files", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = _write_yaml(
        tmp_path,
        {
            "slack": {"bot_token": "xoxb", "app_token": "xapp"},
            "anthropic": {"api_key": "sk-ant"},
        },
    )

    cfg = EngramConfig.load(path)

    assert cfg.embeddings.enabled is True  # default
    assert cfg.embeddings.api_key is None
