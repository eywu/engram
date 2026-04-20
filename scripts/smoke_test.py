"""M1 smoke test.

Starts the Engram bridge, waits for a live DM from a human, and measures
ingress → agent → egress timing + cost.

Usage:
    # Prerequisite: engram setup complete; ~/.engram/config.yaml has tokens.
    uv run python scripts/smoke_test.py

Exit codes:
    0 = pass (at least one round-trip observed within timeout)
    1 = config error
    2 = bridge failed to start
    3 = no message received within timeout
    4 = bridge crashed
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Ensure src/ is importable when run via `uv run python scripts/…`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engram.config import EngramConfig

TIMEOUT_SECONDS = 90
LOG_PATH = Path("/tmp/engram-smoke-test.log")


@dataclass
class RoundTrip:
    received_at: float
    session_label: str
    user_text_len: int
    posted_chunks: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    posted_at: float | None = None


async def main() -> int:
    # 1. Verify config.
    print("== M1 Smoke Test ==", flush=True)
    try:
        cfg = EngramConfig.load()
    except RuntimeError as e:
        print(f"[FAIL] config error: {e}", file=sys.stderr)
        return 1

    print(f"  model: {cfg.anthropic.model}", flush=True)
    print(f"  bot token: {cfg.slack.bot_token[:8]}…{cfg.slack.bot_token[-4:]}", flush=True)
    print(f"  app token: {cfg.slack.app_token[:8]}…{cfg.slack.app_token[-4:]}", flush=True)
    print(flush=True)

    # 2. Start the bridge as a subprocess so its logs are isolated.
    print(f"Starting bridge — logs → {LOG_PATH}", flush=True)
    log_fd = LOG_PATH.open("wb")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "engram.main",
        stdout=log_fd,
        stderr=log_fd,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    # 3. Wait for Socket Mode to connect.
    print("Waiting up to 15s for Socket Mode to connect…", flush=True)
    connected = await _wait_for_pattern(LOG_PATH, r"engram\.starting", timeout=15.0)
    if not connected:
        print("[FAIL] bridge never emitted engram.starting. Check log.", file=sys.stderr)
        proc.send_signal(signal.SIGTERM)
        await proc.wait()
        return 2
    print("[OK] bridge running.", flush=True)
    print(flush=True)

    # 4. Prompt for a human DM.
    print("=" * 60, flush=True)
    print("NOW: in Slack (growthgauge.slack.com), DM the Engram bot.", flush=True)
    print("     Any message works. Try: 'Hello'", flush=True)
    print(f"     Waiting up to {TIMEOUT_SECONDS}s…", flush=True)
    print("=" * 60, flush=True)

    # 5. Poll the log for ingress → egress events.
    trip: RoundTrip | None = None
    start = time.monotonic()
    while time.monotonic() - start < TIMEOUT_SECONDS:
        # Did the bridge die?
        if proc.returncode is not None:
            print(f"[FAIL] bridge exited with code {proc.returncode}", file=sys.stderr)
            return 4

        text = _read_log_safely(LOG_PATH)
        trip = _parse_round_trip(text)
        if trip and trip.posted_at:
            break
        await asyncio.sleep(0.5)

    # 6. Stop the bridge.
    print("\nStopping bridge…", flush=True)
    proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        print("  (bridge didn't exit in 10s; killing)", flush=True)
        proc.kill()
        await proc.wait()
    log_fd.close()

    # 7. Report.
    print()
    print("=" * 60, flush=True)
    if not trip:
        print("[FAIL] no message received within timeout.", flush=True)
        print(f"       See log: {LOG_PATH}", flush=True)
        return 3
    if not trip.posted_at:
        print("[FAIL] received a message but never posted a reply.", flush=True)
        print(f"       session: {trip.session_label}", flush=True)
        print(f"       See log: {LOG_PATH}", flush=True)
        return 3

    duration_s = trip.posted_at - trip.received_at
    print("[PASS] round-trip complete", flush=True)
    print(f"  session:        {trip.session_label}", flush=True)
    print(f"  user text len:  {trip.user_text_len}", flush=True)
    print(f"  chunks posted:  {trip.posted_chunks}", flush=True)
    print(f"  cost:           ${trip.cost_usd:.4f}" if trip.cost_usd is not None else "  cost:           (not reported)", flush=True)
    print(f"  sdk duration:   {trip.duration_ms}ms" if trip.duration_ms else "  sdk duration:   (not reported)", flush=True)
    print(f"  wall duration:  {duration_s:.2f}s", flush=True)
    print()
    print(f"  done criterion: {'✓' if duration_s <= 30 else '⚠'} response within 30s", flush=True)
    print("=" * 60, flush=True)
    return 0


async def _wait_for_pattern(path: Path, pattern: str, timeout: float) -> bool:
    start = time.monotonic()
    regex = re.compile(pattern)
    while time.monotonic() - start < timeout:
        text = _read_log_safely(path)
        if regex.search(text):
            return True
        await asyncio.sleep(0.25)
    return False


def _read_log_safely(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


INGRESS_RE = re.compile(
    r"ingress\.received session=(\S+) user=(\S+) len=(\d+)"
)
EGRESS_RE = re.compile(
    r"egress\.posted session=(\S+) chunks=(\d+) cost=(\S+) duration_ms=(\S+)"
)


def _parse_round_trip(text: str) -> RoundTrip | None:
    ing = INGRESS_RE.search(text)
    if not ing:
        return None
    session, _user, text_len = ing.group(1), ing.group(2), int(ing.group(3))
    # Timestamp from line prefix (we set format "%(asctime)s ...")
    received_at = _guess_timestamp(text, ing.start()) or time.time()
    trip = RoundTrip(received_at=received_at, session_label=session, user_text_len=text_len)

    eg = EGRESS_RE.search(text)
    if eg and eg.group(1) == session:
        trip.posted_chunks = int(eg.group(2))
        cost = eg.group(3)
        duration = eg.group(4)
        trip.cost_usd = float(cost) if cost not in ("None", "") else None
        trip.duration_ms = int(duration) if duration not in ("None", "") else None
        trip.posted_at = _guess_timestamp(text, eg.start()) or time.time()
    return trip


def _guess_timestamp(text: str, offset: int) -> float | None:
    """Parse '2026-04-20 13:40:12,345 INFO …' style prefix for the line at offset."""
    # Find the start of the current line
    line_start = text.rfind("\n", 0, offset) + 1
    line_prefix = text[line_start : line_start + 23]
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d{3})", line_prefix)
    if not m:
        return None
    import datetime

    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp() + int(m.group(2)) / 1000.0
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
