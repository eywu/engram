# Slack App Setup

Engram needs its own Slack app with Socket Mode enabled. This is a one-time
~5-minute manual step.

## 1. Create the app

1. Go to <https://api.slack.com/apps>
2. Click **Create New App** → **From an app manifest**
3. Pick the workspace you want Engram in
4. Paste the manifest below and click **Next** → **Create**

## 2. Manifest

```yaml
display_information:
  name: Engram
  description: Personal AI agent — per-channel memory and skills.
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Engram
    always_online: true
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  slash_commands:
    - command: /engram
      description: "Manage Engram permission tiers, YOLO mode, and nightly-summary inclusion"
      usage_hint: "upgrade | yolo | channels | exclude | include"
      should_escape: false
    - command: /exclude-from-nightly
      description: "Exclude this channel from the nightly cross-channel summary"
      usage_hint: ""
      should_escape: false
    - command: /include-in-nightly
      description: "Include this channel in the nightly cross-channel summary"
      usage_hint: ""
      should_escape: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - channels:read
      - chat:write
      - commands
      - files:read
      - files:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - mpim:history
      - mpim:read
      - reactions:read
      - reactions:write
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
      - message.im
      - message.mpim
  interactivity:
    # Block Kit button actions arrive over Socket Mode when interactivity is enabled.
    # They are not event_subscriptions and do not use an "actions" bot scope.
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

**Slash commands are registered for you via the manifest.** You do not need to manually create `/engram` or the nightly-summary commands — they ship with the app.

> ℹ️ Running `engram setup` also writes this manifest to
> `/tmp/engram-slack-manifest.yaml` for convenience.

## 3. Install to your workspace

After creating the app:

1. Click **Install to Workspace** (left sidebar under "Settings" → "Install App")
2. Click **Allow**

## 4. Grab your tokens

You need two tokens:

### Bot User OAuth Token (`xoxb-…`)
- Sidebar → **OAuth & Permissions**
- Copy **Bot User OAuth Token** (starts with `xoxb-`)

### App-Level Token (`xapp-…`)
- Sidebar → **Basic Information**
- Scroll to **App-Level Tokens** → **Generate Token and Scopes**
- Give it any name (e.g. `engram-socket`)
- Add scope: **`connections:write`**
- Click **Generate**, then copy the token (starts with `xapp-`)

## 5. Run `engram setup`

```bash
engram setup
```

Paste both tokens when prompted. The wizard writes them to
`~/.engram/config.yaml` with mode `600`.

## 6. Verify

```bash
engram status
```

Should show both tokens (masked) and report "claude CLI ✓" and MCP inventory.

## 7. Run the bridge

```bash
engram run
```

Then DM the Engram bot in Slack. You should get a Claude response within ~30s.
