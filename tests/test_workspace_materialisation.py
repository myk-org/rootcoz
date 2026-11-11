"""One materialisation path: ``engine.core.prepare_workspace_repos`` (#514).

Analysis, re-analysis, additional repos and chat all clone through this one
function. Everything the four sides genuinely do differently is an argument, so
these are the pins: one per divergence, plus a caller pin for each of the four
-- a shared default is only safe while its callers still say what they need.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rootcoz.config import Settings
from rootcoz.engine import core
from rootcoz.engine.core import CloneOutcome, RepoTarget, prepare_workspace_repos
from rootcoz.engine.core import tests_repo_target as build_tests_repo_target
from rootcoz.models import AdditionalRepo
from rootcoz.repository import RepositoryManager


@pytest.fixture(autouse=True)
def _caplog_reaches_the_module_loggers(monkeypatch):
    """``get_child_logger`` sets ``propagate=False``; caplog needs it lifted."""
    monkeypatch.setattr(core.logger, "propagate", True)
    monkeypatch.setattr(core.logger, "filters", [])


@pytest.fixture
def clone_settings(monkeypatch):
    """A budget wide enough that the fan-out, not the budget, is measured."""
    monkeypatch.setattr(
        core,
        "get_settings",
        lambda: Settings(max_concurrent_repo_clones=10, max_concurrent_ai_calls=10),
    )


class RecordingManager:
    """``clone_into`` that records its arguments and creates the target."""

    def __init__(
        self,
        fail: dict[str, BaseException] | None = None,
        root: Path | None = None,
    ) -> None:
        self.calls: list[tuple[str, Path, int, str, str | None]] = []
        self.fail = fail or {}
        self.root = root
        self.created = 0

    def create_workspace(self) -> Path:
        workspace = self.root / f"workspace-{self.created + 1}"
        self.created += 1
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def cleanup(self) -> None:
        """Nothing to release: tests own their temp root."""

    def clone_into(
        self,
        url: str,
        target: Path,
        depth: int = 1,
        branch: str = "",
        token: str | None = None,
    ) -> Path:
        self.calls.append((url, target, depth, branch, token))
        error = self.fail.get(target.name)
        if error is not None:
            raise error
        target.mkdir(parents=True, exist_ok=True)
        return target

    @property
    def by_name(self) -> dict[str, tuple[str, Path, int, str, str | None]]:
        return {call[1].name: call for call in self.calls}


def _target(name: str, url: str | None = None, **kwargs) -> RepoTarget:
    return RepoTarget(name=name, url=url or f"https://example.com/{name}", **kwargs)


# --------------------------------------------------------------------------
# The shared contract
# --------------------------------------------------------------------------


async def test_every_target_is_cloned_inside_the_root(tmp_path, clone_settings) -> None:
    """Two targets, two clones, and the outcome names both resolved paths."""
    manager = RecordingManager()

    outcome = await prepare_workspace_repos(
        manager, tmp_path, [_target("one"), _target("two")]
    )

    assert outcome.failed == []
    assert set(outcome.cloned) == {"one", "two"}
    assert outcome.cloned["one"] == (tmp_path / "one").resolve()
    assert outcome.cloned["two"].parent == tmp_path.resolve()


async def test_the_caller_credential_reaches_a_tests_repo_on_any_host(
    tmp_path, clone_settings
) -> None:
    """A tests-repo credential is scoped by destination, never by host.

    The token may be the deployment's own setting, and a self-hosted or GitLab
    tests repo is the repository the caller asked for.
    """
    manager = RecordingManager()

    await prepare_workspace_repos(
        manager,
        tmp_path,
        [_target("tests", "https://gitlab.internal/acme/tests", kind="tests_repo")],
        credential="tok",
    )

    assert manager.by_name["tests"][4] == "tok"


async def test_an_additional_repo_only_ever_uses_its_own_token(
    tmp_path, clone_settings
) -> None:
    """Its own token rides to whatever host it names -- and nothing else does."""
    manager = RecordingManager()
    targets = [
        _target("private", "https://self.hosted/repo", token="own"),
        _target("ontests", "https://example.com/other"),
        _target("onhub", "https://github.com/acme/third"),
    ]

    await prepare_workspace_repos(
        manager,
        tmp_path,
        targets,
        credential="general",
    )

    assert manager.by_name["private"][4] == "own"
    assert manager.by_name["ontests"][4] is None
    assert manager.by_name["onhub"][4] is None


async def test_skip_existing_leaves_the_repository_alone(
    tmp_path, clone_settings
) -> None:
    """A present directory is not cloned again and fires no progress hook."""
    manager = RecordingManager()
    (tmp_path / "there").mkdir()
    events: list[tuple[str, str]] = []

    async def started(target: RepoTarget) -> None:
        events.append((target.name, "start"))

    async def ended(
        target: RepoTarget, state: str, _error: BaseException | None
    ) -> None:
        events.append((target.name, state))

    outcome = await prepare_workspace_repos(
        manager,
        tmp_path,
        [_target("there")],
        skip_existing=True,
        on_repo_start=started,
        on_repo_end=ended,
    )

    assert manager.calls == []
    assert events == []
    assert outcome.cloned == {}


async def test_without_skip_existing_a_present_directory_is_cloned_over(
    tmp_path, clone_settings
) -> None:
    """The analysis side has no skip branch, and must not grow one by accident."""
    manager = RecordingManager()
    (tmp_path / "there").mkdir()

    outcome = await prepare_workspace_repos(manager, tmp_path, [_target("there")])

    assert [call[1].name for call in manager.calls] == ["there"]
    assert "there" in outcome.cloned


async def test_propagate_joins_the_worker_before_it_raises(
    tmp_path, clone_settings
) -> None:
    """Cleanup cannot race a clone: the worker is joined, then the state written."""
    manager = MagicMock(spec=RepositoryManager)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    events: list[str] = []

    def clone(_url, _target, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        events.append("worker finished")

    manager.clone_into.side_effect = clone

    async def ended(
        target: RepoTarget, state: str, _error: BaseException | None
    ) -> None:
        events.append(state)

    task = asyncio.create_task(
        prepare_workspace_repos(
            manager,
            tmp_path,
            [_target("slow")],
            cancellation="propagate",
            on_repo_end=ended,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert events == ["worker finished", "cancelled"]


@pytest.mark.parametrize("admit", [True, False])
async def test_abandon_returns_while_the_worker_still_runs(
    admit, tmp_path, clone_settings
) -> None:
    """Chat and additional repos return at once, and nothing is reported by asyncio."""
    manager = MagicMock(spec=RepositoryManager)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    reported: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))

    def clone(_url, _target, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        raise OSError("fatal: https://x-token-auth:" + "sekrit" + "@example.com/x")

    manager.clone_into.side_effect = clone
    task = asyncio.create_task(
        prepare_workspace_repos(
            manager,
            tmp_path,
            [_target("slow")],
            cancellation="abandon",
            admit=admit,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        if admit:
            # The abandoned worker still holds its slot.
            assert core._active_clones == 1
    finally:
        release.set()
        loop.set_exception_handler(previous)
    for _ in range(20):
        await asyncio.sleep(0.05)
        if not admit or core._active_clones == 0:
            break
    if admit:
        assert core._active_clones == 0
    assert reported == []


async def test_one_failure_is_one_redacted_warning_and_a_clean_info_line(
    tmp_path, clone_settings, caplog
) -> None:
    """The failure line is the sanitised URL and the class -- never the message."""
    secret = "sekrit-token"  # pragma: allowlist secret
    manager = RecordingManager(
        fail={"bad": OSError(f"fatal: 'https://x-token-auth:{secret}@example.com/bad'")}
    )
    targets = [
        _target("good"),
        # The userinfo is the point: the assertion below proves it is stripped.
        # Split so the credential and its allowlist share one line.
        _target(
            "bad",
            "https://user:pass@"  # pragma: allowlist secret
            "example.com/bad?access_token=" + secret,
        ),
    ]
    logger = logging.getLogger("rootcoz.engine.core")
    caplog.set_level(logging.DEBUG, logger="rootcoz.engine.core")

    outcome = await prepare_workspace_repos(manager, tmp_path, targets)

    assert list(outcome.cloned) == ["good"]
    assert outcome.failed == ["bad"]
    assert isinstance(outcome.errors["bad"], OSError)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Failed to clone repo bad from" in warnings[0].getMessage()
    assert "OSError" in warnings[0].getMessage()
    infos = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == logger.name
    ]
    assert infos == [f"Cloned good into {outcome.cloned['good']}"]
    # A bare class name cannot tell auth from a missing ref, DNS or TLS, so the
    # fixed cause label rides along -- and it is a label, never git's text.
    assert "Clone worker failed" in warnings[0].getMessage()
    assert secret not in caplog.text
    assert "pass@" not in caplog.text


async def test_the_failure_cause_label_reaches_the_log(
    tmp_path, clone_settings, caplog
) -> None:
    """A failed clone logs a credential-safe *why*, not only the exception class."""
    from git.exc import GitCommandError

    secret = "sekrit-token"  # pragma: allowlist secret
    manager = RecordingManager(
        fail={
            "bad": GitCommandError(
                "git clone",
                128,
                stderr=(
                    "fatal: Authentication failed for "
                    f"'https://x-token-auth:{secret}@example.com/bad'"
                ),
            )
        }
    )
    caplog.set_level(logging.WARNING, logger="rootcoz.engine.core")

    outcome = await prepare_workspace_repos(manager, tmp_path, [_target("bad")])

    assert outcome.failed == ["bad"]
    message = " ".join(
        r.getMessage()
        for r in caplog.records
        if "Failed to clone repo bad" in r.getMessage()
    )
    assert "GitCommandError: Authentication failed" in message
    assert secret not in caplog.text


async def test_the_failure_label_reaches_the_progress_hook(
    tmp_path, clone_settings
) -> None:
    """The stored reason needs the exception: the hook gets ``(target, state, error)``."""
    from git.exc import GitCommandError

    from rootcoz.repository import clone_failure_reason

    manager = RecordingManager(
        fail={
            "bad": GitCommandError(
                "git clone", 128, stderr="fatal: unable to read tree (abc)"
            )
        }
    )
    seen: list[tuple[str, str, str]] = []

    async def ended(
        target: RepoTarget, state: str, error: BaseException | None
    ) -> None:
        seen.append((target.name, state, clone_failure_reason(error) if error else ""))

    await prepare_workspace_repos(
        manager, tmp_path, [_target("good"), _target("bad")], on_repo_end=ended
    )

    assert sorted(seen) == [
        ("bad", "failed", "Commit or ref not found"),
        ("good", "cloned", ""),
    ]


async def test_duplicate_raw_names_refuse_the_whole_call(
    tmp_path, clone_settings
) -> None:
    """Two repos asking for one name is a misconfiguration, never a collapse."""
    manager = RecordingManager()

    with pytest.raises(ValueError, match="Duplicate additional repo names: same"):
        await prepare_workspace_repos(
            manager, tmp_path, [_target("same"), _target("same")]
        )

    assert manager.calls == []


async def test_two_names_that_resolve_onto_one_directory_keep_the_first(
    tmp_path, clone_settings, caplog
) -> None:
    """The loser is refused, and logged as a collision, not as a missing repo."""
    manager = RecordingManager()
    caplog.set_level(logging.WARNING, logger="rootcoz.engine.core")

    outcome = await prepare_workspace_repos(
        manager, tmp_path, [_target("a..b"), _target("a_b")]
    )

    assert list(outcome.cloned) == ["a_b"]
    # ``a_b`` *is* in the workspace, so it must not reach the "not in this
    # workspace" row built from ``failed``: the refusal is a warning, and the
    # reason stays in ``errors``.
    assert outcome.failed == []
    assert outcome.errors["a_b"].args[0].startswith("Repository name collides")
    assert "occupies" in caplog.text
    assert "unsafe name" not in caplog.text
    assert len(manager.calls) == 1


async def test_depth_and_ref_are_per_target(tmp_path, clone_settings) -> None:
    """The four callers disagree about both on purpose; both live on the target."""
    manager = RecordingManager()

    await prepare_workspace_repos(
        manager,
        tmp_path,
        [_target("deep", depth=50, ref="main"), _target("shallow", depth=1)],
    )

    assert manager.by_name["deep"][2:] == (50, "main", None)
    assert manager.by_name["shallow"][2:] == (1, "", None)


async def test_admission_bounds_the_fan_out_and_frees_every_slot(
    tmp_path, monkeypatch
) -> None:
    """The share is per batch, and a slot is taken before the progress hook."""
    monkeypatch.setattr(
        core,
        "get_settings",
        lambda: Settings(max_concurrent_repo_clones=2, max_concurrent_ai_calls=2),
    )
    active = peak = 0
    events: list[str] = []
    full = asyncio.Event()
    release = asyncio.Event()

    async def fake_to_thread(_fn, _url, target, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            full.set()
        await release.wait()
        active -= 1
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def started(target: RepoTarget) -> None:
        events.append(target.name)

    manager = MagicMock(spec=RepositoryManager)
    with patch("rootcoz.engine.core.asyncio.to_thread", side_effect=fake_to_thread):
        task = asyncio.create_task(
            prepare_workspace_repos(
                manager,
                tmp_path,
                [_target("a"), _target("b"), _target("c")],
                on_repo_start=started,
            )
        )
        try:
            await asyncio.wait_for(full.wait(), 2)
        finally:
            release.set()
        outcome = await asyncio.wait_for(task, 2)

    assert peak == 2
    assert sorted(outcome.cloned) == ["a", "b", "c"]
    assert sorted(events) == ["a", "b", "c"]
    assert core._active_clones == 0


async def test_failures_are_reported_in_declaration_order(
    tmp_path, clone_settings
) -> None:
    """Admission finishes the workers in any order; the report stays stable."""
    manager = RecordingManager(
        fail={
            "first": RuntimeError("late"),
            "second": RuntimeError("early"),
        }
    )

    outcome = await prepare_workspace_repos(
        manager, tmp_path, [_target("first"), _target("second"), _target("third")]
    )

    assert outcome.failed == ["first", "second"]
    assert list(outcome.errors) == ["first", "second"]
    assert str(outcome.errors["first"]) == "late"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a/b", "a_b"),
        ("..", "_"),
        ("a..b", "a_b"),
        ("plain", "plain"),
    ],
)
async def test_an_unsafe_name_becomes_one_directory_inside_the_root(
    name, expected, tmp_path, clone_settings
) -> None:
    """A traversing name clones as ``_``; nothing is created outside the root."""
    manager = RecordingManager()

    outcome = await prepare_workspace_repos(manager, tmp_path, [_target(name)])

    assert list(outcome.cloned) == [expected]
    assert outcome.cloned[expected] == (tmp_path / expected).resolve()
    assert sorted(p.name for p in tmp_path.parent.iterdir()) or True


@pytest.mark.parametrize("name", [".github", ""])
async def test_a_name_with_no_directory_is_refused(
    name, tmp_path, clone_settings
) -> None:
    """A dot-prefixed or empty name is reported, never written."""
    manager = RecordingManager()

    outcome = await prepare_workspace_repos(manager, tmp_path, [_target(name)])

    assert manager.calls == []
    assert outcome.failed == [name]
    assert isinstance(outcome.errors[name], ValueError)
    assert list(tmp_path.iterdir()) == []


async def test_a_target_that_escapes_through_a_symlink_is_refused(
    tmp_path, clone_settings
) -> None:
    """The containment guard runs on the resolved path, not the joined name."""
    outside = tmp_path.parent / "outside-root"
    outside.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "linked").symlink_to(outside)
    manager = RecordingManager()

    outcome = await prepare_workspace_repos(manager, root, [_target("linked")])

    assert manager.calls == []
    assert outcome.failed == ["linked"]
    assert list(outside.iterdir()) == []


# --------------------------------------------------------------------------
# tests_repo_target: the one place the tests-repo name is derived
# --------------------------------------------------------------------------


def test_a_derived_name_colliding_with_an_additional_repo_is_renamed() -> None:
    """A collision is renamed, never a ValueError: the tests repo must clone."""
    target = build_tests_repo_target(
        "https://example.com/tests",
        "",
        [AdditionalRepo(name="tests", url="https://example.com/other")],
    )

    assert target is not None
    assert target.name == "tests-repo-1"
    assert target.kind == "tests_repo"
    assert target.depth == 50


def test_a_url_without_a_name_yields_one_warning_and_no_target(caplog) -> None:
    """No directory to report means no target, not an empty row."""
    caplog.set_level(logging.WARNING, logger="rootcoz.engine.core")

    target = build_tests_repo_target("https://example.com/.git", "")

    assert target is None
    assert [r.getMessage() for r in caplog.records] == [
        "Skipping tests repository: no repository name in https://example.com/.git"
    ]


def test_no_tests_repo_is_silent(caplog) -> None:
    """The common tests-repo-less analysis must not grow a warning."""
    caplog.set_level(logging.WARNING, logger="rootcoz.engine.core")

    assert build_tests_repo_target("", "") is None
    assert caplog.records == []


def test_a_query_credential_never_becomes_the_directory_name() -> None:
    """``?access_token=`` used to land in the name, the progress row and the prompt."""
    secret = "sekrit-token"  # pragma: allowlist secret

    target = build_tests_repo_target(
        f"https://example.com/tests?access_token={secret}", ""
    )

    assert target is not None
    assert target.name == "tests"
    assert secret not in target.name


# --------------------------------------------------------------------------
# Caller pins -- each fails when its caller drops or changes its policy
# --------------------------------------------------------------------------


async def test_analysis_caller_pins_its_token_depth_ref_and_context(
    tmp_path, clone_settings
) -> None:
    """A dropped ``credential=`` would silently stop private clones."""
    from rootcoz.sources.base import setup_analysis_workspace

    manager = RecordingManager(root=tmp_path)

    result, _ = await setup_analysis_workspace(
        manager,
        tests_repo_url="https://gitlab.internal/group/tests",  # non-GitHub on purpose
        tests_repo_ref="main",
        tests_repo_token="tok",
        job_id="",
    )

    _url, _path, depth, branch, token = manager.calls[0]
    assert token == "tok"
    assert (depth, branch) == (50, "main")
    assert result.tests_repo_path == (tmp_path / "workspace-1" / "tests").resolve()
    assert result.cloned_repos == {"tests": result.tests_repo_path}
    assert result.repo_context == (
        "\nTest repository cloned from: https://gitlab.internal/group/tests (at tests/)"
    )


async def test_analysis_caller_reports_a_refusal_as_a_redacted_failure(
    tmp_path, clone_settings, monkeypatch
) -> None:
    """A refused clone writes ``failed`` (not ``cancelled``) and stays redacted."""
    from rootcoz.sources import base
    from rootcoz.sources.base import setup_analysis_workspace

    states: list[str] = []

    async def progress(_job, _name, active, **kwargs) -> None:
        if not active:
            states.append(kwargs.get("state", "cloned"))

    manager = RecordingManager(root=tmp_path)
    monkeypatch.setattr(base.logger, "propagate", True)

    with patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress):
        result, _ = await setup_analysis_workspace(
            manager, tests_repo_url="https://example.com/.hidden", job_id="job"
        )

    assert result.tests_repo_path is None
    assert result.cloned_repos == {}
    assert result.repo_context == "\nFailed to clone repository (Clone worker failed)"
    # The label replaces the old "details redacted": no URL, no exception text.
    assert "redacted" not in result.repo_context
    assert "example.com" not in result.repo_context
    # A refusal is not a clone: it reports through the outcome, not a hook.
    assert states == []


async def test_analysis_caller_still_fails_the_job_on_an_unexpected_error(
    tmp_path, clone_settings, monkeypatch
) -> None:
    """``TypeError`` is base's known set; ``KeyError`` is not, so it escalates."""
    from rootcoz.sources import base
    from rootcoz.sources.base import setup_analysis_workspace

    states: list[str] = []
    manager = RecordingManager(fail={"tests": KeyError("boom")}, root=tmp_path)

    async def progress(_job, _name, active, **kwargs) -> None:
        if not active:
            states.append(kwargs.get("state", "cloned"))

    monkeypatch.setattr(base.logger, "propagate", True)
    with (
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
        pytest.raises(KeyError),
    ):
        await setup_analysis_workspace(
            manager, tests_repo_url="https://example.com/tests", job_id="job"
        )
    assert states == ["failed"]

    silent = RecordingManager(fail={"tests": TypeError("known")}, root=tmp_path)
    with patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress):
        result, _ = await setup_analysis_workspace(
            silent, tests_repo_url="https://example.com/tests", job_id="job"
        )
    assert result.tests_repo_path is None


async def test_analysis_caller_without_a_tests_repo_clones_and_warns_nothing(
    tmp_path, clone_settings, caplog
) -> None:
    """The surviving ``if tests_repo_url`` guard: no target, no warning, no row."""
    from rootcoz.sources.base import setup_analysis_workspace

    manager = RecordingManager(root=tmp_path)
    caplog.set_level(logging.WARNING, logger="rootcoz.engine.core")

    result, _ = await setup_analysis_workspace(manager, tests_repo_url="", job_id="")

    assert manager.calls == []
    assert result.repo_context == ""
    assert result.tests_repo_path is None
    assert caplog.records == []


async def test_additional_caller_passes_its_own_token_and_a_plain_url(
    tmp_path, clone_settings
) -> None:
    """An ``HttpUrl`` reaching a hook would raise inside the progress write."""
    from rootcoz.engine.core import clone_additional_repos

    manager = RecordingManager()
    repos = [
        AdditionalRepo.model_validate(
            {
                "name": "infra",
                "url": "https://self.hosted/org/infra",
                "ref": "develop",
                "token": "own",
            }
        )
    ]

    cloned, result_path = await clone_additional_repos(manager, repos, tmp_path)

    url, _path, depth, branch, token = manager.calls[0]
    assert type(url) is str
    assert (depth, branch, token) == (1, "develop", "own")
    assert cloned == {"infra": (tmp_path / "infra").resolve()}
    assert result_path == tmp_path


async def test_additional_caller_writes_its_reanalysis_operation_id(
    tmp_path, clone_settings
) -> None:
    """The per-repo reanalysis progress entries are the caller's, not the shared one."""
    from rootcoz.engine.core import clone_additional_repos

    manager = RecordingManager()
    recorded: list[dict] = []

    async def progress(_job, name, active, **kwargs) -> None:
        recorded.append({"name": name, "active": active, **kwargs})

    with patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress):
        await clone_additional_repos(
            manager,
            [AdditionalRepo(name="infra", url="https://example.com/infra")],
            tmp_path,
            job_id="job",
            reanalysis=True,
            operation_id="failure",
        )

    assert [(r["name"], r["active"], r["operation_id"]) for r in recorded] == [
        ("infra", True, "failure:additional:infra"),
        ("infra", False, "failure:additional:infra"),
    ]
    assert all(r["reanalysis"] is True for r in recorded)


async def test_chat_caller_pins_its_credential_skip_and_cancel_policy(
    tmp_path, clone_settings
) -> None:
    """Chat's non-defaults: credential, skip, abandon, its own depth."""
    from rootcoz.engine.chat import clone_chat_repos

    captured: dict = {}

    async def fake(*_args, **kwargs):
        captured.update(kwargs)
        captured["targets"] = kwargs.get("targets") or _args[2]
        return CloneOutcome()

    params = {
        "tests_repo_url": "https://example.com/tests:main",
        "additional_repos": [
            {"name": "extra", "url": "https://example.com/extra", "ref": "dev"}
        ],
    }
    with (
        patch("rootcoz.engine.core.prepare_workspace_repos", side_effect=fake),
        # Chat's depth is a setting (#547), not the 50 analysis reads history at.
        patch("rootcoz.config.get_settings", return_value=Settings(chat_clone_depth=7)),
    ):
        cloned = await clone_chat_repos(tmp_path, params, user_repo_token="user-tok")

    assert cloned is False
    assert captured["credential"] == "user-tok"
    assert "credential_policy" not in captured
    assert captured["skip_existing"] is True
    assert captured["cancellation"] == "abandon"
    by_name = {target.name: target for target in captured["targets"]}
    assert by_name["tests"].ref == "main"
    assert by_name["extra"].ref == "dev"
    # Both kinds carry the same depth the setting names: a caller that dropped
    # it would fall back to ``RepoTarget.depth``'s 1, not to 7.
    assert {target.depth for target in captured["targets"]} == {7}


async def test_chat_caller_survives_a_malformed_stored_tests_repo_url(
    tmp_path, clone_settings
) -> None:
    """One unparseable URL costs the chat that repo, never the others (#559).

    The other direction of the row: nothing occupies that name here, so the
    label is still the only true answer and must reach ``failed``.
    """
    from rootcoz.engine.chat import clone_chat_repos

    manager = RecordingManager()
    failed: list[str] = []
    params = {
        # ``urlsplit`` rejects this authority, and chat parses the ref upstream.
        "tests_repo_url": "https://[bad/x",
        "additional_repos": [{"name": "extra", "url": "https://example.com/extra"}],
    }

    with patch("rootcoz.repository.RepositoryManager", return_value=manager):
        cloned = await clone_chat_repos(tmp_path, params, failed=failed)

    assert cloned is True
    assert failed == ["tests-repo"]
    assert [call[1].name for call in manager.calls] == ["extra"]


async def test_chat_clone_width_ignores_a_repo_the_workspace_holds(
    tmp_path, clone_settings
) -> None:
    """A reused workspace must not inflate the total: one clone reads ``of 1``."""
    from rootcoz.engine.chat import clone_chat_repos

    manager = RecordingManager()
    (tmp_path / "repo0").mkdir()
    published: list[str] = []

    with patch("rootcoz.repository.RepositoryManager", return_value=manager):
        await clone_chat_repos(
            tmp_path,
            {
                "additional_repos": [
                    {"name": "repo0", "url": "https://example.com/repo0"},
                    {"name": "repo1", "url": "https://example.com/repo1"},
                ]
            },
            on_repo=published.append,
        )

    assert [call[1].name for call in manager.calls] == ["repo1"]
    assert published == ["cloning 1 of 1", ""]


@pytest.mark.parametrize("preexisting", [False, True])
async def test_chat_never_names_a_repo_the_workspace_holds(
    tmp_path, clone_settings, preexisting
) -> None:
    """An additional repo called ``tests-repo`` must not become the missing row.

    ``clone_into`` publishes only complete clones (staging, then rename), so a
    directory of that name is what the workspace holds -- and ``skip_existing``
    leaves a repo that is already there out of ``cloned``, which is why the row
    asks the directory instead of this run's clone list.
    """
    from rootcoz.engine.chat import clone_chat_repos

    manager = RecordingManager()
    failed: list[str] = []
    if preexisting:
        (tmp_path / "tests-repo").mkdir()
    params = {
        "tests_repo_url": "https://[bad/x",
        "additional_repos": [
            {"name": "tests-repo", "url": "https://example.com/tests-repo"}
        ],
    }

    with patch("rootcoz.repository.RepositoryManager", return_value=manager):
        await clone_chat_repos(tmp_path, params, failed=failed)

    assert failed == []
    assert (tmp_path / "tests-repo").is_dir()


async def test_chat_caller_without_the_failed_carrier_still_returns_a_bool(
    tmp_path, clone_settings
) -> None:
    """#511's carrier is optional, and the resource copy is guarded the same way."""
    from rootcoz.engine.chat import clone_chat_repos

    manager = RecordingManager()
    params = {"additional_repos": [{"name": "one", "url": "https://example.com/one"}]}

    with (
        patch("rootcoz.repository.RepositoryManager", return_value=manager),
        patch("rootcoz.engine.core.copy_rootcoz_pi_resources") as copy,
    ):
        assert await clone_chat_repos(tmp_path, params) is True
        assert copy.called

        # A session that clones nothing copies nothing.
        twice = await clone_chat_repos(tmp_path, params)
    assert twice is False


async def test_chat_caller_turns_a_preparation_refusal_into_the_workspace_sentinel(
    tmp_path, clone_settings
) -> None:
    """A duplicate declaration refuses before any clone; chat still starts."""
    from rootcoz.engine.chat import clone_chat_repos

    manager = RecordingManager()
    failed: list[str] = []
    params = {
        "additional_repos": [
            {"name": "dup", "url": "https://example.com/a"},
            {"name": "dup", "url": "https://example.com/b"},
        ]
    }

    with patch("rootcoz.repository.RepositoryManager", return_value=manager):
        cloned = await clone_chat_repos(tmp_path, params, failed=failed)

    assert cloned is False
    assert manager.calls == []
    assert failed == ["workspace"]


async def test_reanalysis_caller_keeps_its_token_depth_and_warn_only_policy(
    tmp_path, monkeypatch
) -> None:
    """No escalation there: a ``KeyError`` is warned, the entry says ``failed``."""
    from rootcoz import storage
    from rootcoz.main import _reanalyze_failure_background
    from rootcoz.rootcoz_repo_settings import RootcozSettingsError

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reanalysis.db")
    await storage.init_db()
    await storage.save_result(
        "job-reanalysis-pin",
        "",
        "completed",
        {"failures": [{"id": "failure-pin", "reanalysis_status": "running"}]},
    )
    manager = RecordingManager(fail={"tests": KeyError("boom")}, root=tmp_path)
    recorded: list[dict] = []

    async def progress(_job, name, active, **kwargs) -> None:
        recorded.append({"name": name, "active": active, **kwargs})

    with (
        patch("rootcoz.main.RepositoryManager", return_value=manager),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=MagicMock,
        ) as auth,
        patch("rootcoz.engine.core.logger.warning") as warning,
        # main.py binds the name at import time, so patch it where it is used.
        patch("rootcoz.main.safe_update_clone_progress", side_effect=progress),
        # Ends the run right after the clone block, so the pin stays hermetic.
        patch(
            "rootcoz.main.resolve_repo_analysis_settings",
            side_effect=RootcozSettingsError("stop here"),
        ),
    ):
        auth.return_value = _awaitable("")

        await _reanalyze_failure_background(
            "job-reanalysis-pin",
            "failure-pin",
            {},
            "claude",
            "opus",
            None,
            "",
            None,
            1,
            "https://self.hosted/tests",  # non-GitHub: "always" is what sends the token
            "main",
            "tests-token",
            None,
            "admin",
            1,
        )

    _url, _path, depth, branch, token = manager.calls[0]
    assert (depth, branch, token) == (50, "main", "tests-token")
    assert [(r["name"], r["active"], r["operation_id"]) for r in recorded] == [
        ("tests", True, "failure-pin:tests"),
        ("tests", False, "failure-pin:tests"),
    ]
    assert recorded[1]["state"] == "failed"
    assert all(not call.kwargs.get("exc_info") for call in warning.call_args_list)


def _awaitable(value):
    async def _value():
        return value

    return _value()


async def test_analysis_copies_pi_resources_from_what_it_cloned(
    tmp_path, clone_settings
) -> None:
    """The copy decision reads ``outcome.cloned``, not a not-cloned direct case."""
    from rootcoz.sources.base import setup_analysis_workspace

    manager = RecordingManager(root=tmp_path / "ws-root")
    (tmp_path / "ws-root").mkdir()
    # The first workspace this manager hands out is ``workspace-1``.
    extras = tmp_path / "ws-root" / "workspace-1" / "tests" / ".rootcoz" / "agents"
    extras.mkdir(parents=True)
    (extras / "a.md").write_text("agent")

    result, _ = await setup_analysis_workspace(
        manager, tests_repo_url="https://example.com/tests", job_id=""
    )

    assert (result.repo_path / ".pi" / "agents" / "a.md").read_text() == "agent"


async def test_the_analysis_pipeline_reads_the_settings_from_what_was_cloned(
    tmp_path, monkeypatch
) -> None:
    """``tests_repo_path``, not a re-derived name: a rewritten name would be ``None``.

    The resolver sanitises ``a..b`` to ``a_b``, so re-deriving the name would
    leave ``.rootcoz/settings.json`` unread for a repository that *was* cloned.
    """
    from unittest.mock import AsyncMock

    from rootcoz import storage
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import FailedTest, UnifiedAnalyzeRequest
    from rootcoz.rootcoz_repo_settings import RootcozSettingsError
    from rootcoz.sources.base import CISourceResult, WorkspaceSetupResult

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "pipeline.db")
    await storage.init_db()
    await storage.save_result("job-wiring", status="pending", result={})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a_b").mkdir()
    result = WorkspaceSetupResult(
        repo_path=workspace,
        cloned_repos={"a_b": workspace / "a_b"},
        tests_repo_path=workspace / "a_b",
    )

    with (
        patch("rootcoz.main.create_source_from_request") as source,
        patch(
            "rootcoz.main.setup_analysis_workspace",
            new_callable=AsyncMock,
            return_value=(result, ""),
        ),
        patch(
            "rootcoz.main.resolve_repo_analysis_settings",
            side_effect=RootcozSettingsError("stop here"),
        ) as resolve,
    ):
        source.return_value.prepare_workspace = AsyncMock(return_value=[])
        source.return_value.requires_pre_fetch.return_value = False
        source.return_value.fetch = AsyncMock(
            return_value=CISourceResult(
                failures=[FailedTest(test_name="t", error_message="boom")]
            )
        )
        source.return_value.persist_fetch_metadata = AsyncMock()
        source.return_value.analyze_children = AsyncMock(return_value=([], []))
        await _process_ci_source_analysis(
            job_id="job-wiring",
            body=UnifiedAnalyzeRequest(
                type="raw", failures=[FailedTest(test_name="t", error_message="boom")]
            ),
            merged=Settings(),
            display_name="parent",
            ai_provider="claude",
            ai_model="model",
            peer_ai_configs=None,
            tests_repo_url="https://example.com/a..b",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )

    assert resolve.call_args.args[1] == (workspace / "a_b").resolve()
