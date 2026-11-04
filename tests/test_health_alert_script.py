"""Tests for chart/files/health-alert.sh — the script behind the chart alert CronJob.

Runs the real script with `sh` and a fixture body in METRICS_FILE, so no network,
no curl and no cluster are involved. An empty SLACK_WEBHOOK_URL puts the script in
dry mode: the message it would post is printed to stdout.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "chart" / "files" / "health-alert.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "health-alert"


def run_script(
    *, metrics_file: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "METRICS_FILE": metrics_file,
        "METRICS_URL": "",
        "ENV_LABEL": "test",
        "SLACK_WEBHOOK_URL": "",
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestHealthAlertScript:
    def test_healthy_body_sends_nothing(self):
        result = run_script(metrics_file=str(FIXTURES / "healthy.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_warning_at_75_percent(self):
        result = run_script(metrics_file=str(FIXTURES / "disk-warn.metrics"))
        assert result.returncode == 0
        assert "⚠️" in result.stdout
        assert "4.5G / 6.0G (75%)" in result.stdout

    def test_critical_at_90_percent_uses_its_own_sizes(self):
        result = run_script(metrics_file=str(FIXTURES / "disk-crit.metrics"))
        assert result.returncode == 0
        assert "🔴" in result.stdout
        assert "5.4G / 6.0G (90%)" in result.stdout
        # The critical branch must not have been served by the warning numbers.
        assert "4.5G" not in result.stdout

    def test_missing_pvc_gauges_skip_the_disk_check(self):
        """Pre-rollout case: an image without the gauges must stay silent on disk."""
        result = run_script(metrics_file=str(FIXTURES / "no-pvc-gauges.metrics"))
        assert result.returncode == 0
        assert "disk" not in result.stdout

    def test_error_rate_threshold(self):
        low = run_script(metrics_file=str(FIXTURES / "error-low.metrics"))
        assert low.returncode == 0
        assert "error rate" not in low.stdout

        high = run_script(metrics_file=str(FIXTURES / "error-high.metrics"))
        assert high.returncode == 0
        assert "error rate 6.00% (threshold 5%)" in high.stdout

    def test_unhealthy_and_all_checks_in_one_message(self):
        result = run_script(metrics_file=str(FIXTURES / "unhealthy.metrics"))
        assert result.returncode == 0
        lines = result.stdout.strip().splitlines()
        assert lines == [
            "🔴 health: unhealthy",
            "⚠️ error rate 6.00% (threshold 5%)",
            "🔴 disk 5.4G / 6.0G (90%)",
        ]

    def test_unreachable_metrics(self, tmp_path):
        result = run_script(metrics_file=str(tmp_path / "missing.metrics"))
        assert result.returncode == 0
        assert "/metrics unreachable" in result.stdout

    def test_payload_escapes_control_characters(self, tmp_path):
        """A tab in a value must not produce invalid JSON for the webhook."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        dump = tmp_path / "payload.json"
        shim = bindir / "curl"
        shim.write_text(
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--data" ]; then\n'
            "    shift\n"
            '    printf "%s" "$1" > "$CURL_DUMP"\n'
            "  fi\n"
            "  shift\n"
            "done\n"
        )
        shim.chmod(0o755)

        result = run_script(
            metrics_file=str(tmp_path / "missing.metrics"),
            extra_env={
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "SLACK_WEBHOOK_URL": "http://webhook.invalid/hook",
                "CURL_DUMP": str(dump),
                "ENV_LABEL": "Root\tCoz",
            },
        )

        assert result.returncode == 0
        payload = dump.read_text()
        assert "\t" not in payload
        assert json.loads(payload)["text"] == "🔴 Root\tCoz: /metrics unreachable"
