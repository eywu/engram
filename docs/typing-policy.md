# Typing Policy

This policy applies to `src/engram`. Tests remain permissive unless a ticket says
otherwise.

## Mypy configuration

Mypy settings live in `pyproject.toml` under `[tool.mypy]`. Keep changes narrow:
prefer per-module overrides for third-party gaps over weakening checks globally.

Run the gate locally with:

```bash
uv run mypy src/engram
```

## Choosing an escape hatch

Use `assert` when the code has performed a runtime check that narrows a value and
the invariant should fail loudly if violated.

Use `cast()` when the type checker cannot infer a value that is already guaranteed
by surrounding code or a third-party contract. Keep casts close to the boundary.

Use `# type: ignore[...]` only when neither runtime narrowing nor `cast()` can
express the situation cleanly, especially for incorrect or incomplete third-party
stubs.

## Ignore comments

Every ignore must include the mypy error code and a justification:

```python
value = sdk.value  # type: ignore[attr-defined]  # SDK stubs omit runtime field.
```

If the ignore works around a known upstream SDK bug, include the upstream issue or
PR URL in the comment. Otherwise include a one-line reason that explains why the
ignore is safe.

## Dependency stubs

When adding a dependency, check whether it ships inline types. If mypy reports
missing imports for a package with external stubs, add the matching `types-X`
package to the `dev` extra in `pyproject.toml`, then run:

```bash
uv sync --extra dev
uv run mypy src/engram
```

Commit the updated `uv.lock` with the dependency change.

## Strict mode

`--strict` is out of scope for now. Evaluate it in a follow-up after this CI gate
lands cleanly.
