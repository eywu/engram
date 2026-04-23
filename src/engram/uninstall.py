"""Clean removal workflow for Engram."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import typer

from engram.paths import engram_home

SLACK_APPS_URL = "https://api.slack.com/apps"


@dataclass(frozen=True)
class LaunchdJob:
    label: str
    description: str
    plist_path: Path


def run_uninstall(*, keep_data: bool = False, purge: bool = False, dry_run: bool = False) -> None:
    """Run the interactive or non-interactive uninstall workflow."""
    if keep_data and purge:
        raise typer.BadParameter("--keep-data cannot be combined with --purge")

    home = engram_home()
    jobs = _launchd_jobs()

    if dry_run:
        typer.echo("Dry run: no changes will be made.")
        _print_plan(home, jobs, purge=purge, keep_data=keep_data)
        _print_dry_run_commands(jobs, purge=purge, keep_data=keep_data)
        return

    if not purge:
        _print_plan(home, jobs, purge=False, keep_data=keep_data)
        if not typer.confirm("Continue?", default=False):
            typer.echo("Aborted.")
            return

    delete_data = purge or (
        not keep_data
        and typer.confirm(
            f"Delete {_display_home(home)}/? This removes config, memory DB, and logs.",
            default=False,
        )
    )
    uninstall_cli = purge or typer.confirm(
        "Uninstall the `engram` CLI with `uv tool uninstall engram`?",
        default=False,
    )
    slack_cleanup = purge or typer.confirm(
        "Open Slack app cleanup instructions at the end?",
        default=False,
    )

    typer.echo()
    typer.echo("Removing Engram...")
    for job in jobs:
        _unload_launchd_job(job)
    _remove_launchd_plists(jobs)

    if delete_data:
        _delete_engram_home(home)
    else:
        typer.echo(f"  - kept {_display_home(home)}/")

    if uninstall_cli:
        _uninstall_cli()
    else:
        typer.echo("  - kept engram CLI")

    typer.echo()
    _print_slack_cleanup_message(selected=slack_cleanup)


def _launchd_jobs() -> tuple[LaunchdJob, ...]:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    return (
        LaunchdJob(
            label="com.engram.bridge",
            description="launchd bridge job",
            plist_path=launch_agents / "com.engram.bridge.plist",
        ),
        LaunchdJob(
            label="com.engram.v3.nightly",
            description="launchd nightly job",
            plist_path=launch_agents / "com.engram.v3.nightly.plist",
        ),
    )


def _print_plan(
    home: Path,
    jobs: tuple[LaunchdJob, ...],
    *,
    purge: bool,
    keep_data: bool,
) -> None:
    typer.echo("This will remove Engram from your system.")
    typer.echo()
    for job in jobs:
        typer.echo(f"  ✓ unload {job.description} ({job.label})")
    typer.echo("  ✓ remove launchd plist files")
    typer.echo()
    typer.echo("Optional:")
    data_marker = "x" if purge else " "
    cli_marker = "x" if purge else " "
    slack_marker = "x" if purge else " "
    data_suffix = " (skipped by --keep-data)" if keep_data and not purge else ""
    data_line = (
        f"  [{data_marker}] delete {_display_home(home)}/ "
        f"(config, memory DB, logs, {_home_size(home)}){data_suffix}"
    )
    typer.echo(data_line)
    typer.echo(f"  [{cli_marker}] uninstall the `engram` CLI (uv tool uninstall engram)")
    typer.echo(f"  [{slack_marker}] remove your Slack app (NOT automated — you'll get a link)")
    typer.echo()


def _print_dry_run_commands(
    jobs: tuple[LaunchdJob, ...],
    *,
    purge: bool,
    keep_data: bool,
) -> None:
    domain = _launchctl_domain()
    typer.echo("Commands that would run:")
    for job in jobs:
        typer.echo(f"  launchctl bootout {domain}/{job.label}")
        typer.echo(f"  launchctl unload {_display_path(job.plist_path)}  # fallback")
    for job in jobs:
        typer.echo(f"  rm -f {_display_path(job.plist_path)}")
    if purge:
        typer.echo(f"  rm -rf {_display_home(engram_home())}/")
        typer.echo("  uv tool uninstall engram")
    elif keep_data:
        typer.echo(f"  # keep {_display_home(engram_home())}/")
    else:
        typer.echo(f"  # prompt before deleting {_display_home(engram_home())}/")
        typer.echo("  # prompt before running: uv tool uninstall engram")
    typer.echo(f"  # Slack app cleanup is manual: {SLACK_APPS_URL}")


def _unload_launchd_job(job: LaunchdJob) -> None:
    domain = _launchctl_domain()
    bootout = ["launchctl", "bootout", f"{domain}/{job.label}"]
    result = _run_command(bootout)
    if result is not None and result.returncode == 0:
        typer.echo(f"  ✓ unloaded {job.label}")
        return

    if job.plist_path.exists():
        unload = ["launchctl", "unload", str(job.plist_path)]
        fallback = _run_command(unload)
        if fallback is not None and fallback.returncode == 0:
            typer.echo(f"  ✓ unloaded {job.label} using launchctl unload")
            return
        _warn_command(f"could not unload {job.label}", fallback or result)
        return

    _warn_command(f"{job.label} was not loaded or its plist was missing", result)


def _remove_launchd_plists(jobs: tuple[LaunchdJob, ...]) -> None:
    for job in jobs:
        try:
            job.plist_path.unlink(missing_ok=True)
        except OSError as exc:
            typer.echo(f"  ! could not remove {_display_path(job.plist_path)}: {exc}")
        else:
            typer.echo(f"  ✓ removed {_display_path(job.plist_path)}")


def _delete_engram_home(home: Path) -> None:
    if home.name != ".engram":
        raise RuntimeError(f"refusing to delete unexpected Engram home: {home}")
    if not home.exists() and not home.is_symlink():
        typer.echo(f"  - {_display_home(home)}/ was already absent")
        return
    if home.is_symlink() or home.is_file():
        home.unlink()
    else:
        shutil.rmtree(home)
    typer.echo(f"  ✓ deleted {_display_home(home)}/")


def _uninstall_cli() -> None:
    result = _run_command(["uv", "tool", "uninstall", "engram"])
    if result is not None and result.returncode == 0:
        typer.echo("  ✓ uninstalled engram CLI")
        return
    _warn_command("could not uninstall engram CLI", result)


def _print_slack_cleanup_message(*, selected: bool) -> None:
    if selected:
        typer.echo("Slack app cleanup is manual:")
    else:
        typer.echo("Optional Slack app cleanup remains manual:")
    typer.echo(f"  {SLACK_APPS_URL}")
    typer.echo("Open your Engram app there and remove it from the workspace if desired.")


def _run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None


def _warn_command(message: str, result: subprocess.CompletedProcess[str] | None) -> None:
    detail = _command_detail(result)
    if detail:
        typer.echo(f"  ! {message}: {detail}")
    else:
        typer.echo(f"  ! {message}")


def _command_detail(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "command not found"
    output = (result.stderr or result.stdout or "").strip()
    if not output:
        return f"exit {result.returncode}"
    return output.splitlines()[0]


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _display_home(home: Path) -> str:
    if home == Path.home() / ".engram":
        return "~/.engram"
    return str(home)


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _home_size(home: Path) -> str:
    if not home.exists():
        return "not found"
    total = 0
    for path in home.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return _format_bytes(total)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            if value >= 10:
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
