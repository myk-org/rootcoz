"""Interactive rootcoz Helm install helper.

Prompts for bootstrap settings, writes split values files, and runs helm upgrade --install.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "chart"
VALID_PROVIDERS = ("gemini", "claude", "cursor")


def _prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
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
        value = input(f"{message} ({default_label}): ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n.")


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


def _collect_routing() -> dict[str, Any]:
    cluster = _prompt_choice(
        "Cluster type (openshift/kubernetes/clusterip)",
        ("openshift", "kubernetes", "clusterip"),
        "openshift",
    )
    generated: dict[str, Any] = {
        "route": {"enabled": cluster == "openshift"},
        "ingress": {
            "enabled": cluster == "kubernetes",
            "tls": {"enabled": cluster == "kubernetes"},
        },
    }
    if cluster == "openshift":
        host = _prompt("Route hostname (e.g. rootcoz.apps.cluster.com)")
        if not host:
            raise ValueError("Route hostname is required for OpenShift")
        generated["route"]["host"] = host
    elif cluster == "kubernetes":
        host = _prompt("Ingress hostname (e.g. rootcoz.example.com)")
        if not host:
            raise ValueError("Ingress hostname is required for Kubernetes")
        generated["ingress"]["host"] = host
        generated["ingress"]["className"] = _prompt("Ingress class", "nginx")
        generated["ingress"]["tls"]["secretName"] = _prompt(
            "TLS secret name", "rootcoz-tls"
        )
    return generated


def _prompt_secret(message: str, *, required: bool = True) -> str:
    value = getpass.getpass(f"{message}: ").strip()
    if required and not value:
        raise ValueError(f"{message} is required")
    return value


def _collect_ai() -> tuple[dict[str, Any], dict[str, Any]]:
    provider = _prompt_choice("AI provider", VALID_PROVIDERS, "gemini")
    model_default = {
        "gemini": "gemini-2.5-pro",
        "claude": "claude-sonnet-4-20250514",
        "cursor": "gpt-5",
    }[provider]
    model = _prompt("AI model", model_default)

    generated: dict[str, Any] = {"ai": {"provider": provider, "model": model}}
    secrets: dict[str, Any] = {"ai": {}}

    if provider == "gemini":
        secrets["ai"]["geminiApiKey"] = _prompt_secret("Gemini API key")
    elif provider == "claude":
        use_vertex = _prompt_yes_no("Use Google Vertex AI for Claude?", default=False)
        generated["ai"]["vertex"] = {"enabled": use_vertex}
        if use_vertex:
            generated["ai"]["vertex"]["projectId"] = _prompt("GCP project ID")
            generated["ai"]["vertex"]["region"] = _prompt("Vertex region", "us-east5")
            sa_path = _prompt("Path to GCP service account JSON key file")
            secrets["ai"]["vertex"] = {"serviceAccountKey": _read_file(sa_path)}
        else:
            secrets["ai"]["anthropicApiKey"] = _prompt_secret("Anthropic API key")
    else:
        api_key = _prompt_secret("Cursor API key", required=False)
        auth_path = _prompt("Path to Cursor auth.json (optional)", "")
        if api_key:
            secrets["ai"]["cursor"] = {"apiKey": api_key}
        if auth_path:
            secrets["ai"].setdefault("cursor", {})["authJson"] = _read_json_file(
                auth_path
            )
        if not secrets["ai"].get("cursor"):
            raise ValueError(
                "Cursor provider requires an API key and/or auth.json file"
            )

    return generated, secrets


def _write_values(path: Path, data: dict[str, Any], secret: bool) -> bool:
    """Write a YAML values file, optionally with restricted permissions.

    For secret files the descriptor is opened with mode 0600 *before* any
    content is written, eliminating the TOCTOU window where a
    world-readable file briefly exists on disk.

    If *path* already exists the user is prompted for confirmation before
    overwriting.

    Returns True if the file was written, False if skipped.
    """
    if path.exists():
        answer = input(f"{path} already exists. Overwrite? (y/n): ").strip().lower()
        if answer not in {"y", "yes"}:
            print(f"Skipped writing {path}")
            return False

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
    return True


def _run_helm(
    release: str,
    namespace: str,
    generated_path: Path,
    secrets_path: Path,
    dry_run: bool,
) -> None:
    helm = shutil.which("helm")
    if not helm:
        raise RuntimeError("helm not found in PATH")

    cmd = [
        helm,
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
    parser.add_argument("--release", default="rootcoz", help="Helm release name")
    parser.add_argument("--namespace", default="rootcoz", help="Kubernetes namespace")
    parser.add_argument(
        "--generated-file",
        default="values.generated.yaml",
        help="Output path for non-secret values",
    )
    parser.add_argument(
        "--secrets-file",
        default="values.secrets.yaml",
        help="Output path for secret values",
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

    print("rootcoz Helm setup\n")
    try:
        generated = _collect_routing()
        ai_generated, ai_secrets = _collect_ai()
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    generated.update(ai_generated)

    generated_path = Path(args.generated_file)
    secrets_path = Path(args.secrets_file)

    wrote_generated = _write_values(generated_path, generated, secret=False)
    wrote_secrets = _write_values(secrets_path, ai_secrets, secret=True)

    if not wrote_generated and not wrote_secrets:
        print("\nBoth files were skipped. Nothing to do.")
        return 0

    if wrote_generated:
        print(f"\nWrote {generated_path}")
    if wrote_secrets:
        print(f"Wrote {secrets_path} (mode 0600)")
        print("Never commit values.secrets.yaml.")

    if args.skip_helm:
        print("\nSkipping helm install (--skip-helm).")
        return 0

    if not _prompt_yes_no("\nRun helm upgrade --install now?", default=True):
        print("Skipped helm install.")
        return 0

    try:
        _run_helm(
            release=args.release,
            namespace=args.namespace,
            generated_path=generated_path,
            secrets_path=secrets_path,
            dry_run=args.dry_run,
        )
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Helm install failed: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Retrieve admin API key:")
    # Replicate Helm _helpers.tpl fullname logic:
    # 1. If fullnameOverride is set -> use it (truncated to 63 chars).
    # 2. If release name already contains the chart name -> use release.
    # 3. Otherwise -> "{release}-{chart}".
    # Since this script doesn't set fullnameOverride, only cases 2-3 apply.
    chart_name = "rootcoz"
    if chart_name in args.release:
        fullname = args.release[:63]
    else:
        fullname = f"{args.release}-{chart_name}"[:63]
    print(
        f"  kubectl get secret {fullname}-secret -n {args.namespace} "
        "-o jsonpath='{.data.ADMIN_KEY}' | base64 -d; echo"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
