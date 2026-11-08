"""Guard: tox's own `{...}` replacer must not mangle shell brace groups (#469).

A `commands` string that contains a shell brace group opening as `{ ` is read by tox's
replacer (``tox/config/loader/replacer.py``, ``REPLACE_START="{"``,
``ARG_DELIMITER=":"``) as a ``{key:value}`` substitution, so the first ``:`` inside the
group truncates the command. A colon in a JSON payload is fine because `{"...":1}` is
not a brace group; an ``{env:HOME}`` substitution is fine because it opens with a key.
"""

import re
import tomllib
from pathlib import Path

TOX_TOML = Path(__file__).resolve().parent.parent / "tox.toml"
REASON = (
    "tox's {…} replacer reads ':' as a key:value delimiter and truncates the command "
    "on tox 4.67.4"
)


def _commands(data: dict) -> list[tuple[str, str, str]]:
    """(env, key, command string) for every commands/commands_pre entry."""
    found = []
    for env, cfg in data.get("env", {}).items():
        if not isinstance(cfg, dict):
            continue
        for key in ("commands", "commands_pre"):
            for entry in cfg.get(key, []):
                for command in [entry] if isinstance(entry, str) else entry:
                    if isinstance(command, str):
                        found.append((env, key, command))
    return found


def _brace_groups(command: str) -> list[str]:
    """Shell brace groups: `{` + whitespace, matched to the balancing `}`."""
    groups = []
    for match in re.finditer(r"\{\s", command):
        depth = 0
        for index in range(match.start(), len(command)):
            depth += {"{": 1, "}": -1}.get(command[index], 0)
            if depth == 0:
                groups.append(command[match.start() : index + 1])
                break
    return groups


def test_no_colon_inside_shell_brace_group_in_tox_commands():
    data = tomllib.loads(TOX_TOML.read_text())
    offenders = [
        f"{TOX_TOML.name} [{env}] {key}: {group!r}\n    reason: {REASON}"
        for env, key, command in _commands(data)
        for group in _brace_groups(command)
        if ":" in group
    ]
    assert not offenders, "colon inside a shell brace group:\n" + "\n".join(offenders)


def test_brace_group_detection_ignores_tox_substitutions_and_json():
    """The detector must find shell groups only, so it can never false-positive."""
    assert _brace_groups("printf 'x:\n'") == []  # no group at all
    assert _brace_groups("{env:HOME}/bin:{env:PATH}") == []  # tox substitutions
    assert _brace_groups("--set-json 'x={\"a.b/GPU_2\":1}'") == []  # JSON payload
    assert _brace_groups("{ printf 'x:' ; }") == ["{ printf 'x:' ; }"]
    # Nested groups are reported inner and outer, so neither can hide a colon.
    assert _brace_groups("{ a || { b:; }; }") == ["{ a || { b:; }; }", "{ b:; }"]
