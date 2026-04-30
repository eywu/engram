from __future__ import annotations

from typing import cast

from engram._slack_types import SlackInteractivePayload
from engram.ingress import (
    _slack_payload_channel_id,
    _slack_payload_user_id,
    _slack_payload_view,
)


def _payload(data: dict) -> SlackInteractivePayload:
    return cast(SlackInteractivePayload, data)


def test_slack_payload_user_id_missing_user_returns_none() -> None:
    assert _slack_payload_user_id(_payload({"type": "block_actions"})) is None


def test_slack_payload_user_id_empty_user_returns_none() -> None:
    assert (
        _slack_payload_user_id(_payload({"type": "block_actions", "user": {}}))
        is None
    )


def test_slack_payload_user_id_empty_id_returns_none() -> None:
    assert (
        _slack_payload_user_id(
            _payload({"type": "block_actions", "user": {"id": ""}})
        )
        is None
    )


def test_slack_payload_user_id_valid_payload_returns_user_id() -> None:
    assert (
        _slack_payload_user_id(
            _payload({"type": "block_actions", "user": {"id": "U07TEST123"}})
        )
        == "U07TEST123"
    )


def test_slack_payload_channel_id_missing_channel_returns_empty_string() -> None:
    assert (
        _slack_payload_channel_id(
            _payload({"type": "block_actions", "user": {"id": "U07TEST123"}})
        )
        == ""
    )


def test_slack_payload_channel_id_empty_channel_returns_empty_string() -> None:
    assert (
        _slack_payload_channel_id(
            _payload(
                {
                    "type": "block_actions",
                    "user": {"id": "U07TEST123"},
                    "channel": {},
                }
            )
        )
        == ""
    )


def test_slack_payload_channel_id_empty_id_returns_empty_string() -> None:
    assert (
        _slack_payload_channel_id(
            _payload(
                {
                    "type": "block_actions",
                    "user": {"id": "U07TEST123"},
                    "channel": {"id": ""},
                }
            )
        )
        == ""
    )


def test_slack_payload_channel_id_valid_payload_returns_channel_id() -> None:
    assert (
        _slack_payload_channel_id(
            _payload(
                {
                    "type": "block_actions",
                    "user": {"id": "U07TEST123"},
                    "channel": {"id": "C07TEST123"},
                }
            )
        )
        == "C07TEST123"
    )


def test_slack_payload_view_missing_view_returns_empty_dict() -> None:
    assert (
        _slack_payload_view(
            _payload({"type": "view_submission", "user": {"id": "U07TEST123"}})
        )
        == {}
    )


def test_slack_payload_view_valid_payload_returns_view() -> None:
    view = {"id": "V07TEST123", "callback_id": "test_callback"}
    assert (
        _slack_payload_view(
            _payload(
                {
                    "type": "view_submission",
                    "user": {"id": "U07TEST123"},
                    "view": view,
                }
            )
        )
        == view
    )
