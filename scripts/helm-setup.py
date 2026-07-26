"""Interactive rootcoz Helm install helper.

Prompts for bootstrap settings, writes split values files under a user-chosen
directory outside the git repo (default ``~/.config/rootcoz/helm``), and runs
helm upgrade --install. On re-run, existing values files are offered as defaults.
Legacy values files polluted with terminal escape codes are sanitized on load.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "chart"
VALID_PROVIDERS = ("gemini", "claude", "cursor")
_ADMIN_KEY_MIN_LENGTH = 16
_GENERATED_FILENAME = "values.generated.yaml"
_SECRETS_FILENAME = "values.secrets.yaml"  # pragma: allowlist secret
_SETUP_META_FILENAME = "setup-meta.yaml"
_DEFAULT_OUTPUT_DIR = Path.home() / ".config" / "rootcoz" / "helm"
_DEFAULT_RELEASE = "rootcoz"
_DEFAULT_NAMESPACE = "rootcoz"

# CSI / OSC / other terminal escapes that getpass/input can capture on special keys
# (e.g. Insert → ESC [ 2 ~).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # other Fe escapes
)


def _strip_terminal_controls(value: str, *, keep: str = "\t\n") -> str:
    """Remove ANSI escapes and C0/C1 controls; keep selected whitespace chars.

    Also drops Unicode C1 controls (U+0080–U+009F), including 8-bit CSI (``\\x9b``).
    """
    cleaned = _ANSI_ESCAPE_RE.sub("", value)
    return "".join(
        ch
        for ch in cleaned
        if ch in keep or (ord(ch) >= 32 and not (0x80 <= ord(ch) <= 0x9F))
    )


def _sanitize_user_input(value: str) -> str:
    """Strip terminal escape sequences and C0 controls from interactive input."""
    return _strip_terminal_controls(value, keep="\t\n").strip()


def _sanitize_yaml_text(value: str) -> str:
    """Strip terminal escapes/C0 controls from on-disk YAML without trimming edges."""
    return _strip_terminal_controls(value, keep="\t\n\r")


def _prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = _sanitize_user_input(input(f"{message}{suffix}: "))
    return value or default


def _prompt_choice(message: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        value = _prompt(message, default).lower()
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}")


def _prompt_yes_no(message: str, default: bool = True) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        value = _sanitize_user_input(input(f"{message} ({default_label}): ")).lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n.")


def _prompt_secret(
    message: str,
    *,
    required: bool = True,
    existing: str = "",
) -> str:
    """Prompt for a secret. Blank keeps *existing* when set."""
    suffix = " [stored — leave blank to keep]" if existing else ""
    value = _sanitize_user_input(getpass.getpass(f"{message}{suffix}: "))
    if not value and existing:
        return existing
    if required and not value:
        raise ValueError(f"{message} is required")
    return value


def _nested_get(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    if cur is None:
        return default
    return cur


def _load_values_parse_error(path: Path, exc: Exception) -> ValueError:
    """Build a ValueError for unrecoverable YAML parse failures."""
    return ValueError(
        f"Failed to parse {path}: {exc}. "
        "The file may be corrupted (e.g. terminal escape codes in a secret). "
        "Delete it and re-run setup."
    )


def _load_values(path: Path) -> dict[str, Any]:
    """Load a previously written values YAML file, or {} if missing/empty.

    Files written before input sanitization may contain terminal escape codes
    (e.g. Insert → ESC[2~) that PyYAML rejects. Strip those, rewrite via
    ``_write_values`` when possible, and continue so re-runs can re-prompt for
    blanked secrets. If cleaned text still does not parse, raise with
    delete-and-re-run guidance.
    """
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        # PyYAML rejects embedded ESC/control chars, so parse failure is the
        # signal for the known Insert-key corruption mode.
        data = yaml.safe_load(raw)
    except yaml.YAMLError as first_exc:
        cleaned = _sanitize_yaml_text(raw)
        if cleaned == raw:
            raise _load_values_parse_error(path, first_exc) from first_exc
        try:
            data = yaml.safe_load(cleaned)
        except yaml.YAMLError as exc:
            raise _load_values_parse_error(path, exc) from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected a YAML mapping in {path}")
        secret = path.name == _SECRETS_FILENAME
        try:
            _write_values(path, data, secret=secret)
            rewrite_note = "and rewrote the file"
        except OSError as write_exc:
            rewrite_note = f"in memory only (could not rewrite file: {write_exc})"
        print(
            f"Warning: stripped terminal escape codes from {path} {rewrite_note}. "
            "Re-enter any blanked secrets when prompted.",
            file=sys.stderr,
        )
        return data
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def _read_file(path: str) -> str:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    return file_path.read_text(encoding="utf-8")


def _read_json_file(path: str) -> dict[str, Any]:
    content = _read_file(path)
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    """Intentionally dependency-free YAML serializer.

    Supports flat and nested dicts with string, numeric, and bool values.
    Limitations: no anchors/aliases, no flow style, no list values (dict-only).
    """

    def render(value: Any, indent: int = 0) -> list[str]:
        prefix = "  " * indent
        lines: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}{key}:")
                    lines.extend(render(item, indent + 1))
                elif isinstance(item, bool):
                    lines.append(f"{prefix}{key}: {'true' if item else 'false'}")
                elif item is None:
                    lines.append(f"{prefix}{key}: null")
                else:
                    lines.append(f"{prefix}{key}: {_yaml_scalar(item, indent)}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}-")
                    lines.extend(render(item, indent + 1))
                else:
                    lines.append(f"{prefix}- {_yaml_scalar(item)}")
        else:
            lines.append(f"{prefix}{_yaml_scalar(value)}")
        return lines

    return "\n".join(render(data)) + "\n"


# YAML reserved words that must be quoted to avoid misinterpretation.
_YAML_RESERVED = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "null",
        "~",
    }
)


def _yaml_scalar(value: Any, indent: int = 0) -> str:
    """Format a Python value as a safe YAML scalar string.

    Handles empty strings, strings containing YAML-special characters,
    multiline strings (emitted as block scalars ``|``), YAML boolean
    literals / null, and bare numeric strings by double-quoting them.
    """
    text = str(value)
    # Multiline -> YAML block scalar
    if "\n" in text:
        block_indent = "  " * (indent + 1)
        indented = "\n".join(block_indent + line for line in text.splitlines())
        return f"|\n{indented}"
    if text == "" or any(ch in text for ch in ":#{}[]&*!|>'\"@%`"):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    # Quote YAML reserved words (booleans, null) so they stay strings.
    if text.lower() in _YAML_RESERVED:
        return f'"{text}"'
    # Quote bare numeric strings so they aren't parsed as numbers.
    try:
        float(text)
        return f'"{text}"'
    except ValueError:
        pass
    return text


def _infer_cluster(existing: dict[str, Any]) -> str:
    """Derive cluster type from a previously saved generated values file."""
    route_enabled = bool(_nested_get(existing, "route", "enabled", default=False))
    ingress_enabled = bool(_nested_get(existing, "ingress", "enabled", default=False))
    if route_enabled:
        return "openshift"
    if ingress_enabled:
        return "kubernetes"
    if existing.get("route") is not None or existing.get("ingress") is not None:
        return "clusterip"
    return "openshift"


def _collect_routing(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    cluster = _prompt_choice(
        "Cluster type (openshift/kubernetes/clusterip)",
        ("openshift", "kubernetes", "clusterip"),
        _infer_cluster(existing),
    )
    generated: dict[str, Any] = {
        "route": {"enabled": cluster == "openshift"},
        "ingress": {
            "enabled": cluster == "kubernetes",
            "tls": {"enabled": cluster == "kubernetes"},
        },
    }
    if cluster == "openshift":
        host = _prompt(
            "Route hostname (optional, leave empty for OpenShift auto-generated)",
            str(_nested_get(existing, "route", "host", default="") or ""),
        )
        if host:
            generated["route"]["host"] = host
    elif cluster == "kubernetes":
        host = _prompt(
            "Ingress hostname (e.g. rootcoz.example.com)",
            str(_nested_get(existing, "ingress", "host", default="") or ""),
        )
        if not host:
            raise ValueError("Ingress hostname is required for Kubernetes")
        generated["ingress"]["host"] = host
        generated["ingress"]["className"] = _prompt(
            "Ingress class",
            str(_nested_get(existing, "ingress", "className", default="") or "nginx"),
        )
        generated["ingress"]["tls"]["secretName"] = _prompt(
            "TLS secret name",
            str(
                _nested_get(existing, "ingress", "tls", "secretName", default="")
                or "rootcoz-tls"
            ),
        )
    return generated


def _collect_ai(
    existing_generated: dict[str, Any] | None = None,
    existing_secrets: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_generated = existing_generated or {}
    existing_secrets = existing_secrets or {}

    provider_default = str(
        _nested_get(existing_generated, "ai", "provider", default="") or "gemini"
    )
    if provider_default not in VALID_PROVIDERS:
        provider_default = "gemini"
    provider = _prompt_choice("AI provider", VALID_PROVIDERS, provider_default)

    model_defaults = {
        "gemini": "gemini-2.5-pro",
        "claude": "claude-sonnet-4-20250514",
        "cursor": "gpt-5",
    }
    saved_model = str(_nested_get(existing_generated, "ai", "model", default="") or "")
    saved_provider = str(
        _nested_get(existing_generated, "ai", "provider", default="") or ""
    )
    if saved_model and saved_provider == provider:
        model_default = saved_model
    else:
        model_default = model_defaults[provider]
    model = _prompt("AI model", model_default)

    generated: dict[str, Any] = {"ai": {"provider": provider, "model": model}}
    secrets: dict[str, Any] = {"ai": {}}

    if provider == "gemini":
        secrets["ai"]["geminiApiKey"] = _prompt_secret(
            "Gemini API key",
            existing=str(
                _nested_get(existing_secrets, "ai", "geminiApiKey", default="") or ""
            ),
        )
    elif provider == "claude":
        saved_vertex = bool(
            _nested_get(existing_generated, "ai", "vertex", "enabled", default=False)
        )
        use_vertex = _prompt_yes_no(
            "Use Google Vertex AI for Claude?", default=saved_vertex
        )
        generated["ai"]["vertex"] = {"enabled": use_vertex}
        if use_vertex:
            generated["ai"]["vertex"]["projectId"] = _prompt(
                "GCP project ID",
                str(
                    _nested_get(
                        existing_generated, "ai", "vertex", "projectId", default=""
                    )
                    or ""
                ),
            )
            generated["ai"]["vertex"]["region"] = _prompt(
                "Vertex region",
                str(
                    _nested_get(
                        existing_generated, "ai", "vertex", "region", default=""
                    )
                    or "us-east5"
                ),
            )
            existing_sa = _nested_get(
                existing_secrets, "ai", "vertex", "serviceAccountKey", default=""
            )
            if isinstance(existing_sa, dict):
                existing_sa = json.dumps(existing_sa)
            existing_sa_str = str(existing_sa or "")
            if existing_sa_str:
                keep = _prompt_yes_no(
                    "Keep existing GCP service account key?", default=True
                )
                if keep:
                    secrets["ai"]["vertex"] = {"serviceAccountKey": existing_sa_str}
                else:
                    sa_path = _prompt("Path to GCP service account JSON key file")
                    secrets["ai"]["vertex"] = {"serviceAccountKey": _read_file(sa_path)}
            else:
                sa_path = _prompt("Path to GCP service account JSON key file")
                secrets["ai"]["vertex"] = {"serviceAccountKey": _read_file(sa_path)}
        else:
            secrets["ai"]["anthropicApiKey"] = _prompt_secret(
                "Anthropic API key",
                existing=str(
                    _nested_get(existing_secrets, "ai", "anthropicApiKey", default="")
                    or ""
                ),
            )
    else:
        existing_cursor_key = str(
            _nested_get(existing_secrets, "ai", "cursor", "apiKey", default="") or ""
        )
        existing_auth = _nested_get(
            existing_secrets, "ai", "cursor", "authJson", default=""
        )
        api_key = _prompt_secret(
            "Cursor API key", required=False, existing=existing_cursor_key
        )
        if api_key:
            secrets["ai"]["cursor"] = {"apiKey": api_key}

        if existing_auth:
            keep_auth = _prompt_yes_no("Keep existing Cursor auth.json?", default=True)
            if keep_auth:
                secrets["ai"].setdefault("cursor", {})["authJson"] = existing_auth
            else:
                auth_path = _prompt("Path to Cursor auth.json (optional)", "")
                if auth_path:
                    secrets["ai"].setdefault("cursor", {})["authJson"] = (
                        _read_json_file(auth_path)
                    )
        else:
            auth_path = _prompt("Path to Cursor auth.json (optional)", "")
            if auth_path:
                secrets["ai"].setdefault("cursor", {})["authJson"] = _read_json_file(
                    auth_path
                )

        if not secrets["ai"].get("cursor"):
            raise ValueError(
                "Cursor provider requires an API key and/or auth.json file"
            )

    return generated, secrets


def _collect_admin(existing_secrets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect bootstrap admin API key used as the first-login password."""
    existing_secrets = existing_secrets or {}
    existing_key = str(_nested_get(existing_secrets, "admin", "key", default="") or "")
    print(
        "\nBootstrap admin login uses username 'admin' and an API key as the password."
    )
    while True:
        key = _prompt_secret(
            "Admin API key (first-login password)",
            existing=existing_key,
        )
        if len(key) < _ADMIN_KEY_MIN_LENGTH:
            print(f"Admin API key must be at least {_ADMIN_KEY_MIN_LENGTH} characters.")
            continue
        # Kept stored value — no re-confirm needed.
        if existing_key and key == existing_key:
            return {"admin": {"key": key}}
        confirm = _prompt_secret("Confirm admin API key")
        if key != confirm:
            print("Keys do not match. Try again.")
            continue
        return {"admin": {"key": key}}


def _write_values(path: Path, data: dict[str, Any], secret: bool) -> None:
    """Write a YAML values file, optionally with restricted permissions.

    For secret files the descriptor is opened with mode 0600 *before* any
    content is written, eliminating the TOCTOU window where a
    world-readable file briefly exists on disk.

    Always overwrites — callers collect fresh values (with prior values as
    defaults) before writing.
    """
    content = _dump_yaml(data)
    if secret:
        fd = os.open(
            str(path),
            os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(str(path), 0o600)
    else:
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o644)


def _path_under_repo(path: Path) -> bool:
    """Return True if *path* resolves inside the rootcoz git checkout."""
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
        return True
    except ValueError:
        return False


def _ensure_output_dir(path: Path) -> Path:
    """Create output dir with mode 0700; refuse accidental writes into the repo."""
    resolved = path.expanduser().resolve()
    if _path_under_repo(resolved):
        raise ValueError(
            f"Refusing to write values under the git repo ({resolved}). "
            f"Choose a directory outside the checkout (default: {_DEFAULT_OUTPUT_DIR})."
        )
    try:
        resolved.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(resolved, 0o700)
    except OSError as exc:
        raise ValueError(f"Cannot create output directory {resolved}: {exc}") from exc
    return resolved


def _resolve_output_paths(
    output_dir: str | None,
    *,
    prompt: bool = True,
) -> tuple[Path, Path]:
    """Resolve generated/secrets paths under an output directory.

    When *output_dir* is None and *prompt* is True, ask the user (defaulting to
    ``~/.config/rootcoz/helm``).
    """
    if output_dir is None and prompt:
        raw = _prompt(
            "Output directory for values files (outside the git repo)",
            str(_DEFAULT_OUTPUT_DIR),
        )
    else:
        raw = output_dir or str(_DEFAULT_OUTPUT_DIR)
    out = _ensure_output_dir(Path(raw))
    return out / _GENERATED_FILENAME, out / _SECRETS_FILENAME


def _collect_install_target(
    existing_meta: dict[str, Any] | None = None,
    *,
    release_default: str | None = None,
    namespace_default: str | None = None,
) -> dict[str, str]:
    """Prompt for Helm release name and Kubernetes namespace."""
    existing_meta = existing_meta or {}
    release = _prompt(
        "Helm release name",
        release_default or str(existing_meta.get("release") or "") or _DEFAULT_RELEASE,
    )
    namespace = _prompt(
        "Kubernetes namespace",
        namespace_default
        or str(existing_meta.get("namespace") or "")
        or _DEFAULT_NAMESPACE,
    )
    if not release:
        raise ValueError("Helm release name is required")
    if not namespace:
        raise ValueError("Kubernetes namespace is required")
    return {"release": release, "namespace": namespace}


def _resolve_helm() -> str:
    """Return the helm binary path or raise if Helm 3 is not on PATH."""
    helm = shutil.which("helm")
    if not helm:
        raise RuntimeError(
            "helm not found in PATH. Install Helm 3 "
            "(https://helm.sh/docs/intro/install/) or re-run with --skip-helm "
            "to only write values files."
        )
    return helm


def _run_helm(
    release: str,
    namespace: str,
    generated_path: Path,
    secrets_path: Path,
    dry_run: bool,
) -> None:
    cmd = [
        _resolve_helm(),
        "upgrade",
        "--install",
        release,
        str(CHART_DIR),
        "--namespace",
        namespace,
        "--create-namespace",
        "-f",
        str(generated_path),
        "-f",
        str(secrets_path),
    ]
    if dry_run:
        cmd.append("--dry-run")

    print("\nRunning:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive rootcoz Helm setup")
    parser.add_argument(
        "--release",
        default=None,
        help=f"Helm release name (default: prompt, usually {_DEFAULT_RELEASE})",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help=f"Kubernetes namespace (default: prompt, usually {_DEFAULT_NAMESPACE})",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            f"Directory for values.generated.yaml / values.secrets.yaml "
            f"(default: prompt, usually {_DEFAULT_OUTPUT_DIR})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to helm (no cluster changes)",
    )
    parser.add_argument(
        "--skip-helm",
        action="store_true",
        help="Only write values files, do not run helm",
    )
    args = parser.parse_args()

    if not CHART_DIR.is_dir():
        print(f"Chart not found at {CHART_DIR}", file=sys.stderr)
        return 1

    # Fail fast before prompts/credentials when Helm will be required.
    if not args.skip_helm:
        try:
            _resolve_helm()
        except RuntimeError as exc:
            print(f"Setup failed: {exc}", file=sys.stderr)
            return 1

    print("rootcoz Helm setup\n")

    # Resolve output dir first so re-runs can load prior values as defaults.
    try:
        generated_path, secrets_path = _resolve_output_paths(
            args.output_dir,
            prompt=args.output_dir is None,
        )
    except ValueError as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    meta_path = generated_path.parent / _SETUP_META_FILENAME
    try:
        existing_generated = _load_values(generated_path)
        existing_secrets = _load_values(secrets_path)
        existing_meta = _load_values(meta_path)
    except ValueError as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Values directory: {generated_path.parent}")
    if existing_generated or existing_secrets or existing_meta:
        print("Loaded existing values (used as defaults).")
    print()

    # Collect and save incrementally so earlier values survive if a later
    # step fails (e.g. AI config error doesn't lose release/namespace).
    try:
        install_target = _collect_install_target(
            existing_meta,
            release_default=args.release,
            namespace_default=args.namespace,
        )
        _write_values(meta_path, install_target, secret=False)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    try:
        generated = _collect_routing(existing_generated)
        _write_values(generated_path, generated, secret=False)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    try:
        ai_generated, ai_secrets = _collect_ai(existing_generated, existing_secrets)
        generated.update(ai_generated)
        _write_values(generated_path, generated, secret=False)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    try:
        admin_secrets = _collect_admin(existing_secrets)
        ai_secrets.update(admin_secrets)
        _write_values(secrets_path, ai_secrets, secret=True)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nWrote {meta_path}")
    print(f"Wrote {generated_path}")
    print(f"Wrote {secrets_path} (mode 0600)")
    print("Do not commit these files — they live outside the git repo by design.")

    if args.skip_helm:
        print("\nSkipping helm install (--skip-helm).")
        return 0

    if not _prompt_yes_no("\nRun helm upgrade --install now?", default=True):
        print("Skipped helm install.")
        return 0

    try:
        _run_helm(
            release=install_target["release"],
            namespace=install_target["namespace"],
            generated_path=generated_path,
            secrets_path=secrets_path,
            dry_run=args.dry_run,
        )
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Helm install failed: {exc}", file=sys.stderr)
        return 1

    release = install_target["release"]
    namespace = install_target["namespace"]

    # Try to fetch the route URL after deploy.
    route_url = ""
    if not args.dry_run and generated.get("route", {}).get("enabled"):
        try:
            route_host = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "route",
                    f"{release}-route",
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.spec.host}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if route_host.returncode == 0 and route_host.stdout.strip():
                route_url = f"https://{route_host.stdout.strip()}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # kubectl not available or timed out — skip

    print("\nDone. First login:")
    if route_url:
        print(f"  URL: {route_url}")
    print("  Username: admin")
    print("  Password: the admin API key you set during setup")
    print(f"(Also stored in {secrets_path} as admin.key.)")
    print(f"Release: {release}  Namespace: {namespace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
