"""Ingress HITL tests for Slack button actions and thread replies."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from engram.config import AnthropicConfig, EngramConfig, SlackConfig
from engram.footguns import match_footgun
from engram.hitl import PendingQuestion
from engram.ingress import (
    CHANNELS_PAGE_ACTION_PATTERN,
    FOOTGUN_CONFIRM_OPEN_ACTION_ID,
    HITL_ACTION_ID_PATTERN,
    NEW_SESSION_ACTION_ID_PATTERN,
    NIGHTLY_TOGGLE_ACTION_PATTERN,
    TIER_PICK_ACTION_PATTERN,
    UPGRADE_ACTION_ID_PATTERN,
    YOLO_DURATION_ACTION_PATTERN,
    YOLO_EXTEND_ACTION_ID_PATTERN,
    YOLO_REVOKE_ACTION_ID_PATTERN,
    handle_block_action,
    handle_engram_command,
    handle_footgun_confirm_closed,
    handle_footgun_confirm_open,
    handle_footgun_confirm_submit,
    handle_meta_eligibility_command,
    handle_thread_reply,
    parse_meta_eligibility_command,
    register_listeners,
)
from engram.manifest import (
    ChannelManifest,
    ChannelStatus,
    IdentityTemplate,
    PermissionTier,
    dump_manifest,
    load_manifest,
)
from engram.notifications import PENDING_CHANNEL_ACTION_ID_PATTERN
from engram.paths import channel_manifest_path
from engram.router import Router


class DecoratorApp:
    def __init__(self) -> None:
        self.actions = []
        self.commands = []
        self.events = []
        self.views = []
        self.view_closed_handlers = []

    def action(self, pattern):
        def decorator(func):
            self.actions.append((pattern, func))
            return func

        return decorator

    def command(self, command_name):
        def decorator(func):
            self.commands.append((command_name, func))
            return func

        return decorator

    def event(self, event_name):
        def decorator(func):
            self.events.append((event_name, func))
            return func

        return decorator

    def view(self, callback_id):
        def decorator(func):
            self.views.append((callback_id, func))
            return func

        return decorator

    def view_closed(self, callback_id):
        def decorator(func):
            self.view_closed_handlers.append((callback_id, func))
            return func

        return decorator


class FakeSlackClient:
    def __init__(self) -> None:
        self.post_calls = []
        self.update_calls = []
        self.ephemeral_calls = []
        self.view_open_calls = []
        self.chat_postMessage = self._chat_post_message

    async def _chat_post_message(self, **kwargs):
        self.post_calls.append(kwargs)
        return {"ok": True, "ts": "1713800000.000200"}

    async def chat_update(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):  # noqa: N802 (Slack SDK method name)
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}

    async def views_open(self, **kwargs):
        self.view_open_calls.append(kwargs)
        return {"ok": True}


def make_question(
    permission_request_id: str = "prq-1",
    *,
    channel_id: str = "C07TEST123",
    suggestions=None,
    who_can_answer: str | None = None,
    tool_input=None,
    footgun_match=None,
) -> PendingQuestion:
    return PendingQuestion(
        permission_request_id=permission_request_id,
        channel_id=channel_id,
        session_id="session-1",
        turn_id="turn-1",
        tool_name="Bash",
        tool_input=dict(tool_input or {"cmd": "pytest", "timeout": 30}),
        suggestions=list(suggestions or []),
        who_can_answer=who_can_answer,
        posted_at=datetime(2026, 4, 22, tzinfo=UTC),
        timeout_s=300,
        slack_channel_ts="1713800000.000100",
        slack_thread_ts="1713800000.000100",
        footgun_match=footgun_match,
    )


def block_action_payload(value: str, *, user_id: str = "U123") -> dict:
    parts = value.split("|")
    if len(parts) > 1 and parts[1] == "always":
        action_id = "hitl_choice_always_0"
    else:
        choice_key = parts[1] if len(parts) > 1 else "0"
        action_id = f"hitl_choice_{choice_key}"
    return {
        "type": "block_actions",
        "actions": [
            {
                "action_id": action_id,
                "block_id": "hitl_actions",
                "value": value,
            }
        ],
        "user": {"id": user_id},
    }


def footgun_open_payload(
    permission_request_id: str = "prq-1",
    *,
    user_id: str = "U123",
    trigger_id: str = "trigger-1",
) -> dict:
    return {
        "type": "block_actions",
        "trigger_id": trigger_id,
        "actions": [
            {
                "action_id": FOOTGUN_CONFIRM_OPEN_ACTION_ID,
                "block_id": "footgun_actions",
                "value": permission_request_id,
            }
        ],
        "user": {"id": user_id},
    }


def footgun_submit_payload(
    permission_request_id: str = "prq-1",
    *,
    typed_value: str,
    user_id: str = "U123",
) -> dict:
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": "footgun_confirm_submit",
            "private_metadata": permission_request_id,
            "state": {
                "values": {
                    "footgun_confirm_input": {
                        "confirmation_text": {"value": typed_value}
                    }
                }
            },
        },
    }


def footgun_closed_payload(
    permission_request_id: str = "prq-1",
    *,
    user_id: str = "U123",
) -> dict:
    return {
        "type": "view_closed",
        "user": {"id": user_id},
        "view": {
            "callback_id": "footgun_confirm_submit",
            "private_metadata": permission_request_id,
        },
    }


def permission_update() -> PermissionUpdate:
    return PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue(tool_name="Bash", rule_content="pytest")],
        behavior="allow",
        destination="session",
    )


def make_config() -> EngramConfig:
    return EngramConfig(
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        anthropic=AnthropicConfig(api_key="sk-ant-test"),
    )


def make_config_with_owner_dm(owner_dm_channel_id: str = "D07OWNER") -> EngramConfig:
    cfg = make_config()
    cfg.owner_dm_channel_id = owner_dm_channel_id
    cfg.owner_user_id = "U07OWNER"
    return cfg


async def wait_until(predicate) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition was not met before timeout")
        await asyncio.sleep(0)


def _write_channel_manifest(
    home,
    channel_id: str,
    *,
    nightly_included: bool = True,
    identity: IdentityTemplate = IdentityTemplate.TASK_ASSISTANT,
    tier: PermissionTier = PermissionTier.TASK_ASSISTANT,
) -> None:
    path = channel_manifest_path(channel_id, home)
    path.parent.mkdir(parents=True)
    dump_manifest(
        ChannelManifest(
            channel_id=channel_id,
            identity=identity,
            status=ChannelStatus.ACTIVE,
            label="#growth",
            permission_tier=tier,
            nightly_included=nightly_included,
        ),
        path,
    )


def test_register_listeners_attaches_hitl_action_handler():
    app = DecoratorApp()

    register_listeners(app, make_config(), Router(), agent=object())

    assert len(app.actions) == 11
    patterns = [pattern for pattern, _handler in app.actions]
    assert HITL_ACTION_ID_PATTERN in patterns
    assert PENDING_CHANNEL_ACTION_ID_PATTERN in patterns
    assert UPGRADE_ACTION_ID_PATTERN in patterns
    assert YOLO_EXTEND_ACTION_ID_PATTERN in patterns
    assert YOLO_REVOKE_ACTION_ID_PATTERN in patterns
    assert NEW_SESSION_ACTION_ID_PATTERN in patterns
    assert TIER_PICK_ACTION_PATTERN in patterns
    assert YOLO_DURATION_ACTION_PATTERN in patterns
    assert NIGHTLY_TOGGLE_ACTION_PATTERN in patterns
    assert CHANNELS_PAGE_ACTION_PATTERN in patterns
    assert FOOTGUN_CONFIRM_OPEN_ACTION_ID in patterns
    assert HITL_ACTION_ID_PATTERN.match("hitl_choice_0")
    assert HITL_ACTION_ID_PATTERN.match("hitl_choice_4")
    assert HITL_ACTION_ID_PATTERN.match("hitl_choice_always_0")
    assert HITL_ACTION_ID_PATTERN.match("hitl_choice_deny")
    assert not HITL_ACTION_ID_PATTERN.match("hitl_other_0")
    assert not HITL_ACTION_ID_PATTERN.match("hitl_choice_cancel")
    assert PENDING_CHANNEL_ACTION_ID_PATTERN.match("pending_channel_approve")
    assert PENDING_CHANNEL_ACTION_ID_PATTERN.match("pending_channel_deny")
    assert PENDING_CHANNEL_ACTION_ID_PATTERN.match("pending_channel_view_manifest")
    assert UPGRADE_ACTION_ID_PATTERN.match("upgrade_decision_approve_permanent")
    assert UPGRADE_ACTION_ID_PATTERN.match("upgrade_decision_approve_30d")
    assert UPGRADE_ACTION_ID_PATTERN.match("upgrade_decision_approve_24h")
    assert UPGRADE_ACTION_ID_PATTERN.match("upgrade_decision_approve_6h")
    assert UPGRADE_ACTION_ID_PATTERN.match("upgrade_decision_deny")
    assert YOLO_EXTEND_ACTION_ID_PATTERN.match("yolo_extend_C07TEST123")
    assert YOLO_REVOKE_ACTION_ID_PATTERN.match("yolo_revoke_C07TEST123")
    assert NEW_SESSION_ACTION_ID_PATTERN.match("engram_new_session_confirm")
    assert NEW_SESSION_ACTION_ID_PATTERN.match("engram_new_session_cancel")
    assert TIER_PICK_ACTION_PATTERN.match("engram_tier_pick")
    assert TIER_PICK_ACTION_PATTERN.match("engram_tier_pick:trusted:C07TEST123")
    assert YOLO_DURATION_ACTION_PATTERN.match("engram_yolo_duration")
    assert YOLO_DURATION_ACTION_PATTERN.match("engram_yolo_duration:24:C07TEST123")
    assert NIGHTLY_TOGGLE_ACTION_PATTERN.match("engram_nightly_toggle")
    assert NIGHTLY_TOGGLE_ACTION_PATTERN.match("engram_nightly_toggle:exclude:C07TEST123")
    assert CHANNELS_PAGE_ACTION_PATTERN.match("engram_channels_page")
    assert CHANNELS_PAGE_ACTION_PATTERN.match("engram_channels_page:2")
    assert [command for command, _handler in app.commands] == [
        "/engram",
        "/exclude-from-nightly",
        "/include-in-nightly",
    ]
    assert [callback_id for callback_id, _handler in app.views] == [
        "footgun_confirm_submit"
    ]
    assert [callback_id for callback_id, _handler in app.view_closed_handlers] == [
        "footgun_confirm_submit"
    ]


def test_parse_meta_eligibility_commands():
    exclude = parse_meta_eligibility_command("please exclude this channel from nightly")
    include = parse_meta_eligibility_command("/engram include <#C07TEAM|growth>")
    legacy = parse_meta_eligibility_command("/include-in-nightly <#C07TEAM|growth>")

    assert exclude is not None
    assert exclude.eligible is False
    assert exclude.target is None
    assert include is not None
    assert include.eligible is True
    assert include.target == "<#C07TEAM|growth>"
    assert legacy is not None
    assert legacy.eligible is True
    assert legacy.target == "<#C07TEAM|growth>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "eligible", "initial_included", "user_id", "ok", "expected_text", "expected_included"),
    [
        (
            PermissionTier.TASK_ASSISTANT,
            False,
            False,
            "U123",
            True,
            "Channel is already excluded. No change.",
            False,
        ),
        (
            PermissionTier.TASK_ASSISTANT,
            True,
            False,
            "U07OWNER",
            False,
            "Cannot include a `safe` channel in the nightly summary. Safe channels are excluded by default to protect team privacy. Upgrade the channel to `trusted` first: `/engram upgrade`",
            False,
        ),
        (
            PermissionTier.OWNER_SCOPED,
            False,
            True,
            "U123",
            True,
            "Channel excluded from nightly cross-channel summary.",
            False,
        ),
        (
            PermissionTier.OWNER_SCOPED,
            True,
            False,
            "U07OWNER",
            True,
            "Channel included in nightly cross-channel summary.",
            True,
        ),
        (
            PermissionTier.YOLO,
            False,
            True,
            "U123",
            True,
            "Channel excluded from nightly cross-channel summary.",
            False,
        ),
        (
            PermissionTier.YOLO,
            True,
            False,
            "U07OWNER",
            True,
            "Channel included in nightly cross-channel summary.",
            True,
        ),
    ],
)
async def test_nightly_inclusion_command_tier_matrix(
    tmp_path,
    tier: PermissionTier,
    eligible: bool,
    initial_included: bool,
    user_id: str,
    ok: bool,
    expected_text: str,
    expected_included: bool,
):
    home = tmp_path / ".engram"
    _write_channel_manifest(
        home,
        "C07TEAM",
        nightly_included=initial_included,
        tier=tier,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_meta_eligibility_command(
        router=router,
        config=make_config_with_owner_dm(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id=user_id,
        eligible=eligible,
        target_text=None,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result["ok"] is ok
    assert manifest.nightly_included is expected_included
    assert slack.ephemeral_calls[-1]["text"] == expected_text


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [PermissionTier.OWNER_SCOPED, PermissionTier.YOLO])
async def test_include_command_requires_owner(tmp_path, tier: PermissionTier):
    home = tmp_path / ".engram"
    _write_channel_manifest(
        home,
        "C07TEAM",
        nightly_included=False,
        tier=tier,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    result = await handle_meta_eligibility_command(
        router=router,
        config=make_config_with_owner_dm(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OTHER",
        eligible=True,
        target_text=None,
    )

    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert result == {"ok": False, "error": "not owner"}
    assert manifest.nightly_included is False
    assert slack.ephemeral_calls[-1] == {
        "channel": "C07TEAM",
        "user": "U07OTHER",
        "text": "Only the owner can include a channel in the nightly summary.",
    }


@pytest.mark.asyncio
async def test_engram_include_and_exclude_subcommands_route_to_same_handler(tmp_path):
    home = tmp_path / ".engram"
    _write_channel_manifest(
        home,
        "C07TEAM",
        nightly_included=False,
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()

    include = await handle_engram_command(
        router=router,
        config=make_config_with_owner_dm(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07OWNER",
        command_text="include",
    )
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert include["ok"] is True
    assert manifest.nightly_included is True
    assert slack.ephemeral_calls[-1]["text"] == "Channel included in nightly cross-channel summary."

    exclude = await handle_engram_command(
        router=router,
        config=make_config_with_owner_dm(),
        slack_client=slack,
        source_channel_id="C07TEAM",
        source_channel_name="growth",
        user_id="U07REQUESTER",
        command_text="exclude",
    )
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert exclude["ok"] is True
    assert manifest.nightly_included is False
    assert slack.ephemeral_calls[-1]["text"] == "Channel excluded from nightly cross-channel summary."


@pytest.mark.asyncio
async def test_legacy_nightly_aliases_route_to_direct_toggle_behavior(tmp_path):
    home = tmp_path / ".engram"
    _write_channel_manifest(
        home,
        "C07TEAM",
        nightly_included=False,
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    app = DecoratorApp()
    ack_calls = 0

    async def ack():
        nonlocal ack_calls
        ack_calls += 1

    register_listeners(app, make_config_with_owner_dm(), router, agent=object())
    commands = dict(app.commands)

    await commands["/include-in-nightly"](
        ack=ack,
        body={
            "channel_id": "C07TEAM",
            "channel_name": "growth",
            "user_id": "U07OWNER",
            "text": "",
        },
        client=slack,
    )
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert ack_calls == 1
    assert manifest.nightly_included is True
    assert slack.ephemeral_calls[-1]["text"] == "Channel included in nightly cross-channel summary."

    await commands["/exclude-from-nightly"](
        ack=ack,
        body={
            "channel_id": "C07TEAM",
            "channel_name": "growth",
            "user_id": "U07REQUESTER",
            "text": "",
        },
        client=slack,
    )
    manifest = load_manifest(channel_manifest_path("C07TEAM", home))
    assert ack_calls == 2
    assert manifest.nightly_included is False
    assert slack.ephemeral_calls[-1]["text"] == "Channel excluded from nightly cross-channel summary."


@pytest.mark.asyncio
async def test_registered_hitl_action_handler_acks_and_resolves_question():
    app = DecoratorApp()
    router = Router()
    slack = FakeSlackClient()
    q = make_question(suggestions=[{"name": "Run pytest"}])
    router.hitl.register(q)
    ack_calls = 0

    async def ack():
        nonlocal ack_calls
        ack_calls += 1

    register_listeners(app, make_config(), router, agent=object())
    _pattern, handler = app.actions[0]

    await handler(
        ack=ack,
        body=block_action_payload("prq-1|0"),
        client=slack,
    )

    assert ack_calls == 1
    await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == q.tool_input
    assert slack.update_calls[0]["text"] == "Answered: Run pytest"


@pytest.mark.asyncio
async def test_block_action_happy_path(caplog: pytest.LogCaptureFixture):
    router = Router()
    slack = FakeSlackClient()
    suggestion = permission_update()
    q = make_question(suggestions=[suggestion])
    router.hitl.register(q)

    with caplog.at_level(logging.INFO, logger="engram.hitl"):
        ack = await handle_block_action(
            block_action_payload("prq-1|0"), router, slack
        )
        await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)

    assert ack == {"ok": True}
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == q.tool_input
    assert result.updated_permissions == [suggestion]
    assert slack.update_calls[0]["channel"] == "C07TEST123"
    assert slack.update_calls[0]["ts"] == "1713800000.000100"
    answer_records = [
        record
        for record in caplog.records
        if record.name == "engram.hitl"
        and record.getMessage() == "hitl.answer_received"
    ]
    assert len(answer_records) == 1
    answer = answer_records[0]
    assert answer.permission_request_id == "prq-1"
    assert answer.choice == "0"
    assert answer.decision == "allow"


@pytest.mark.asyncio
async def test_block_action_deny_button():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)

    ack = await handle_block_action(
        block_action_payload("prq-1|deny"), router, slack
    )

    assert ack == {"ok": True}
    await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)
    result = q.future.result()
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "user denied"
    assert result.interrupt is True
    assert slack.update_calls[0]["text"] == "Answered: Deny"
    assert slack.update_calls[0]["blocks"][0]["text"]["text"] == "❌ Answered: Deny"


@pytest.mark.asyncio
async def test_block_action_always_allow_updates_manifest_and_logs(
    tmp_path,
    caplog,
):
    home = tmp_path / ".engram"
    _write_channel_manifest(
        home,
        "D07OWNER",
        identity=IdentityTemplate.OWNER_DM_FULL,
        tier=PermissionTier.OWNER_SCOPED,
    )
    router = Router(home=home, owner_dm_channel_id="D07OWNER")
    slack = FakeSlackClient()
    q = make_question(channel_id="D07OWNER")
    q.tool_name = "WebFetch"
    router.hitl.register(q)

    with caplog.at_level(logging.INFO, logger="engram.hitl"):
        ack = await handle_block_action(
            block_action_payload("prq-1|always|WebFetch"),
            router,
            slack,
        )
        await wait_until(lambda: q.future.done() and len(slack.update_calls) == 1)

    assert ack == {"ok": True}
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is not None
    update = result.updated_permissions[0]
    assert update.type == "addRules"
    assert update.rules is not None
    assert update.rules[0].tool_name == "WebFetch"
    manifest = load_manifest(channel_manifest_path("D07OWNER", home))
    assert "WebFetch" in manifest.permissions.allow
    assert manifest.permissions.allow.count("WebFetch") == 1
    assert slack.update_calls[0]["text"] == (
        "Answered: Always allow fetch (will not ask again in this channel)"
    )
    assert slack.update_calls[0]["blocks"][0]["text"]["text"] == (
        "✅ Answered: Always allow fetch (will not ask again in this channel)"
    )
    always_records = [
        record
        for record in caplog.records
        if record.name == "engram.hitl"
        and record.getMessage() == "hitl.always_allow_granted"
    ]
    assert len(always_records) == 1
    assert always_records[0].tool == "WebFetch"
    assert always_records[0].channel == "D07OWNER"


@pytest.mark.asyncio
async def test_block_action_wrong_user_rejected():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(who_can_answer="U_ALLOWED")
    router.hitl.register(q)

    ack = await handle_block_action(
        block_action_payload("prq-1|0", user_id="U_OTHER"), router, slack
    )

    assert ack == {"ok": False, "error": "not authorized"}
    assert not q.future.done()
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_footgun_confirm_open_opens_modal_for_owner():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(
        who_can_answer="U123",
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )
    router.hitl.register(q)

    ack = await handle_footgun_confirm_open(
        footgun_open_payload(),
        router,
        slack,
    )

    assert ack == {"ok": True}
    assert slack.ephemeral_calls == []
    assert len(slack.view_open_calls) == 1
    call = slack.view_open_calls[0]
    assert call["trigger_id"] == "trigger-1"
    assert call["view"]["callback_id"] == "footgun_confirm_submit"
    assert call["view"]["private_metadata"] == "prq-1"


@pytest.mark.asyncio
async def test_footgun_confirm_open_rejects_non_owner():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(
        who_can_answer="U_ALLOWED",
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )
    router.hitl.register(q)

    ack = await handle_footgun_confirm_open(
        footgun_open_payload(user_id="U_OTHER"),
        router,
        slack,
    )

    assert ack == {"ok": False, "error": "not authorized"}
    assert slack.view_open_calls == []
    assert slack.ephemeral_calls == [
        {
            "channel": "C07TEST123",
            "user": "U_OTHER",
            "text": "Owner approval required for destructive actions.",
        }
    ]


@pytest.mark.asyncio
async def test_footgun_confirm_submit_requires_exact_CONFIRM():  # noqa: N802 (testing literal string "CONFIRM")
    router = Router()
    slack = FakeSlackClient()
    tool_input = {"cmd": "rm -rf /tmp/demo", "cwd": "/tmp/demo"}
    q = make_question(
        who_can_answer="U123",
        tool_input=tool_input,
        footgun_match=match_footgun("Bash", tool_input),
    )
    router.hitl.register(q)

    ack = await handle_footgun_confirm_submit(
        footgun_submit_payload(typed_value="CONFIRM"),
        router,
        slack,
    )

    assert ack == {"ok": True}
    assert q.future.done()
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == tool_input
    assert slack.update_calls[0]["text"] == "Answered: Confirmed destructive action"


@pytest.mark.asyncio
async def test_footgun_confirm_submit_wrong_text_denies():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(
        who_can_answer="U123",
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )
    router.hitl.register(q)

    ack = await handle_footgun_confirm_submit(
        footgun_submit_payload(typed_value="confirm"),
        router,
        slack,
    )

    assert ack == {"ok": True}
    assert q.future.done()
    result = q.future.result()
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    assert slack.update_calls[0]["text"] == "Answered: Destructive action denied"


@pytest.mark.asyncio
async def test_footgun_confirm_close_denies():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(
        who_can_answer="U123",
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )
    router.hitl.register(q)

    ack = await handle_footgun_confirm_closed(
        footgun_closed_payload(),
        router,
        slack,
    )

    assert ack == {"ok": True}
    assert q.future.done()
    result = q.future.result()
    assert isinstance(result, PermissionResultDeny)
    assert slack.update_calls[0]["text"] == "Answered: Destructive action denied"


@pytest.mark.asyncio
async def test_block_action_missing_question_ok():
    router = Router()
    slack = FakeSlackClient()

    ack = await handle_block_action(
        block_action_payload("missing|0"), router, slack
    )

    assert ack == {"ok": False, "error": "question not found (may be resolved)"}
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_block_action_already_resolved_idempotent():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)
    original_result = PermissionResultAllow()
    router.hitl.resolve("prq-1", original_result)

    ack = await handle_block_action(
        block_action_payload("prq-1|deny"), router, slack
    )

    assert ack == {"ok": True, "info": "already resolved"}
    assert q.future.result() is original_result
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_thread_reply_happy_path():
    router = Router()
    slack = FakeSlackClient()
    q = make_question()
    router.hitl.register(q)

    await handle_thread_reply(
        {
            "channel": "C07TEST123",
            "thread_ts": "1713800000.000100",
            "text": "Please run the focused pytest target.",
            "user": "U123",
        },
        router,
        slack,
    )

    assert q.future.done()
    result = q.future.result()
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {
        "cmd": "pytest",
        "timeout": 30,
        "_user_answer": "Please run the focused pytest target.",
    }
    assert slack.update_calls[0]["text"] == (
        "Answered: Please run the focused pytest target."
    )


@pytest.mark.asyncio
async def test_thread_reply_wrong_channel_ignored():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(channel_id="C07TEST123")
    router.hitl.register(q)

    await handle_thread_reply(
        {
            "channel": "C07OTHER",
            "thread_ts": "1713800000.000100",
            "text": "This should not resolve the question.",
            "user": "U123",
        },
        router,
        slack,
    )

    assert not q.future.done()
    assert slack.update_calls == []


@pytest.mark.asyncio
async def test_thread_reply_ignored_for_footgun_confirmation():
    router = Router()
    slack = FakeSlackClient()
    q = make_question(
        tool_input={"cmd": "rm -rf /tmp/demo"},
        footgun_match=match_footgun("Bash", {"cmd": "rm -rf /tmp/demo"}),
    )
    router.hitl.register(q)

    await handle_thread_reply(
        {
            "channel": "C07TEST123",
            "thread_ts": "1713800000.000100",
            "text": "CONFIRM",
            "user": "U123",
        },
        router,
        slack,
    )

    assert not q.future.done()
    assert slack.update_calls == []
