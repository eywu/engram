"""Trust resolution for MCP package-backed server additions."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import yaml

from engram import paths

log = logging.getLogger(__name__)

MCP_TRUST_CACHE_TTL = timedelta(hours=24)
MCP_TRUST_CACHE_FILE = "mcp_trust_cache.json"
TRUSTED_PUBLISHERS_OVERLAY_FILE = "trusted_publishers.yaml"
_PYPI_STATS_PACKAGE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|>|<)(?P<version>[^;,\s]+))?$"
)
_GITHUB_REPO_RE = re.compile(
    r"^(?:https://|git\+https://|git://|ssh://git@|git@)github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/#]+?)(?:\.git)?(?:[#/].*)?$",
    re.IGNORECASE,
)


class PackageRegistry(StrEnum):
    NPM = "npm"
    PYPI = "pypi"
    CUSTOM = "custom"


class MCPTrustTier(StrEnum):
    OFFICIAL = "official"
    COMMUNITY_TRUSTED = "community-trusted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MCPPackageRef:
    registry: PackageRegistry
    lookup_name: str | None
    display_name: str | None
    version: str | None


@dataclass(frozen=True)
class MCPTrustDecision:
    server_name: str
    tier: MCPTrustTier
    registry: str
    package_name: str | None = None
    version: str | None = None
    publisher: str | None = None
    publishers: list[str] = field(default_factory=list)
    first_published_at: str | None = None
    last_published_at: str | None = None
    weekly_downloads: int | None = None
    repo_url: str | None = None
    repo_stars: int | None = None
    repo_contributors: int | None = None
    repo_last_commit_at: str | None = None
    package_age_days: int | None = None
    trust_summary: str = ""
    reason: str = ""

    def to_cache_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tier"] = self.tier.value
        return payload

    @classmethod
    def from_cache_payload(cls, payload: dict[str, Any]) -> MCPTrustDecision:
        data = dict(payload)
        data["tier"] = MCPTrustTier(data["tier"])
        return cls(**data)


async def resolve_mcp_server_trust(
    server_name: str,
    server_config: dict[str, Any] | None,
    *,
    home: Path | None = None,
    fetch_json: Any | None = None,
    now: datetime | None = None,
) -> MCPTrustDecision:
    """Resolve MCP package metadata and classify its trust tier.

    Unknown or failed lookups fail closed and return ``unknown``.
    """
    current_time = now or datetime.now(UTC)
    fetcher = fetch_json or _fetch_json
    trusted_publishers = _load_trusted_publishers(home)
    cache_key = _cache_key(server_name, server_config, trusted_publishers)
    cached = _load_cached_decision(cache_key, home=home, now=current_time)
    if cached is not None:
        return cached

    try:
        decision = await _resolve_uncached_decision(
            server_name,
            server_config,
            trusted_publishers=trusted_publishers,
            fetch_json=fetcher,
            now=current_time,
        )
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        log.warning(
            "mcp_trust.resolve_failed server=%s error=%s",
            server_name,
            f"{type(exc).__name__}: {exc}",
        )
        decision = _unknown_decision(
            server_name,
            registry=PackageRegistry.CUSTOM,
            reason="metadata lookup failed",
        )

    _store_cached_decision(cache_key, decision, home=home, now=current_time)
    return decision


def render_owner_approval_markdown(
    *,
    channel_id: str,
    channel_label: str | None,
    decisions: list[MCPTrustDecision],
) -> str:
    """Render the owner-DM approval card body for unknown MCP additions."""
    channel_text = channel_label or channel_id
    lines = [
        f"*Channel:* {channel_text} (`{channel_id}`)",
        "",
        "*Requested MCP additions require owner approval.*",
    ]
    for decision in decisions:
        package_text = decision.package_name or "(unknown package)"
        version_text = decision.version or "latest/unspecified"
        lines.extend(
            [
                "",
                f"*Server:* `{decision.server_name}`",
                f"• *Package:* `{package_text}` @ `{version_text}`",
                f"• *Source registry:* {decision.registry}",
                f"• *Maintainer / publisher:* {decision.publisher or 'unknown'}",
                f"• *First published:* {_display_date(decision.first_published_at)}",
                f"• *Last published:* {_display_date(decision.last_published_at)}",
                f"• *Weekly downloads:* {_display_downloads(decision.weekly_downloads)}",
                f"• *Source repo:* {_display_repo(decision.repo_url, decision.repo_stars)}",
                f"• *Trust signals:* {decision.trust_summary or 'none'}",
            ]
        )
    return "\n".join(lines)


def render_community_notification(
    *,
    channel_id: str,
    channel_label: str | None,
    decisions: list[MCPTrustDecision],
) -> str:
    """Render the owner-DM notification for community-trusted additions."""
    channel_text = channel_label or channel_id
    lines = [
        "Community-trusted MCP addition auto-approved.",
        f"Channel: {channel_text} ({channel_id})",
    ]
    for decision in decisions:
        package_text = decision.package_name or decision.server_name
        version_text = decision.version or "latest/unspecified"
        lines.append(
            f"- {decision.server_name}: {package_text} @ {version_text} "
            f"[{decision.registry}; {decision.trust_summary}]"
        )
    return "\n".join(lines)


def add_trusted_publishers(
    publishers: list[tuple[str, str]],
    *,
    home: Path | None = None,
) -> None:
    """Persist trusted publisher overrides in the operator overlay file."""
    if not publishers:
        return
    overlay_path = paths.state_dir(home) / TRUSTED_PUBLISHERS_OVERLAY_FILE
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay = _load_yaml_mapping(overlay_path) if overlay_path.exists() else {}
    changed = False
    for registry_name, publisher in publishers:
        registry = str(registry_name or "").strip().lower()
        normalized = _normalize_publisher(publisher)
        if registry not in {"npm", "pypi"} or not normalized:
            continue
        section = overlay.setdefault(registry, {})
        section_publishers = section.setdefault("publishers", [])
        if normalized not in section_publishers:
            section_publishers.append(normalized)
            changed = True
    if changed:
        overlay_path.write_text(
            yaml.safe_dump(overlay, sort_keys=False, default_flow_style=False, indent=2),
            encoding="utf-8",
        )


async def _resolve_uncached_decision(
    server_name: str,
    server_config: dict[str, Any] | None,
    *,
    trusted_publishers: dict[str, dict[str, set[str]]],
    fetch_json: Any,
    now: datetime,
) -> MCPTrustDecision:
    if server_name == "engram-memory":
        return _official_decision(
            server_name,
            registry=PackageRegistry.CUSTOM,
            package_name="engram-memory",
            version=None,
            publisher="engram",
            publishers=["engram"],
            reason="built-in Engram MCP server",
        )

    package_ref = _extract_package_ref(server_config)
    if package_ref is None:
        return _unknown_decision(
            server_name,
            registry=PackageRegistry.CUSTOM,
            reason="server command does not map to an npm or PyPI package",
        )

    if package_ref.lookup_name is None:
        return _unknown_decision(
            server_name,
            registry=package_ref.registry,
            reason="package launcher detected but package name could not be parsed",
            package_name=package_ref.display_name,
            version=package_ref.version,
        )

    if package_ref.registry == PackageRegistry.NPM:
        metadata = await _fetch_npm_metadata(package_ref, fetch_json=fetch_json)
    elif package_ref.registry == PackageRegistry.PYPI:
        metadata = await _fetch_pypi_metadata(package_ref, fetch_json=fetch_json)
    else:
        metadata = {}

    decision = await _classify_decision(
        server_name,
        package_ref=package_ref,
        metadata=metadata,
        trusted_publishers=trusted_publishers,
        fetch_json=fetch_json,
        now=now,
    )
    return decision


async def _classify_decision(
    server_name: str,
    *,
    package_ref: MCPPackageRef,
    metadata: dict[str, Any],
    trusted_publishers: dict[str, dict[str, set[str]]],
    fetch_json: Any,
    now: datetime,
) -> MCPTrustDecision:
    registry_key = package_ref.registry.value
    trusted = trusted_publishers.get(registry_key, {})
    publisher_names = [
        _normalize_publisher(name)
        for name in metadata.get("publishers", [])
        if _normalize_publisher(name)
    ]
    primary_publisher = next(iter(publisher_names), None)
    repo_url = metadata.get("repo_url")
    repo_signals = await _fetch_repo_signals(repo_url, fetch_json=fetch_json)

    first_published = _parse_datetime(metadata.get("first_published_at"))
    last_published = _parse_datetime(metadata.get("last_published_at"))
    package_age_days = (now - first_published).days if first_published is not None else None
    weekly_downloads = _coerce_int(metadata.get("weekly_downloads"))
    repo_last_commit = _parse_datetime(repo_signals.get("last_commit_at"))
    repo_contributors = _coerce_int(repo_signals.get("contributors"))
    repo_active = (
        repo_last_commit is not None
        and now - repo_last_commit <= timedelta(days=90)
    )

    if _is_official_package(
        package_ref=package_ref,
        publisher_names=publisher_names,
        repo_owner=repo_signals.get("owner"),
        trusted_publishers=trusted,
    ):
        return _official_decision(
            server_name,
            registry=package_ref.registry,
            package_name=metadata.get("package_name") or package_ref.display_name,
            version=metadata.get("version") or package_ref.version,
            publisher=primary_publisher,
            publishers=publisher_names,
            first_published_at=_isoformat_or_none(first_published),
            last_published_at=_isoformat_or_none(last_published),
            weekly_downloads=weekly_downloads,
            repo_url=repo_url,
            repo_stars=_coerce_int(repo_signals.get("stars")),
            repo_contributors=repo_contributors,
            repo_last_commit_at=_isoformat_or_none(repo_last_commit),
            package_age_days=package_age_days,
            reason="publisher or scope matches trusted allowlist",
            trust_summary=_trust_summary(
                package_age_days=package_age_days,
                weekly_downloads=weekly_downloads,
                repo_active=repo_active,
                repo_contributors=repo_contributors,
            ),
        )

    community_trusted = (
        package_age_days is not None
        and package_age_days >= 365
        and weekly_downloads is not None
        and weekly_downloads > 1000
        and repo_active
        and repo_contributors is not None
        and repo_contributors > 5
    )
    tier = (
        MCPTrustTier.COMMUNITY_TRUSTED if community_trusted else MCPTrustTier.UNKNOWN
    )
    reason = (
        "meets community-trusted thresholds"
        if community_trusted
        else "package does not meet official or community-trusted thresholds"
    )
    return MCPTrustDecision(
        server_name=server_name,
        tier=tier,
        registry=package_ref.registry.value,
        package_name=metadata.get("package_name") or package_ref.display_name,
        version=metadata.get("version") or package_ref.version,
        publisher=primary_publisher,
        publishers=publisher_names,
        first_published_at=_isoformat_or_none(first_published),
        last_published_at=_isoformat_or_none(last_published),
        weekly_downloads=weekly_downloads,
        repo_url=repo_url,
        repo_stars=_coerce_int(repo_signals.get("stars")),
        repo_contributors=repo_contributors,
        repo_last_commit_at=_isoformat_or_none(repo_last_commit),
        package_age_days=package_age_days,
        trust_summary=_trust_summary(
            package_age_days=package_age_days,
            weekly_downloads=weekly_downloads,
            repo_active=repo_active,
            repo_contributors=repo_contributors,
        ),
        reason=reason,
    )


def _official_decision(
    server_name: str,
    *,
    registry: PackageRegistry,
    package_name: str | None,
    version: str | None,
    publisher: str | None,
    publishers: list[str],
    reason: str,
    first_published_at: str | None = None,
    last_published_at: str | None = None,
    weekly_downloads: int | None = None,
    repo_url: str | None = None,
    repo_stars: int | None = None,
    repo_contributors: int | None = None,
    repo_last_commit_at: str | None = None,
    package_age_days: int | None = None,
    trust_summary: str | None = None,
) -> MCPTrustDecision:
    return MCPTrustDecision(
        server_name=server_name,
        tier=MCPTrustTier.OFFICIAL,
        registry=registry.value,
        package_name=package_name,
        version=version,
        publisher=publisher,
        publishers=publishers,
        first_published_at=first_published_at,
        last_published_at=last_published_at,
        weekly_downloads=weekly_downloads,
        repo_url=repo_url,
        repo_stars=repo_stars,
        repo_contributors=repo_contributors,
        repo_last_commit_at=repo_last_commit_at,
        package_age_days=package_age_days,
        trust_summary=trust_summary or "allowlisted publisher or org",
        reason=reason,
    )


def _unknown_decision(
    server_name: str,
    *,
    registry: PackageRegistry,
    reason: str,
    package_name: str | None = None,
    version: str | None = None,
) -> MCPTrustDecision:
    return MCPTrustDecision(
        server_name=server_name,
        tier=MCPTrustTier.UNKNOWN,
        registry=registry.value,
        package_name=package_name,
        version=version,
        trust_summary="insufficient trust signals",
        reason=reason,
    )


async def _fetch_npm_metadata(
    package_ref: MCPPackageRef,
    *,
    fetch_json: Any,
) -> dict[str, Any]:
    package_name = package_ref.lookup_name
    assert package_name is not None
    package_data = await fetch_json(
        f"https://registry.npmjs.org/{quote(package_name, safe='@/')}"
    )
    version = package_ref.version or package_data.get("dist-tags", {}).get("latest")
    version_data = (package_data.get("versions") or {}).get(version or "", {})
    time_data = package_data.get("time") or {}
    publishers = _extract_npm_publishers(version_data, package_data)
    return {
        "package_name": package_name,
        "version": version,
        "publishers": publishers,
        "first_published_at": time_data.get("created"),
        "last_published_at": time_data.get(version or "") or time_data.get("modified"),
        "weekly_downloads": (
            await fetch_json(
                f"https://api.npmjs.org/downloads/point/last-week/{quote(package_name, safe='@/')}"
            )
        ).get("downloads"),
        "repo_url": _normalize_repo_url(
            _first_nonempty(
                (version_data.get("repository") or {}).get("url"),
                (package_data.get("repository") or {}).get("url"),
                package_data.get("homepage"),
            )
        ),
    }


async def _fetch_pypi_metadata(
    package_ref: MCPPackageRef,
    *,
    fetch_json: Any,
) -> dict[str, Any]:
    package_name = package_ref.lookup_name
    assert package_name is not None
    package_data = await fetch_json(f"https://pypi.org/pypi/{quote(package_name)}/json")
    info = package_data.get("info") or {}
    releases = package_data.get("releases") or {}
    version = package_ref.version or info.get("version")
    first_published = _first_release_timestamp(releases)
    last_published = _latest_release_timestamp(releases.get(version or "") or [])
    if last_published is None:
        last_published = _latest_release_timestamp(
            [item for release in releases.values() for item in release]
        )
    pypi_stats = await fetch_json(
        f"https://pypistats.org/api/packages/{quote(package_name)}/recent"
    )
    project_urls = info.get("project_urls") or {}
    publishers = [
        name
        for name in (
            info.get("maintainer"),
            info.get("author"),
        )
        if name
    ]
    return {
        "package_name": package_name,
        "version": version,
        "publishers": publishers,
        "first_published_at": first_published,
        "last_published_at": last_published,
        "weekly_downloads": ((pypi_stats.get("data") or {}).get("last_week")),
        "repo_url": _normalize_repo_url(
            _first_nonempty(
                project_urls.get("Source"),
                project_urls.get("Repository"),
                project_urls.get("Homepage"),
                info.get("home_page"),
            )
        ),
    }


async def _fetch_repo_signals(
    repo_url: str | None,
    *,
    fetch_json: Any,
) -> dict[str, Any]:
    if not repo_url:
        return {}
    repo_match = _GITHUB_REPO_RE.match(repo_url.strip())
    if repo_match is None:
        return {"url": repo_url}
    owner = repo_match.group("owner")
    repo = repo_match.group("repo")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repo_data = await fetch_json(
        f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}",
        headers=headers,
    )
    contributors_data = await fetch_json(
        f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contributors?per_page=100&anon=1",
        headers=headers,
    )
    contributors = (
        len(contributors_data)
        if isinstance(contributors_data, list)
        else None
    )
    return {
        "url": repo_url,
        "owner": owner.lower(),
        "stars": _coerce_int(repo_data.get("stargazers_count")),
        "contributors": contributors,
        "last_commit_at": repo_data.get("pushed_at"),
    }


async def _fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    timeout = aiohttp.ClientTimeout(total=10)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(url, headers=headers) as response,
    ):
        response.raise_for_status()
        return await response.json()


def _extract_package_ref(server_config: dict[str, Any] | None) -> MCPPackageRef | None:
    if not isinstance(server_config, dict):
        return None
    command = str(server_config.get("command") or "").strip()
    args = [str(arg) for arg in server_config.get("args") or []]
    if not command:
        return None
    if command in {"npx", "pnpm"}:
        spec = _extract_npm_spec(command, args)
        name, version = _split_npm_spec(spec)
        return MCPPackageRef(
            registry=PackageRegistry.NPM,
            lookup_name=name,
            display_name=name or spec,
            version=version,
        )
    if command == "uvx":
        spec = _extract_uvx_spec(args)
        name, version = _split_pypi_spec(spec)
        return MCPPackageRef(
            registry=PackageRegistry.PYPI,
            lookup_name=name,
            display_name=name or spec,
            version=version,
        )
    if command == "pip":
        spec = _extract_pip_spec(args)
        name, version = _split_pypi_spec(spec)
        return MCPPackageRef(
            registry=PackageRegistry.PYPI,
            lookup_name=name,
            display_name=name or spec,
            version=version,
        )
    if command == "uv":
        spec = _extract_uv_tool_spec(args)
        name, version = _split_pypi_spec(spec)
        return MCPPackageRef(
            registry=PackageRegistry.PYPI,
            lookup_name=name,
            display_name=name or spec,
            version=version,
        )
    return None


def _extract_npm_spec(command: str, args: list[str]) -> str | None:
    filtered = [arg for arg in args if arg]
    if command == "pnpm" and filtered and filtered[0] == "dlx":
        filtered = filtered[1:]
    index = 0
    while index < len(filtered) and filtered[index].startswith("-"):
        if filtered[index] in {"-p", "--package"} and index + 1 < len(filtered):
            return filtered[index + 1]
        index += 1
    return filtered[index] if index < len(filtered) else None


def _extract_uvx_spec(args: list[str]) -> str | None:
    return _first_non_flag(args)


def _extract_pip_spec(args: list[str]) -> str | None:
    filtered = [arg for arg in args if arg]
    if filtered and filtered[0] == "install":
        filtered = filtered[1:]
    return _first_non_flag(filtered)


def _extract_uv_tool_spec(args: list[str]) -> str | None:
    filtered = [arg for arg in args if arg]
    if filtered[:2] == ["tool", "run"] or filtered[:2] == ["tool", "install"]:
        filtered = filtered[2:]
    return _first_non_flag(filtered)


def _first_non_flag(args: list[str]) -> str | None:
    for arg in args:
        if arg and not arg.startswith("-"):
            return arg
    return None


def _split_npm_spec(spec: str | None) -> tuple[str | None, str | None]:
    if not spec:
        return None, None
    if spec.startswith("@"):
        at_index = spec.rfind("@")
        if at_index > 0 and "/" in spec[:at_index]:
            return spec[:at_index], spec[at_index + 1 :] or None
        return spec, None
    if "@" not in spec:
        return spec, None
    package_name, version = spec.rsplit("@", 1)
    return package_name or None, version or None


def _split_pypi_spec(spec: str | None) -> tuple[str | None, str | None]:
    if not spec:
        return None, None
    match = _PYPI_STATS_PACKAGE_RE.match(spec)
    if match is None:
        return None, None
    return match.group("name"), match.group("version")


def _extract_npm_publishers(
    version_data: dict[str, Any],
    package_data: dict[str, Any],
) -> list[str]:
    publishers: list[str] = []
    publisher = (version_data.get("publisher") or {}).get("name")
    if publisher:
        publishers.append(str(publisher))
    for maintainer in version_data.get("maintainers") or package_data.get("maintainers") or []:
        name = maintainer.get("name") if isinstance(maintainer, dict) else None
        if name:
            publishers.append(str(name))
    deduped: list[str] = []
    seen: set[str] = set()
    for publisher_name in publishers:
        normalized = _normalize_publisher(publisher_name)
        if normalized and normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
    return deduped


def _is_official_package(
    *,
    package_ref: MCPPackageRef,
    publisher_names: list[str],
    repo_owner: str | None,
    trusted_publishers: dict[str, set[str]],
) -> bool:
    allowlisted_publishers = trusted_publishers.get("publishers", set())
    if any(name in allowlisted_publishers for name in publisher_names):
        return True
    scopes = trusted_publishers.get("scopes", set())
    if package_ref.registry == PackageRegistry.NPM and package_ref.lookup_name:
        for scope in scopes:
            if package_ref.lookup_name.startswith(scope):
                return True
    github_orgs = trusted_publishers.get("github_orgs", set())
    return bool(repo_owner and repo_owner in github_orgs)


def _trust_summary(
    *,
    package_age_days: int | None,
    weekly_downloads: int | None,
    repo_active: bool,
    repo_contributors: int | None,
) -> str:
    age_text = f"age={package_age_days}d" if package_age_days is not None else "age=unknown"
    downloads_text = (
        f"downloads={weekly_downloads}/week"
        if weekly_downloads is not None
        else "downloads=unknown"
    )
    contributors_text = (
        f"contributors={repo_contributors}"
        if repo_contributors is not None
        else "contributors=unknown"
    )
    repo_text = "repo_active=yes" if repo_active else "repo_active=no"
    return ", ".join((age_text, downloads_text, contributors_text, repo_text))


def _normalize_repo_url(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("git+"):
        text = text[4:]
    if text.startswith("git://"):
        text = "https://" + text[6:]
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text[len("git@github.com:") :]
    return text[:-4] if text.endswith(".git") else text


def _first_release_timestamp(releases: dict[str, list[dict[str, Any]]]) -> str | None:
    timestamps: list[str] = []
    for release_files in releases.values():
        for item in release_files:
            timestamp = item.get("upload_time_iso_8601") or item.get("upload_time")
            if timestamp:
                timestamps.append(str(timestamp))
    return min(timestamps) if timestamps else None


def _latest_release_timestamp(files: list[dict[str, Any]]) -> str | None:
    timestamps = [
        str(item.get("upload_time_iso_8601") or item.get("upload_time"))
        for item in files
        if item.get("upload_time_iso_8601") or item.get("upload_time")
    ]
    return max(timestamps) if timestamps else None


def _load_trusted_publishers(home: Path | None) -> dict[str, dict[str, set[str]]]:
    builtin_path = files("engram.permissions").joinpath("trusted_publishers.yaml")
    builtin = yaml.safe_load(builtin_path.read_text(encoding="utf-8")) or {}
    overlay_path = paths.state_dir(home) / TRUSTED_PUBLISHERS_OVERLAY_FILE
    overlay = _load_yaml_mapping(overlay_path) if overlay_path.exists() else {}
    merged: dict[str, dict[str, set[str]]] = {}
    for registry_name in {"npm", "pypi"}:
        registry_data: dict[str, set[str]] = {}
        for key in {"publishers", "scopes", "github_orgs"}:
            values: set[str] = set()
            for raw in (
                (builtin.get(registry_name) or {}).get(key) or [],
                (overlay.get(registry_name) or {}).get(key) or [],
            ):
                values.update(
                    _normalize_publisher(value)
                    for value in raw
                    if _normalize_publisher(value)
                )
            registry_data[key] = values
        merged[registry_name] = registry_data
    return merged


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.warning("mcp_trust.overlay_invalid path=%s", path, exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_publisher(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _cache_path(home: Path | None) -> Path:
    return paths.state_dir(home) / MCP_TRUST_CACHE_FILE


def _cache_key(
    server_name: str,
    server_config: dict[str, Any] | None,
    trusted_publishers: dict[str, dict[str, set[str]]],
) -> str:
    trusted_signature = {
        registry: {
            key: sorted(values)
            for key, values in sections.items()
        }
        for registry, sections in trusted_publishers.items()
    }
    return json.dumps(
        {
            "server_name": server_name,
            "server_config": server_config,
            "trusted_publishers": trusted_signature,
        },
        sort_keys=True,
    )


def _load_cached_decision(
    cache_key: str,
    *,
    home: Path | None,
    now: datetime,
) -> MCPTrustDecision | None:
    cache_path = _cache_path(home)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("mcp_trust.cache_invalid path=%s", cache_path, exc_info=True)
        return None
    entry = (payload.get("entries") or {}).get(cache_key)
    if not isinstance(entry, dict):
        return None
    fetched_at = _parse_datetime(entry.get("fetched_at"))
    if fetched_at is None or now - fetched_at > MCP_TRUST_CACHE_TTL:
        return None
    decision_payload = entry.get("decision")
    if not isinstance(decision_payload, dict):
        return None
    return MCPTrustDecision.from_cache_payload(decision_payload)


def _store_cached_decision(
    cache_key: str,
    decision: MCPTrustDecision,
    *,
    home: Path | None,
    now: datetime,
) -> None:
    cache_path = _cache_path(home)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"entries": {}}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
            payload = loaded
    payload.setdefault("entries", {})[cache_key] = {
        "fetched_at": now.isoformat(),
        "decision": decision.to_cache_payload(),
    }
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _display_date(value: str | None) -> str:
    if value is None:
        return "unknown"
    parsed = _parse_datetime(value)
    if parsed is None:
        return value
    return parsed.date().isoformat()


def _display_downloads(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unknown"


def _display_repo(url: str | None, stars: int | None) -> str:
    if not url:
        return "unknown"
    if stars is None:
        return url
    return f"{url} ({stars:,}★)"
