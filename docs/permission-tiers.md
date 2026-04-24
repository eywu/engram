# Permission tiers

Engram channels run with one of three canonical permission tiers:

| Tier | Intended use | Behavior summary |
| --- | --- | --- |
| `safe` | Shared channels | Deny-only baseline, HITL for most sensitive tools, rate-limited, excluded from nightly meta-summary by default behavior. |
| `trusted` | Private owner channels | Read-heavy auto-allow, sticky HITL for writes, higher trust for normal coding and repo work. |
| `yolo` | Short bursts of risky iteration | Temporary high-trust mode with a typed-confirm footgun barrier and a bounded time window. |

Historical aliases remain readable for backwards compatibility:

| Historical name | Canonical tier |
| --- | --- |
| `task-assistant` | `safe` |
| `owner-scoped` | `trusted` |

Existing channel manifests that still use the historical names are accepted on load, normalized in memory, and written back with the canonical names the next time Engram persists the manifest.

## Requesting an upgrade in Slack

From any channel, run:

```text
/engram upgrade <safe|trusted|yolo> [reason...]
```

Examples:

```text
/engram upgrade trusted this is my private repo workspace
/engram upgrade yolo trying a risky refactor this afternoon
```

Engram posts a waiting message in the source channel and sends an approval card to the configured owner DM.

## Approval flow

Only the configured owner can approve upgrade buttons.

- Non-YOLO requests offer `Approve until revoked`, `Approve 30d`, and `Deny`.
- YOLO requests offer `Approve 24h`, `Approve 6h`, and `Deny`.
- The source-channel waiting message is edited to the final result.
- The owner-DM card is edited to show the decision.
- If a newer request is made in the same channel before approval, the older DM card is marked `Superseded by newer request.`

## CLI shortcuts

The owner can bypass Slack approval with:

```text
engram channels upgrade <channel-id> <tier> [--until 24h|30d|permanent]
engram channels tier <channel-id>
```

`engram channels upgrade` accepts the historical aliases `task-assistant` and `owner-scoped`, but prints a deprecation warning and persists `safe` or `trusted`.

`engram channels tier` prints the effective tier, YOLO status, and expiry timestamp if one is active.

If your Slack workspace won't let you register slash commands, use the full
CLI parity guide in [INSTALL.md](INSTALL.md#managing-engram-without-slash-commands).

## See also

- [Managing Engram without slash commands](INSTALL.md#managing-engram-without-slash-commands)
- [Human-in-the-loop](hitl.md)
- [Footgun confirmations](footguns.md)
