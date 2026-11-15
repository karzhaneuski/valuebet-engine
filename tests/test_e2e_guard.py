"""Guard test enforcing that E2E integration tests do not construct ORM objects directly."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_NAMES = {"OddsSnapshot", "MatchStat", "Result"}
TESTS_DIR = Path(__file__).resolve().parent


def _check_file_for_forbidden_construction(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    # Track alias mappings: e.g. from x import OddsSnapshot as OS -> OS maps to OddsSnapshot
    # or import x as y -> y.OddsSnapshot
    # local name -> canonical forbidden name
    aliases: dict[str, str] = {}
    modules: dict[str, str] = {}  # alias -> module

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                orig = alias.name
                as_name = alias.asname or alias.name
                if orig in FORBIDDEN_NAMES:
                    aliases[as_name] = orig
        elif isinstance(node, ast.Import):
            for alias in node.names:
                as_name = alias.asname or alias.name
                modules[as_name] = alias.name

    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            target_name: str | None = None
            if isinstance(func, ast.Name):
                if func.id in aliases:
                    target_name = aliases[func.id]
                elif func.id in FORBIDDEN_NAMES:
                    target_name = func.id
            elif isinstance(func, ast.Attribute):
                # e.g. orm.OddsSnapshot or core.orm.OddsSnapshot
                attr_name = func.attr
                if attr_name in FORBIDDEN_NAMES:
                    target_name = attr_name

            if target_name is not None:
                violations.append(
                    f"{path.name}:{node.lineno}: direct construction of {target_name}"
                )

    return violations


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(p, id=p.name)
        for p in sorted(TESTS_DIR.glob("test_e2e_*.py"))
    ]
    + [pytest.param(TESTS_DIR / "e2e_support.py", id="e2e_support.py")],
)
def test_e2e_guard(path: Path) -> None:
    violations = _check_file_for_forbidden_construction(path)
    assert not violations, (
        f"Forbidden direct constructions found in {path.name}:\n" + "\n".join(violations)
    )


