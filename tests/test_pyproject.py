from __future__ import annotations

import tomllib
from pathlib import Path


def test_dev_dependency_group_includes_lint_and_test_tools():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    dependency_names = {dependency.split(">=", 1)[0] for dependency in dev_dependencies}

    assert "ruff" in dependency_names
    assert "pytest" in dependency_names
    assert "pytest-asyncio" in dependency_names
    assert "pytest-cov" in dependency_names
    assert "mypy" in dependency_names
