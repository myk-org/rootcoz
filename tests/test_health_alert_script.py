"""Tests for chart/files/health-alert.sh — the script behind the chart alert CronJob.

Runs the real script with `sh` and a fixture body in METRICS_FILE, so no network,
no curl and no cluster are involved. An empty SLACK_WEBHOOK_URL puts the script in
dry mode: the message it would post is printed to stdout.

Silence is asserted as `stdout == "" and returncode == 0`: the harness runs with
check=False, so silence alone cannot tell "correctly quiet" from "died quietly".
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


def fixture(name: str) -> str:
    return str(FIXTURES / name)


def webhook_shim(tmp_path: Path) -> dict[str, str]:
    """Env that sends the script's curl to a file dump instead of the network."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
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
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SLACK_WEBHOOK_URL": "http://webhook.invalid/hook",
        "CURL_DUMP": str(tmp_path / "payload.json"),
    }


def test_unhealthy_and_all_checks_in_one_message():
    result = run_script(metrics_file=fixture("unhealthy.metrics"))
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert lines == [
        "🔴 health: unhealthy",
        "🔴 server errors 5xx 2/20 (10.00%) above 5% threshold",
        "🔴 disk 5.4G / 6.0G (90%)",
    ]


class TestHealthAlertScript:
    def test_healthy_body_sends_nothing(self):
        result = run_script(metrics_file=fixture("healthy.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_warning_at_75_percent(self):
        result = run_script(metrics_file=fixture("disk-warn.metrics"))
        assert result.returncode == 0
        assert "⚠️" in result.stdout
        assert "4.5G / 6.0G (75%)" in result.stdout

    def test_critical_at_90_percent_uses_its_own_sizes(self):
        result = run_script(metrics_file=fixture("disk-crit.metrics"))
        assert result.returncode == 0
        assert "🔴" in result.stdout
        assert "5.4G / 6.0G (90%)" in result.stdout
        # The critical branch must not have been served by the warning numbers.
        assert "4.5G" not in result.stdout

    def test_missing_data_gauges_skip_the_disk_check(self):
        """Pre-rollout case: an image without the gauges must stay silent on disk."""
        result = run_script(metrics_file=fixture("no-data-gauges.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_unreachable_metrics(self, tmp_path):
        result = run_script(metrics_file=str(tmp_path / "missing.metrics"))
        assert result.returncode == 0
        assert "/metrics unreachable" in result.stdout

    def test_non_metrics_body_is_loud_instead_of_silent(self):
        """A login or proxy error page at a wrong URL must not silence every check."""
        result = run_script(metrics_file=fixture("not-metrics.metrics"))
        assert result.returncode == 0
        assert result.stdout.strip() == (
            "🔴 test: metrics endpoint returned no rootcoz metrics"
        )

    def test_payload_escapes_control_characters(self, tmp_path):
        """A tab in a value must not produce invalid JSON for the webhook."""
        env = webhook_shim(tmp_path)
        env["ENV_LABEL"] = "Root\tCoz"
        dump = tmp_path / "payload.json"

        result = run_script(
            metrics_file=str(tmp_path / "missing.metrics"), extra_env=env
        )

        assert result.returncode == 0
        payload = dump.read_text()
        assert "\t" not in payload
        assert json.loads(payload)["text"] == "🔴 Root\tCoz: /metrics unreachable"

    def test_payload_escapes_every_control_character(self, tmp_path):
        """A backspace or form feed in a value must not produce invalid JSON."""
        env = webhook_shim(tmp_path)
        env["ENV_LABEL"] = "Root\bCoz\fX"
        dump = tmp_path / "payload.json"

        result = run_script(
            metrics_file=str(tmp_path / "missing.metrics"), extra_env=env
        )

        assert result.returncode == 0
        payload = dump.read_text()
        assert "\b" not in payload
        assert "\f" not in payload
        assert json.loads(payload)["text"] == "🔴 Root\bCoz\fX: /metrics unreachable"


class TestServerErrorSignal:
    """5xx alone decides "the server is erroring" (D3/D4)."""

    def test_server_error_message_names_class_and_counts(self):
        result = run_script(metrics_file=fixture("server-error-only.metrics"))
        assert result.returncode == 0
        assert "server errors 5xx 3/20 (15.00%)" in result.stdout
        assert "above 5% threshold" in result.stdout

    def test_quiet_server_error_still_pages(self):
        """A lone 500 in a tiny window is still an incident: no request floor on 5xx."""
        result = run_script(metrics_file=fixture("quiet-server-error.metrics"))
        assert result.returncode == 0
        assert "server errors 5xx 1/4 (25.00%)" in result.stdout

    def test_server_error_at_the_threshold_does_not_page(self):
        result = run_script(metrics_file=fixture("server-at-threshold.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_thresholds_are_configurable(self):
        quiet = run_script(
            metrics_file=fixture("server-error-only.metrics"),
            extra_env={"SERVER_ERROR_PERCENT": "20"},
        )
        assert quiet.returncode == 0
        assert quiet.stdout == ""

        floored = run_script(
            metrics_file=fixture("client-storm.metrics"),
            extra_env={"CLIENT_ERROR_MIN_REQUESTS": "1000"},
        )
        assert floored.returncode == 0
        assert floored.stdout == ""


class TestClientErrorSignal:
    """4xx is judged on its own high threshold and never as an outage (D3)."""

    def test_quiet_client_error_does_not_page(self):
        """The reported false positive: 1 4xx in 4 requests must not page."""
        result = run_script(metrics_file=fixture("quiet-client-error.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_empty_window_is_silent_and_exits_zero(self):
        result = run_script(metrics_file=fixture("empty-window.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_missing_class_samples_are_zero_not_unknown(self):
        result = run_script(metrics_file=fixture("no-class-samples.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_client_errors_alone_are_silent(self):
        result = run_script(metrics_file=fixture("client-error-only.metrics"))
        assert result.returncode == 0
        assert result.stdout == ""

    def test_client_error_storm_names_class_and_says_not_an_outage(self):
        result = run_script(metrics_file=fixture("client-storm.metrics"))
        assert result.returncode == 0
        assert "client error storm 4xx 30/40 (75.00%)" in result.stdout
        assert "not an outage" in result.stdout

    def test_both_classes_one_message(self):
        result = run_script(metrics_file=fixture("both-classes.metrics"))
        assert result.returncode == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 2
        assert "server errors 5xx 4/20 (20.00%)" in lines[0]
        assert "client error storm 4xx 6/20 (30.00%)" in lines[1]
        assert "ℹ️" not in result.stdout

    def test_client_error_percent_100_disables_the_storm(self):
        result = run_script(
            metrics_file=fixture("client-storm.metrics"),
            extra_env={"CLIENT_ERROR_PERCENT": "100"},
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_client_error_percent_100_is_a_real_off_switch(self):
        """A 100 % share must not storm under the documented off switch (strict >)."""
        result = run_script(
            metrics_file=fixture("full-client-errors.metrics"),
            extra_env={"CLIENT_ERROR_PERCENT": "100"},
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_storm_floor_is_inclusive(self):
        at_floor = run_script(metrics_file=fixture("storm-at-floor.metrics"))
        assert at_floor.returncode == 0
        assert "client error storm 4xx 3/10 (30.00%)" in at_floor.stdout

        below = run_script(metrics_file=fixture("storm-below-floor.metrics"))
        assert below.returncode == 0
        assert below.stdout == ""

    def test_storm_at_the_threshold_reports_at_or_below(self):
        result = run_script(metrics_file=fixture("storm-at-threshold.metrics"))
        assert result.returncode == 0
        lines = result.stdout.strip().splitlines()
        assert lines[0] == "🔴 health: unhealthy"
        assert lines[1].startswith("ℹ️ client errors 4xx 10/40")
        assert "at or below the 25% storm threshold" in lines[1]
        assert len(lines) == 2

    def test_client_line_rides_along_with_an_unrelated_page(self):
        result = run_script(metrics_file=fixture("unhealthy-client.metrics"))
        assert result.returncode == 0
        lines = result.stdout.strip().splitlines()
        assert lines[0] == "🔴 health: unhealthy"
        assert lines[1].startswith("ℹ️ client errors 4xx 2/20")
        assert "at or below" in lines[1]

    def test_storm_floor_suppresses_the_client_line_entirely(self):
        """A sub-floor share is unjudgeable: not even described inside another page."""
        result = run_script(metrics_file=fixture("unhealthy-quiet-client.metrics"))
        assert result.returncode == 0
        assert result.stdout.strip() == "🔴 health: unhealthy"


class TestUnreadableBodies:
    """Shapes that cannot answer "share of requests" page one line and never exit (D7)."""

    def test_missing_errors_total_is_loud(self):
        result = run_script(metrics_file=fixture("no-errors-total.metrics"))
        assert result.returncode == 0
        assert "🔴" in result.stdout
        assert "no rootcoz_errors_total" in result.stdout

    def test_all_series_absent_still_pages(self):
        """A body cannot fall between the three conditions and stay silent."""
        result = run_script(metrics_file=fixture("only-error-rate.metrics"))
        assert result.returncode == 0
        assert "🔴" in result.stdout
        assert "no rootcoz_errors_total" in result.stdout

    def test_missing_class_family_is_loud_and_keeps_health_and_disk(self):
        result = run_script(metrics_file=fixture("no-class-family.metrics"))
        assert result.returncode == 0
        text = result.stdout
        assert "rootcoz_errors_by_class does not add up to rootcoz_errors_total" in text
        assert "🔴 health: unhealthy" in text
        assert "🔴 disk 5.7G / 6.0G (95%)" in text
        assert len(text.strip().splitlines()) == 3

    def test_partial_label_drift_is_loud(self):
        result = run_script(metrics_file=fixture("label-drift.metrics"))
        assert result.returncode == 0
        text = result.stdout
        assert "rootcoz_errors_by_class does not add up to rootcoz_errors_total" in text
        assert "server errors 5xx" not in text
        assert "ℹ️" not in text
        assert len(text.strip().splitlines()) == 1

    def test_drifted_5xx_suppresses_the_client_line(self):
        result = run_script(metrics_file=fixture("drifted-5xx-readable-4xx.metrics"))
        assert result.returncode == 0
        text = result.stdout
        assert "🔴" in text
        assert "ℹ️" not in text
        assert "client error storm" not in text

    def test_unreadable_denominator_is_loud(self):
        result = run_script(metrics_file=fixture("no-requests-total.metrics"))
        assert result.returncode == 0
        assert "🔴" in result.stdout
        assert "no usable rootcoz_requests_total" in result.stdout
        assert "rootcoz_errors_by_class does not add up" not in result.stdout

    def test_missing_denominator_is_loud_even_with_zero_errors(self):
        """Drift must not wait for the first error to be noticed."""
        result = run_script(
            metrics_file=fixture("no-requests-total-zero-errors.metrics")
        )
        assert result.returncode == 0
        assert "no usable rootcoz_requests_total" in result.stdout
        assert len(result.stdout.strip().splitlines()) == 1

    def test_non_numeric_class_count_is_loud(self):
        """A garbage field 2 must not coerce to a silent "no 5xx"."""
        result = run_script(metrics_file=fixture("nonnumeric-count.metrics"))
        assert result.returncode == 0
        assert "non-numeric rootcoz_errors_by_class" in result.stdout
        assert "server errors 5xx" not in result.stdout
        assert "ℹ️" not in result.stdout

    def test_comment_less_body_is_judged_normally(self):
        """HELP/TYPE are optional: samples alone must be judged, never refused."""
        result = run_script(metrics_file=fixture("no-help-comments.metrics"))
        assert result.returncode == 0
        assert "server errors 5xx 3/20 (15.00%)" in result.stdout
        assert "configuration" not in result.stdout

    def test_hostile_count_text_never_reaches_the_message(self, tmp_path):
        result = run_script(metrics_file=fixture("hostile-count.metrics"))
        assert result.returncode == 0
        assert "server errors 5xx 3/20" in result.stdout
        assert "evil.example" not in result.stdout

        env = webhook_shim(tmp_path)
        dumped = run_script(
            metrics_file=fixture("hostile-count.metrics"), extra_env=env
        )
        assert dumped.returncode == 0
        payload = (tmp_path / "payload.json").read_text()
        assert "server errors 5xx 3/20" in json.loads(payload)["text"]
        assert "evil.example" not in payload
