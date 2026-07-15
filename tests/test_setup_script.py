"""Tests for scripts/setup.py helpers."""

import importlib.util
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_SETUP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
_SPEC = importlib.util.spec_from_file_location("rootcoz_setup", _SETUP_PATH)
assert _SPEC and _SPEC.loader
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)


def test_yaml_scalar_quotes_special_chars() -> None:
    assert setup._yaml_scalar("hello") == "hello"
    assert setup._yaml_scalar("key:val").startswith('"')
    assert setup._yaml_scalar("key:val").endswith('"')


def test_yaml_scalar_quotes_reserved_words() -> None:
    for word in ("true", "false", "yes", "no", "on", "off", "null", "~"):
        assert setup._yaml_scalar(word) == f'"{word}"'
    # Case-insensitive
    assert setup._yaml_scalar("True") == '"True"'
    assert setup._yaml_scalar("FALSE") == '"FALSE"'


def test_yaml_scalar_quotes_numeric_strings() -> None:
    assert setup._yaml_scalar("123") == '"123"'
    assert setup._yaml_scalar("3.14") == '"3.14"'
    assert setup._yaml_scalar("0") == '"0"'


def test_yaml_scalar_multiline_block_scalar() -> None:
    result = setup._yaml_scalar("line1\nline2\nline3", indent=0)
    assert result.startswith("|\n")
    # At indent=0, content should be indented by 2 spaces (one level deeper)
    lines = result.split("\n")[1:]
    for line in lines:
        assert line.startswith("  ")

    # At indent=2, content should be indented by 6 spaces
    result2 = setup._yaml_scalar("line1\nline2", indent=2)
    lines2 = result2.split("\n")[1:]
    for line in lines2:
        assert line.startswith("      ")


# ---------------------------------------------------------------------------
# _collect_routing tests
# ---------------------------------------------------------------------------


def test_collect_routing_openshift() -> None:
    """OpenShift: route enabled, ingress disabled, hostname collected."""
    user_inputs = iter(["openshift", "rootcoz.apps.mycluster.com"])
    with patch("builtins.input", side_effect=user_inputs):
        result = setup._collect_routing()

    assert result["route"]["enabled"] is True
    assert result["route"]["host"] == "rootcoz.apps.mycluster.com"
    assert result["ingress"]["enabled"] is False


def test_collect_routing_kubernetes() -> None:
    """Kubernetes: ingress enabled with host, class, and TLS secret."""
    user_inputs = iter(
        [
            "kubernetes",
            "rootcoz.example.com",
            "nginx",  # ingress class (default accepted)
            "rootcoz-tls",  # TLS secret (default accepted)
        ]
    )
    with patch("builtins.input", side_effect=user_inputs):
        result = setup._collect_routing()

    assert result["route"]["enabled"] is False
    assert result["ingress"]["enabled"] is True
    assert result["ingress"]["host"] == "rootcoz.example.com"
    assert result["ingress"]["className"] == "nginx"
    assert result["ingress"]["tls"]["enabled"] is True
    assert result["ingress"]["tls"]["secretName"] == "rootcoz-tls"  # pragma: allowlist secret  # fmt: skip


def test_collect_routing_kubernetes_custom_ingress_class() -> None:
    """Kubernetes with a non-default ingress class."""
    user_inputs = iter(
        [
            "kubernetes",
            "rootcoz.example.com",
            "traefik",
            "my-tls-secret",
        ]
    )
    with patch("builtins.input", side_effect=user_inputs):
        result = setup._collect_routing()

    assert result["ingress"]["className"] == "traefik"
    assert result["ingress"]["tls"]["secretName"] == "my-tls-secret"  # pragma: allowlist secret  # fmt: skip


def test_collect_routing_clusterip() -> None:
    """ClusterIP: both route and ingress disabled, no hostname asked."""
    user_inputs = iter(["clusterip"])
    with patch("builtins.input", side_effect=user_inputs):
        result = setup._collect_routing()

    assert result["route"]["enabled"] is False
    assert result["ingress"]["enabled"] is False
    assert "host" not in result["route"]
    assert "host" not in result["ingress"]


def test_collect_routing_openshift_missing_host_raises() -> None:
    """OpenShift with blank hostname raises ValueError."""
    user_inputs = iter(["openshift", ""])  # blank hostname
    with (
        patch("builtins.input", side_effect=user_inputs),
        pytest.raises(ValueError, match="(?i)hostname"),
    ):
        setup._collect_routing()


def test_collect_routing_invalid_then_valid_choice() -> None:
    """Invalid choice is re-prompted until valid."""
    # "docker" is invalid, then "clusterip" is valid
    user_inputs = iter(["docker", "clusterip"])
    with patch("builtins.input", side_effect=user_inputs):
        result = setup._collect_routing()

    assert result["route"]["enabled"] is False
    assert result["ingress"]["enabled"] is False


# ---------------------------------------------------------------------------
# _collect_ai tests
# ---------------------------------------------------------------------------


def test_collect_ai_gemini() -> None:
    """Gemini provider: collects API key via getpass."""
    user_inputs = iter(["gemini", ""])  # provider, model (accept default)
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value="gem-key-123"),
    ):
        generated, secrets = setup._collect_ai()

    assert generated["ai"]["provider"] == "gemini"
    assert generated["ai"]["model"] == "gemini-2.5-pro"
    assert secrets["ai"]["geminiApiKey"] == "gem-key-123"  # pragma: allowlist secret


def test_collect_ai_gemini_custom_model() -> None:
    """Gemini provider with a custom model name."""
    user_inputs = iter(["gemini", "gemini-2.5-flash"])
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value="gem-key-456"),
    ):
        generated, secrets = setup._collect_ai()

    assert generated["ai"]["model"] == "gemini-2.5-flash"


def test_collect_ai_claude_direct_api() -> None:
    """Claude provider without Vertex (direct Anthropic API key)."""
    # provider, model (default), vertex? -> "n"
    user_inputs = iter(["claude", "", "n"])
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value="sk-ant-key"),
    ):
        generated, secrets = setup._collect_ai()

    assert generated["ai"]["provider"] == "claude"
    assert generated["ai"]["model"] == "claude-sonnet-4-20250514"
    assert generated["ai"]["vertex"]["enabled"] is False
    assert secrets["ai"]["anthropicApiKey"] == "sk-ant-key"  # pragma: allowlist secret  # fmt: skip


def test_collect_ai_claude_vertex() -> None:
    """Claude provider via Google Vertex AI."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"type": "service_account"}')
        sa_path = f.name
    try:
        # provider, model, vertex? "y", project, region, sa path
        user_inputs = iter(["claude", "", "y", "my-gcp-proj", "us-east5", sa_path])
        with (
            patch("builtins.input", side_effect=user_inputs),
        ):
            generated, secrets = setup._collect_ai()

        assert generated["ai"]["vertex"]["enabled"] is True
        assert generated["ai"]["vertex"]["projectId"] == "my-gcp-proj"
        assert generated["ai"]["vertex"]["region"] == "us-east5"
        assert (
            '{"type": "service_account"}'
            in secrets["ai"]["vertex"]["serviceAccountKey"]
        )
    finally:
        os.unlink(sa_path)


def test_collect_ai_cursor_api_key() -> None:
    """Cursor provider with API key only."""
    user_inputs = iter(["cursor", "", ""])  # provider, model, auth.json path (empty)
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value="cursor-key-789"),
    ):
        generated, secrets = setup._collect_ai()

    assert generated["ai"]["provider"] == "cursor"
    assert secrets["ai"]["cursor"]["apiKey"] == "cursor-key-789"  # pragma: allowlist secret  # fmt: skip


def test_collect_ai_cursor_no_creds_raises() -> None:
    """Cursor with no API key and no auth.json raises ValueError."""
    user_inputs = iter(["cursor", "", ""])  # provider, model, auth.json path (empty)
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value=""),  # blank key
        pytest.raises(ValueError, match="(?i)cursor"),
    ):
        setup._collect_ai()


def test_collect_ai_gemini_empty_key_raises() -> None:
    """Gemini with blank API key raises ValueError."""
    user_inputs = iter(["gemini", ""])
    with (
        patch("builtins.input", side_effect=user_inputs),
        patch("getpass.getpass", return_value=""),
        pytest.raises(ValueError, match="(?i)required"),
    ):
        setup._collect_ai()


# ---------------------------------------------------------------------------
# _write_values tests
# ---------------------------------------------------------------------------


def test_write_values_non_secret_permissions() -> None:
    """Non-secret file is written with 0644 permissions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.generated.yaml"
        data = {"route": {"enabled": True, "host": "test.example.com"}}

        setup._write_values(path, data, secret=False)

        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o644
        content = path.read_text()
        assert "route:" in content
        assert "host: test.example.com" in content


def test_write_values_secret_permissions() -> None:
    """Secret file is written with 0600 permissions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.secrets.yaml"
        data = {"ai": {"geminiApiKey": "super-secret"}}  # pragma: allowlist secret

        setup._write_values(path, data, secret=True)

        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
        content = path.read_text()
        assert "geminiApiKey: super-secret" in content


def test_write_values_content_roundtrip() -> None:
    """Verify full nested structure is written correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.yaml"
        data = {
            "route": {"enabled": True, "host": "rootcoz.apps.cluster.com"},
            "ingress": {"enabled": False, "tls": {"enabled": False}},
            "ai": {"provider": "gemini", "model": "gemini-2.5-pro"},
        }

        setup._write_values(path, data, secret=False)

        content = path.read_text()
        assert "enabled: true" in content
        assert "enabled: false" in content
        assert "provider: gemini" in content
        assert "model: gemini-2.5-pro" in content


def test_write_values_overwrites_existing() -> None:
    """Writing to an existing file after confirming overwrites content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.yaml"
        path.write_text("old: content\n")

        with patch("builtins.input", return_value="y"):
            setup._write_values(path, {"new": "content"}, secret=False)

        content = path.read_text()
        assert "old" not in content
        assert "new: content" in content


def test_write_values_skips_on_decline() -> None:
    """Declining overwrite keeps the original file unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.yaml"
        path.write_text("old: content\n")

        with patch("builtins.input", return_value="n"):
            setup._write_values(path, {"new": "content"}, secret=False)

        content = path.read_text()
        assert "old: content" in content
        assert "new" not in content


def test_dump_yaml_nested_structure() -> None:
    rendered = setup._dump_yaml(
        {
            "route": {"enabled": True, "host": "rootcoz.example.com"},
            "ai": {"provider": "gemini", "model": "gemini-2.5-pro"},
        }
    )
    assert "route:" in rendered
    assert "enabled: true" in rendered
    assert "host: rootcoz.example.com" in rendered
    assert "provider: gemini" in rendered


def test_dump_yaml_nested_multiline_roundtrip() -> None:
    """A multiline value nested several levels deep round-trips through yaml.safe_load."""
    sa_key = '{\n  "type": "service_account",\n  "project_id": "my-proj"\n}'
    data = {
        "ai": {
            "vertex": {
                "serviceAccountKey": sa_key,
            }
        }
    }
    rendered = setup._dump_yaml(data)
    parsed = yaml.safe_load(rendered)
    # YAML block scalar '|' (clip mode) appends a trailing newline.
    assert parsed["ai"]["vertex"]["serviceAccountKey"] == sa_key + "\n"


def test_write_values_returns_false_on_decline() -> None:
    """_write_values returns False when user declines overwrite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "values.yaml"
        path.write_text("old: content\n")

        with patch("builtins.input", return_value="n"):
            result = setup._write_values(path, {"new": "content"}, secret=False)

        assert result is False
        assert "old: content" in path.read_text()
