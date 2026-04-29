"""Internal Slack interaction payload shapes used by ingress handlers."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class SlackUser(TypedDict):
    id: str
    name: NotRequired[str]


class SlackChannel(TypedDict):
    id: str
    name: NotRequired[str]


class SlackBlockAction(TypedDict):
    action_id: str
    type: str
    block_id: NotRequired[str]
    value: NotRequired[str]
    # Block Kit option objects vary by element type; ingress only passes them through.
    selected_option: NotRequired[dict[str, Any]]


class SlackView(TypedDict):
    id: NotRequired[str]
    callback_id: NotRequired[str]
    private_metadata: NotRequired[str]
    # Slack view state is keyed by caller-defined block/action ids.
    state: NotRequired[dict[str, Any]]


class SlackInteractivePayload(TypedDict):
    type: Literal[
        "block_actions",
        "block_suggestion",
        "message_action",
        "shortcut",
        "view_closed",
        "view_submission",
    ]
    user: SlackUser
    channel: NotRequired[SlackChannel]
    actions: NotRequired[list[SlackBlockAction]]
    view: NotRequired[SlackView]
    state: NotRequired[dict[str, Any]]
    trigger_id: NotRequired[str]
