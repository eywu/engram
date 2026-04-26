from __future__ import annotations

import tomllib
from pathlib import Path


def test_dev_tools_live_in_dependency_group_instead_of_project_extra():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert "dependency-groups" in pyproject
    assert "optional-dependencies" not in pyproject["project"]

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    dependency_names = {dependency.split(">=", 1)[0] for dependency in dev_dependencies}

    assert "ruff" in dependency_names
    assert "pytest" in dependency_names
    assert "pytest-asyncio" in dependency_names
    assert "pytest-cov" in dependency_names
    assert "mypy" in dependency_names
