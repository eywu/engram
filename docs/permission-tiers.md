# Permission tiers

Engram channels run with one of three permission tiers:

| Tier | Intended use | Notes |
| --- | --- | --- |
| `task-assistant` | Shared channels | Safe default. Engram asks more often and keeps tighter defaults. |
| `owner-scoped` | Private owner channels | Broader defaults for normal coding and repo work. |
| `yolo` | Short bursts of fast iteration | Time-boxed high-trust mode. Prefer 6h or 24h windows. |

## Requesting an upgrade in Slack

From any channel, run:

```text
/engram upgrade <task-assistant|owner-scoped|yolo> [reason...]
```

Examples:

```text
/engram upgrade owner-scoped this is my private repo workspace
/engram upgrade yolo trying a risky refactor this afternoon
```

Engram posts a waiting message in the source channel and sends an approval card to the configured owner DM.

## Approval flow

Only the configured owner can approve upgrade buttons.

- Non-YOLO requests offer:
  - `Approve until revoked`
  - `Approve 30d`
  - `Deny`
- YOLO requests offer:
  - `Approve 24h`
  - `Approve 6h`
  - `Deny`

After a decision:

- The source-channel waiting message is edited to the final result.
- The owner-DM card is edited to show the decision.
- If a newer request is made in the same channel before approval, the older DM card is marked `Superseded by newer request.`

## CLI shortcuts

The owner can bypass Slack approval with:

```text
engram channels upgrade <channel-id> <tier> [--until 24h|30d|permanent]
engram channels tier <channel-id>
```

`engram channels tier` prints the effective tier, YOLO status, and expiry timestamp if one is active.

## See also

- [Human-in-the-loop](hitl.md)
- [Footgun confirmations](footguns.md)
