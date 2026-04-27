from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGRAM_SRC = PROJECT_ROOT / "src" / "engram"
MANIFEST_PATH = ENGRAM_SRC / "manifest.py"


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _manifest_helper_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    direct_names: set[str] = set()
    module_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "engram" or module.endswith(".engram"):
                for alias in node.names:
                    if alias.name == "manifest":
                        module_names.add(alias.asname or alias.name)
            if module == "manifest" or module.endswith(".manifest"):
                for alias in node.names:
                    if alias.name == "_persist_manifest_update":
                        direct_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(".manifest") and alias.asname is not None:
                    module_names.add(alias.asname)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue

            if value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue

            target_name = targets[0].id
            if target_name in direct_names:
                continue

            value_name = _dotted_name(value)
            if value_name is None:
                continue
            if value_name == "_persist_manifest_update" or value_name in direct_names:
                direct_names.add(target_name)
                changed = True
                continue
            if not value_name.endswith("._persist_manifest_update"):
                continue
            prefix = value_name.removesuffix("._persist_manifest_update")
            if prefix in module_names or value_name.endswith("manifest._persist_manifest_update"):
                direct_names.add(target_name)
                changed = True

    return direct_names, module_names


def _is_manifest_update_call(
    call: ast.Call,
    *,
    direct_names: set[str],
    module_names: set[str],
) -> bool:
    dotted_name = _dotted_name(call.func)
    if dotted_name is None:
        return False
    if dotted_name == "_persist_manifest_update" or dotted_name in direct_names:
        return True
    if not dotted_name.endswith("._persist_manifest_update"):
        return False
    prefix = dotted_name.removesuffix("._persist_manifest_update")
    return prefix in module_names or dotted_name.endswith(
        "manifest._persist_manifest_update"
    )


def _assigned_name_values(tree: ast.AST) -> dict[str, list[ast.AST]]:
    assignments: dict[str, list[ast.AST]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue

        if value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue

        assignments.setdefault(targets[0].id, []).append(value)

    return assignments


def _resolved_value_candidates(
    node: ast.AST,
    *,
    assignments: dict[str, list[ast.AST]],
    seen_names: frozenset[str] = frozenset(),
) -> list[ast.AST]:
    if not isinstance(node, ast.Name):
        return [node]

    if node.id in seen_names:
        return [node]

    values = assignments.get(node.id)
    if not values:
        return [node]

    candidates: list[ast.AST] = []
    next_seen = seen_names | {node.id}
    for value in values:
        candidates.extend(
            _resolved_value_candidates(
                value,
                assignments=assignments,
                seen_names=next_seen,
            )
        )
    return candidates or [node]


def _approved_additions_candidates_from_mapping(
    mapping: ast.Dict,
    *,
    assignments: dict[str, list[ast.AST]],
) -> list[ast.AST]:
    candidates: list[ast.AST] = []

    for key, value in zip(mapping.keys, mapping.values, strict=False):
        if not (isinstance(key, ast.Constant) and key.value == "approved_mcp_additions"):
            continue
        candidates.extend(
            _resolved_value_candidates(
                value,
                assignments=assignments,
            )
        )
    return candidates


def _approved_additions_candidates_for_call(
    call: ast.Call,
    *,
    assignments: dict[str, list[ast.AST]],
) -> list[ast.AST]:
    candidates: list[ast.AST] = []

    for keyword in call.keywords:
        if keyword.arg == "approved_mcp_additions":
            candidates.extend(
                _resolved_value_candidates(
                    keyword.value,
                    assignments=assignments,
                )
            )
            continue

        if keyword.arg is not None:
            continue

        for value in _resolved_value_candidates(
            keyword.value,
            assignments=assignments,
        ):
            if not isinstance(value, ast.Dict):
                continue
            candidates.extend(
                _approved_additions_candidates_from_mapping(
                    value,
                    assignments=assignments,
                )
            )

    return candidates


def _find_external_approved_mcp_manifest_updates(
    project_root: Path,
) -> list[str]:
    engram_src = project_root / "src" / "engram"
    manifest_path = engram_src / "manifest.py"
    offenders: list[str] = []

    for path in sorted(engram_src.rglob("*.py")):
        if path == manifest_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        direct_names, module_names = _manifest_helper_aliases(tree)
        assignments = _assigned_name_values(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_manifest_update_call(
                node,
                direct_names=direct_names,
                module_names=module_names,
            ):
                continue
            approved_additions = _approved_additions_candidates_for_call(
                node,
                assignments=assignments,
            )
            if not approved_additions:
                continue
            if all(
                isinstance(value, ast.Constant) and value.value is None
                for value in approved_additions
            ):
                continue
            offenders.append(f"{path.relative_to(project_root)}:{node.lineno}")

    return offenders


def test_manifest_ci_guard_detects_external_approved_mcp_updates(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    engram_src = project_root / "src" / "engram"
    engram_src.mkdir(parents=True)
    (engram_src / "manifest.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def internal_ok(manifest, path):
                _persist_manifest_update(
                    manifest,
                    path,
                    approved_mcp_additions=["camoufox"],
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "good.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def allowed_none(manifest, path):
                _persist_manifest_update(
                    manifest,
                    path,
                    approved_mcp_additions=None,
                )


            def missing_keyword(manifest, path):
                _persist_manifest_update(manifest, path)
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "good_named_none.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def allowed_none_alias(manifest, path):
                approved = None
                kwargs = {"approved_mcp_additions": approved}
                _persist_manifest_update(
                    manifest,
                    path,
                    **kwargs,
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "bad_direct.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update as persist_update


            def offender(manifest, path):
                persist_update(
                    manifest,
                    path,
                    approved_mcp_additions=["camoufox"],
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "bad_kwargs_alias.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def offender(manifest, path):
                approved = ["camoufox"]
                kwargs = {"approved_mcp_additions": approved}
                persist_kwargs = kwargs
                _persist_manifest_update(
                    manifest,
                    path,
                    **persist_kwargs,
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "bad_attr.py").write_text(
        dedent(
            """
            from engram import manifest as manifest_module


            def offender(manifest, path, additions):
                manifest_module._persist_manifest_update(
                    manifest,
                    path,
                    approved_mcp_additions=additions,
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "bad_assigned_alias.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def offender(manifest, path):
                persist_update = _persist_manifest_update
                persist_update(
                    manifest,
                    path,
                    approved_mcp_additions=["camoufox"],
                )
            """
        ),
        encoding="utf-8",
    )
    (engram_src / "bad_kwargs.py").write_text(
        dedent(
            """
            from engram.manifest import _persist_manifest_update


            def offender(manifest, path):
                _persist_manifest_update(
                    manifest,
                    path,
                    **{"approved_mcp_additions": ["camoufox"]},
                )
            """
        ),
        encoding="utf-8",
    )

    offenders = _find_external_approved_mcp_manifest_updates(project_root)

    assert len(offenders) == 5
    assert any(entry.startswith("src/engram/bad_direct.py:") for entry in offenders)
    assert any(entry.startswith("src/engram/bad_kwargs_alias.py:") for entry in offenders)
    assert any(entry.startswith("src/engram/bad_attr.py:") for entry in offenders)
    assert any(
        entry.startswith("src/engram/bad_assigned_alias.py:") for entry in offenders
    )
    assert any(entry.startswith("src/engram/bad_kwargs.py:") for entry in offenders)


def test_manifest_ci_guard_has_no_live_offenders() -> None:
    offenders = _find_external_approved_mcp_manifest_updates(PROJECT_ROOT)

    assert offenders == [], (
        "External callers must route approved MCP allow-list additions through "
        "persist_approved_mcp_manifest_change. Found disallowed "
        "_persist_manifest_update(..., approved_mcp_additions=...) call sites:\n"
        + "\n".join(offenders)
    )
