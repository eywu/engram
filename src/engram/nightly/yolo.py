"""Nightly YOLO expiry sweep."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from slack_sdk.web.async_client import AsyncWebClient

from engram import paths
from engram.config import DEFAULT_CONFIG_PATH, EngramConfig
from engram.egress import post_yolo_expired_notification
from engram.manifest import ManifestError, YoloDemotion, persist_yolo_demotion

log = logging.getLogger(__name__)


async def sweep_expired_yolo(
    *,
    home: Path | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    now: datetime | None = None,
    slack_client=None,
    owner_dm_channel_id: str | None = None,
) -> list[YoloDemotion]:
    root = paths.engram_home(home)
    current_time = now or datetime.now(UTC)
    results: list[YoloDemotion] = []
    client = slack_client
    dm_channel_id = owner_dm_channel_id

    for manifest_path in _iter_manifest_paths(root):
        try:
            demotion = persist_yolo_demotion(
                manifest_path,
                now=current_time,
                trigger="sweep",
            )
        except ManifestError:
            log.warning(
                "nightly.yolo_manifest_load_failed path=%s",
                manifest_path,
                exc_info=True,
            )
            continue

        if demotion is None:
            continue
        results.append(demotion)

        if dm_channel_id is None:
            dm_channel_id, client = _configured_dm_target(
                config_path=config_path,
                slack_client=client,
            )
        if dm_channel_id is None or client is None:
            log.warning(
                "nightly.yolo_expired_notification_dropped channel=%s reason=no_owner_dm",
                demotion.channel_id,
            )
            continue

        try:
            await post_yolo_expired_notification(
                client,
                owner_dm_channel_id=dm_channel_id,
                channel_id=demotion.channel_id,
                channel_label=demotion.manifest.label,
                pre_yolo_tier=demotion.pre_yolo_tier,
                duration_used=demotion.duration_used,
            )
        except Exception:
            log.warning(
                "nightly.yolo_expired_notification_failed channel=%s",
                demotion.channel_id,
                exc_info=True,
            )

    return results


def _iter_manifest_paths(home: Path) -> Sequence[Path]:
    contexts = paths.contexts_dir(home)
    if not contexts.exists():
        return []
    return sorted(contexts.glob("*/.claude/channel-manifest.yaml"))


def _configured_dm_target(
    *,
    config_path: Path,
    slack_client,
) -> tuple[str | None, object | None]:
    try:
        config = EngramConfig.load(config_path)
    except RuntimeError:
        log.warning(
            "nightly.yolo_config_load_failed path=%s",
            config_path,
            exc_info=True,
        )
        return None, slack_client

    if not config.owner_dm_channel_id:
        return None, slack_client
    client = slack_client or AsyncWebClient(token=config.slack.bot_token)
    return config.owner_dm_channel_id, client
