"""Every module in the package compiles, including the ones nothing imports.

A single syntax error fails at import — and that takes down everything importing
the module, which is how one bad clause in the SSO package would break the whole
provider registry. Neither ruff nor mypy nor a test run necessarily touches a
module that no test imports, so the guard is the interpreter's own verdict on
every source file.

``compile()`` and not a pattern match on the text: the interpreter is the only
authority on what is valid Python, and a regex that tries to recognise a broken
clause ends up flagging correct code (a parenthesised multi-type ``except`` is
four tokens away from the Python-2 form that is forbidden).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "rootcoz"

_SOURCES = sorted(PACKAGE.rglob("*.py"))


def test_the_package_has_sources_to_check() -> None:
    """A guard on the guard: an empty glob would make the test below vacuous."""
    assert len(_SOURCES) > 50


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_every_module_compiles(path: Path) -> None:
    """Compile the file as the interpreter would at import time.

    ``compile`` raises ``SyntaxError`` with the offending line, so a failure here
    names the file and the line rather than a count of suspicious text.
    """
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
