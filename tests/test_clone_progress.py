"""Live and persisted repository clone progress."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rootcoz import storage
from rootcoz.engine.core import clone_additional_repos, safe_update_clone_progress
from rootcoz.models import AdditionalRepo
from rootcoz.repository import RepositoryManager


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "aborted", "completed"])
@pytest.mark.parametrize("scheme", ["https", "git"])
async def test_late_clone_updates_do_not_change_terminal_result(
    tmp_path: Path, monkeypatch, status: str, scheme: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {"job_name": "test"})
    await storage.update_clone_progress(
        "job",
        "slow",
        True,
        url=f"{scheme}://user:secret@example.com/slow?token=hidden",  # pragma: allowlist secret
        ref="main",
    )
    await storage.update_status("job", status, {"error": "cancelled"})
    before = await storage.get_result("job")
    assert [
        (e["state"], e["url"], e["ref"]) for e in before["result"]["progress_log"]
    ] == [
        ("cloning", f"{scheme}://example.com/slow", "main"),
        (
            "failed" if status == "failed" else "cancelled",
            f"{scheme}://example.com/slow",
            "main",
        ),
    ]
    assert "secret" not in str(before) and "hidden" not in str(before)

    # Clone completion can race with cancellation, after the terminal write commits.
    await asyncio.gather(
        storage.update_clone_progress("job", "slow", False),
        storage.update_clone_progress("job", "late", True),
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "aborted"])
async def test_reanalysis_clone_cannot_change_failed_or_cancelled_parent(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {"job_name": "test"})
    await storage.update_clone_progress("job", "slow", True, reanalysis=True)
    await storage.update_status("job", status, {"error": "stopped"})
    before = await storage.get_result("job")

    # A cancelled clone can still run its finally cleanup after the terminal commit.
    await asyncio.gather(
        storage.update_clone_progress("job", "slow", False, reanalysis=True),
        storage.update_clone_progress("job", "late", True, reanalysis=True),
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "aborted"])
async def test_active_reanalysis_clone_updates_terminal_parent(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result(
        "job",
        "",
        status,
        {
            "progress_phase": status,
            "failures": [{"id": "failure", "reanalysis_status": "running"}],
        },
    )
    await storage.update_clone_progress(
        "job",
        "tests",
        True,
        reanalysis=True,
        operation_id="failure",
        url="https://user:secret@example.com/tests?token=hidden",  # pragma: allowlist secret
        ref="main",
    )
    started = await storage.get_result("job")
    assert started["status"] == status
    assert started["result"]["progress_phase"] == status
    assert started["result"]["cloning_repos"] == ["tests"]
    await storage.update_clone_progress(
        "job", "tests", False, reanalysis=True, operation_id="failure", state="failed"
    )
    ended = await storage.get_result("job")
    assert ended["status"] == status
    assert ended["result"]["progress_phase"] == status
    assert ended["result"]["cloning_repos"] == []
    assert [
        (e["state"], e["url"], e["ref"]) for e in ended["result"]["progress_log"]
    ] == [
        ("cloning", "https://example.com/tests", "main"),
        ("failed", "https://example.com/tests", "main"),
    ]
    assert "secret" not in str(ended) and "hidden" not in str(ended)
    before = await storage.get_result("job")
    await storage.update_clone_progress(
        "job", "tests", True, reanalysis=True, operation_id="other"
    )
    await storage.update_clone_progress(
        "job", "tests", False, reanalysis=True, operation_id="failure"
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
async def test_concurrent_reanalysis_clones_keep_repo_active_until_both_finish(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result(
        "job",
        "",
        "completed",
        {
            "progress_phase": "completed",
            "failures": [
                {"id": "one", "reanalysis_status": "running"},
                {"id": "two", "reanalysis_status": "running"},
            ],
        },
    )
    await asyncio.gather(
        *(
            storage.update_clone_progress(
                "job",
                "tests",
                True,
                reanalysis=True,
                operation_id=ident,
                url=f"https://user:secret@example.com/{ident}?token=hidden",  # pragma: allowlist secret
                ref=ident,
            )
            for ident in ("one", "two")
        )
    )
    first = (await storage.get_result("job"))["result"]
    assert first["cloning_repos"] == ["tests"]
    assert len(first["progress_log"]) == 2
    await storage.update_clone_progress(
        "job", "tests", False, reanalysis=True, operation_id="one"
    )
    middle = (await storage.get_result("job"))["result"]
    assert middle["cloning_repos"] == ["tests"]
    assert middle["progress_log"][-1]["repos"] == ["tests"]
    await storage.update_clone_progress(
        "job", "tests", False, reanalysis=True, operation_id="two", state="failed"
    )
    last = (await storage.get_result("job"))["result"]
    assert last["cloning_repos"] == []
    assert last["progress_phase"] == "completed"
    assert sorted((e["state"], e.get("ref")) for e in last["progress_log"]) == sorted(
        [("cloning", "one"), ("cloning", "two"), ("cloned", "one"), ("failed", "two")]
    )
    assert "secret" not in str(last) and "hidden" not in str(last)


@pytest.mark.asyncio
async def test_completed_parent_rejects_late_identified_clone_progress(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result(
        "job",
        "",
        "completed",
        {"failures": [{"id": "failure", "reanalysis_status": "running"}]},
    )
    await storage.update_clone_progress(
        "job", "repo", True, reanalysis=True, operation_id="failure:repo"
    )
    await storage.patch_result_json(
        "job", lambda data: data["failures"][0].update(reanalysis_status="completed")
    )
    before = await storage.get_result("job")
    await storage.update_clone_progress(
        "job", "late", True, reanalysis=True, operation_id="failure:late"
    )
    await storage.update_clone_progress(
        "job", "repo", False, reanalysis=True, operation_id="failure:repo"
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
async def test_reanalysis_clone_updates_completed_parent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "completed", {"progress_phase": "completed"})
    await storage.update_clone_progress("job", "repo", True, reanalysis=True)
    result = await storage.get_result("job")
    assert result["status"] == "completed"
    assert result["result"]["cloning_repos"] == ["repo"]
    await storage.update_clone_progress("job", "repo", False, reanalysis=True)
    refreshed = (await storage.get_result("job"))["result"]
    assert refreshed["cloning_repos"] == []
    assert refreshed["progress_phase"] == "completed"
    assert [(e["repo"], e["state"]) for e in refreshed["progress_log"]] == [
        ("repo", "cloning"),
        ("repo", "cloned"),
    ]


@pytest.mark.asyncio
async def test_clone_progress_notifies_sse_after_persist() -> None:
    notifications: list[str] = []

    async def persist(job_id: str, _name: str, _started: bool, **_kwargs) -> None:
        assert job_id == "job"
        notifications.append("persisted")

    with (
        patch("rootcoz.engine.core.update_clone_progress", side_effect=persist),
        patch(
            "rootcoz.engine.core._on_progress_updated",
            lambda _job: notifications.append("sse"),
        ),
    ):
        await safe_update_clone_progress("job", "tests", True)
    assert notifications == ["persisted", "sse"]


@pytest.mark.asyncio
async def test_parallel_clone_progress_and_failure(tmp_path: Path) -> None:
    """Both admitted clones appear; failed clones leave the active set."""
    repos = [
        AdditionalRepo(name=name, url=f"https://example.com/{name}")
        for name in ("one", "two")
    ]
    manager = MagicMock(spec=RepositoryManager)
    entered = asyncio.Event()
    release = asyncio.Event()
    active: set[str] = set()
    snapshots: list[set[str]] = []

    async def progress(
        _job_id: str,
        name: str,
        started: bool,
        *,
        state: str = "cloned",
        reanalysis: bool = False,
        url: str = "",
        ref: str = "",
    ) -> None:
        if started:
            active.add(name)
        else:
            active.discard(name)
        snapshots.append(set(active))

    async def clone(_fn, _url, target, **_kwargs):
        if len(active) == 2:
            entered.set()
        await release.wait()
        if target.name == "two":
            raise RuntimeError("clone failed")

    with (
        patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone),
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
    ):
        task = asyncio.create_task(
            clone_additional_repos(manager, repos, tmp_path, job_id="job")
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert {"one", "two"} in snapshots
        finally:
            release.set()
        cloned, _ = await task
    assert set(cloned) == {"one"}
    assert active == set()


@pytest.mark.asyncio
async def test_clone_events_survive_refresh_with_sanitized_url_and_ref(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    await asyncio.gather(
        storage.update_clone_progress(
            "job",
            "one",
            True,
            url="https://user:secret@example.com/one?token=hidden#fragment",  # pragma: allowlist secret
            ref="main",
        ),
        storage.update_clone_progress(
            "job", "two", True, url="git@example.com:two", ref="dev"
        ),
    )
    await storage.update_clone_progress("job", "one", True)
    await asyncio.gather(
        storage.update_clone_progress("job", "one", False, state="cloned"),
        storage.update_clone_progress("job", "two", False, state="failed"),
    )
    await storage.update_clone_progress("job", "two", False, state="failed")
    result = (await storage.get_result("job"))["result"]
    assert result["cloning_repos"] == []
    assert sorted((e["repo"], e["state"]) for e in result["progress_log"]) == [
        ("one", "cloned"),
        ("one", "cloning"),
        ("two", "cloning"),
        ("two", "failed"),
    ]
    assert all(
        e["phase"] == "cloning" and "timestamp" in e for e in result["progress_log"]
    )
    assert [
        (e.get("url"), e.get("ref"))
        for e in result["progress_log"]
        if e["repo"] == "one"
    ] == [
        ("https://example.com/one", "main"),
        ("https://example.com/one", "main"),
    ]
    assert all("url" not in e for e in result["progress_log"] if e["repo"] == "two")
    assert "secret" not in str(result) and "hidden" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["cloned", "failed", "cancelled"])
async def test_git_clone_url_survives_terminal_transition_and_refresh(
    tmp_path: Path, monkeypatch, state: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    await storage.update_clone_progress(
        "job",
        "tests",
        True,
        url="git://user:secret@example.com/tests.git?token=hidden#fragment",  # pragma: allowlist secret
        ref="main",
    )
    await storage.update_clone_progress("job", "tests", False, state=state)
    result = (await storage.get_result("job"))["result"]
    assert result["cloning_repos"] == []
    assert [(e["state"], e["url"], e["ref"]) for e in result["progress_log"]] == [
        ("cloning", "git://example.com/tests.git", "main"),
        (state, "git://example.com/tests.git", "main"),
    ]
    assert "secret" not in str(result) and "hidden" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/repo",
        "git:///repo",
        "git://[broken/repo",
        "git@example.com:repo",
        "git://example.com:bad/repo",
        "git://bad host/repo",
    ],
)
async def test_clone_progress_withholds_malformed_or_unsafe_url(
    tmp_path: Path, monkeypatch, url: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    await storage.update_clone_progress("job", "repo", True, url=url)
    await storage.update_clone_progress("job", "repo", False)
    assert all(
        "url" not in entry
        for entry in (await storage.get_result("job"))["result"]["progress_log"]
    )


@pytest.mark.asyncio
async def test_additional_clone_failure_persists_named_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    manager = MagicMock(spec=RepositoryManager)

    def clone(_url, target, **_kwargs):
        if target.name == "two":
            raise RuntimeError("clone failed")

    manager.clone_into.side_effect = clone
    repos = [
        AdditionalRepo(
            name=name,
            url=f"https://user:secret@example.com/{name}?token=hidden",  # pragma: allowlist secret
            ref="main",
        )
        for name in ("one", "two")
    ]
    cloned, _ = await clone_additional_repos(manager, repos, tmp_path, job_id="job")
    result = (await storage.get_result("job"))["result"]
    assert set(cloned) == {"one"}
    assert result["cloning_repos"] == []
    assert sorted((e["repo"], e["state"]) for e in result["progress_log"]) == [
        ("one", "cloned"),
        ("one", "cloning"),
        ("two", "cloning"),
        ("two", "failed"),
    ]
    assert all(
        e["url"] == f"https://example.com/{e['repo']}" and e["ref"] == "main"
        for e in result["progress_log"]
    )
    assert "secret" not in str(result) and "hidden" not in str(result)


@pytest.mark.asyncio
async def test_cancel_additional_clone_persists_cancelled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    started, release = asyncio.Event(), asyncio.Event()

    async def clone(*_args, **_kwargs):
        started.set()
        await release.wait()

    with patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone):
        task = asyncio.create_task(
            clone_additional_repos(
                MagicMock(spec=RepositoryManager),
                [AdditionalRepo(name="one", url="https://example.com/one")],
                tmp_path,
                job_id="job",
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            result = (await storage.get_result("job"))["result"]
            assert result["cloning_repos"] == []
            assert [(e["repo"], e["state"]) for e in result["progress_log"]] == [
                ("one", "cloning"),
                ("one", "cancelled"),
            ]
        finally:
            release.set()


@pytest.mark.asyncio
async def test_abort_marks_active_clones_cancelled_once(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    await storage.update_clone_progress(
        "job",
        "one",
        True,
        url="https://user:secret@example.com/one?token=hidden",  # pragma: allowlist secret
        ref="main",
    )
    await storage.update_clone_progress(
        "job", "two", True, url="https://example.com/two", ref="dev"
    )
    await storage.update_status("job", "aborted", {"error": "stopped"})
    before = await storage.get_result("job")
    await storage.update_clone_progress("job", "one", False, state="cancelled")
    assert await storage.get_result("job") == before
    assert before["result"]["cloning_repos"] == []
    assert [
        (e["repo"], e["state"], e["url"], e["ref"])
        for e in before["result"]["progress_log"]
    ] == [
        ("one", "cloning", "https://example.com/one", "main"),
        ("two", "cloning", "https://example.com/two", "dev"),
        ("one", "cancelled", "https://example.com/one", "main"),
        ("two", "cancelled", "https://example.com/two", "dev"),
    ]
    assert "secret" not in str(before) and "hidden" not in str(before)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["aborted", "failed"])
async def test_terminal_status_ends_every_active_clone_operation(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result(
        "job",
        "",
        "running",
        {
            "failures": [
                {"id": ident, "reanalysis_status": "running"}
                for ident in ("one", "two", "done")
            ]
        },
    )
    await storage.update_clone_progress(
        "job", "tests", True, url="https://example.com/legacy", ref="old"
    )
    for ident in ("one", "two", "done"):
        await storage.update_clone_progress(
            "job",
            "tests",
            True,
            reanalysis=True,
            operation_id=ident,
            url=f"https://example.com/{ident}",
            ref=ident,
        )
    await storage.update_clone_progress(
        "job", "tests", False, reanalysis=True, operation_id="done"
    )
    await storage.update_status("job", status, {"error": "stopped"})
    before = await storage.get_result("job")
    result = before["result"]
    assert result["cloning_repos"] == []
    assert result["progress_phase"] == status
    assert [
        (e.get("operation_id"), e["state"], e.get("url"), e.get("ref"))
        for e in result["progress_log"][-3:]
    ] == [
        (
            "one",
            "failed" if status == "failed" else "cancelled",
            "https://example.com/one",
            "one",
        ),
        (
            "two",
            "failed" if status == "failed" else "cancelled",
            "https://example.com/two",
            "two",
        ),
        (
            None,
            "failed" if status == "failed" else "cancelled",
            "https://example.com/legacy",
            "old",
        ),
    ]
    for ident in ("one", "two"):
        await storage.update_clone_progress(
            "job", "tests", False, reanalysis=True, operation_id=ident
        )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
async def test_cancel_during_start_progress_releases_clone_slot(tmp_path: Path) -> None:
    from rootcoz.engine import core

    manager = MagicMock(spec=RepositoryManager)
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def progress(
        _job: str,
        _name: str,
        started: bool,
        *,
        state: str = "cloned",
        reanalysis: bool = False,
        url: str = "",
        ref: str = "",
    ) -> None:
        if started:
            entered.set()
            await blocker.wait()

    with (
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
        patch("rootcoz.engine.core.asyncio.to_thread") as worker,
    ):
        task = asyncio.create_task(
            clone_additional_repos(
                manager,
                [AdditionalRepo(name="one", url="https://example.com/one")],
                tmp_path,
                job_id="job",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        before = core._active_clones
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert core._active_clones == before - 1
        worker.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_clone_clears_active_name(tmp_path: Path) -> None:
    """A cancelled analysis leaves no stale active repo in progress."""
    manager = MagicMock(spec=RepositoryManager)
    started = asyncio.Event()
    release = asyncio.Event()
    active: set[str] = set()

    async def progress(
        _job_id: str,
        name: str,
        running: bool,
        *,
        state: str = "cloned",
        reanalysis: bool = False,
        url: str = "",
        ref: str = "",
    ) -> None:
        if running:
            active.add(name)
        else:
            active.discard(name)

    async def clone(*_args, **_kwargs):
        started.set()
        await release.wait()

    with (
        patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone),
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
    ):
        task = asyncio.create_task(
            clone_additional_repos(
                manager,
                [AdditionalRepo(name="one", url="https://example.com/one")],
                tmp_path,
                job_id="job",
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        assert active == {"one"}
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert active == set()
        finally:
            release.set()
