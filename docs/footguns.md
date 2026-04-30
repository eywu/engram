# Footgun confirmations

Engram treats some shell commands as footguns. When Claude proposes one, Engram pauses the turn, posts a destructive-action confirmation card in Slack, and the confirm button opens a modal that requires a fresh typed `CONFIRM`.

## What it is

Footgun confirmation is a second safety barrier for destructive shell patterns. It is separate from normal HITL approval cards: the command does not run unless the authorized responder opens the modal and types the exact confirmation text.

The canonical detector lives in [`src/engram/footguns.py`](../src/engram/footguns.py). Treat that file as the source of truth for what does and does not trigger this flow.

## When it fires

Current built-in patterns are:

- Recursive `rm` with a recursive flag, such as `rm -rf build` or `rm -fr build`
- `sudo`
- `curl ... | sh`, `curl ... | bash`, or `curl ... | zsh`
- `wget ... | sh`, `wget ... | bash`, or `wget ... | zsh`
- `dd if=...`
- `mkfs`
- `fdisk`
- `chmod -R 777`
- Direct writes to Linux block devices, such as `echo x > /dev/sda1`
- `git push --force` to shared branches
- Destructive SQL matching `drop table`, `drop database`, `truncate table`, or `truncate database`

The detector is exact and finite. If a pattern is not in `src/engram/footguns.py`, it is not currently part of the built-in confirmation list.

## How it interacts with tiers

- `trusted` channels and the owner DM show the destructive-action card and require the typed confirmation modal before the command can continue.
- `safe` channels usually block these commands earlier because Bash and write-side tools are denied by default. If a footgun still matches there, Engram denies it outright and tells the user to request an upgrade.
- `yolo` still shows the confirmation flow. YOLO bypasses normal HITL questions, but destructive footguns remain gated by typed confirmation on purpose.

## How to override

Type `CONFIRM` exactly and submit the modal. `confirm`, extra text, any other value, or closing the modal all abort the command.

## Testing note

The canonical pattern coverage lives in [`tests/test_footguns.py`](../tests/test_footguns.py). That test keeps the positive cases aligned with `FOOTGUN_PATTERNS` and checks the expected non-matches, including plain `rm -f` and force-pushing a personal namespaced branch.

## See also

- [Permission tiers](permission-tiers.md)
- [Human-in-the-loop](hitl.md)
