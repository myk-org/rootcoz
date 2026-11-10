"""The analysis slot cap through the routes and a real pipeline (#544).

Every case drives the product path: the route, the stored row, the gate inside
``_process_ci_source_analysis``. Only the AI, the workspace and the sidecar
preflight are faked -- they are what makes a pipeline take time, not what decides
whether it takes a slot.
"""

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rootcoz import analysis_slots, main, storage
from rootcoz.models import AnalysisDetail, FailureAnalysis
from rootcoz.sources.base import WorkspaceSetupResult
from tests.conftest import make_app_client

ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_REAL_AUTO_REVIEW = main._auto_review_matching_failures
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}


class _SlotHold:
    """Park a named job at the first await below the gate until released.

    Holding inside ``source.fetch`` would not do: a job parked there has not
    acquired a slot yet, so with a cap of 1 the *second* poster would take it and
    every queued assertion would describe the wrong job.
    """

    def __init__(self) -> None:
        self._released = threading.Event()
        self._entered: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.held: set[str] = set()

    def enter(self, name: str) -> threading.Event:
        with self._lock:
            return self._entered.setdefault(name, threading.Event())

    def release(self, name: str) -> None:
        with self._lock:
            self.held.discard(name)
        self._released.set()

    def release_all(self) -> None:
        with self._lock:
            self.held.clear()
        self._released.set()

    async def wait(self, job_id: str, display_name: str) -> None:
        if display_name not in self.held:
            return
        self.enter(display_name).set()
        while not self._released.is_set():
            await asyncio.sleep(0.01)

    def hold(self, *names: str) -> None:
        with self._lock:
            self.held.update(names)
            self._released.clear()


@pytest.fixture
def slot_limit(monkeypatch: pytest.MonkeyPatch):
    def _set(limit: int) -> None:
        monkeypatch.setattr(
            analysis_slots,
            "get_settings",
            lambda: SimpleNamespace(max_concurrent_analyses=limit),
        )

    return _set


@pytest.fixture
def hold() -> _SlotHold:
    return _SlotHold()


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hold: _SlotHold
) -> None:
    async def _preflight(job_id, *_args, display_name="", **_kwargs):
        await hold.wait(job_id, display_name)
        return True

    def _fake_preflight(job_id, _provider, _model, display_name="", **_kwargs):
        return _preflight(job_id, display_name=display_name)

    monkeypatch.setattr(main, "_preflight_sidecar_check", _fake_preflight)
    monkeypatch.setattr(
        main,
        "setup_analysis_workspace",
        AsyncMock(return_value=(WorkspaceSetupResult(tmp_path), "")),
    )
    monkeypatch.setattr(
        main,
        "run_orchestrated_analysis",
        AsyncMock(
            return_value=(
                [
                    FailureAnalysis(
                        test_name="t",
                        error="boom",
                        error_signature="sig",
                        analysis=AnalysisDetail(details="diagnosed"),
                    )
                ],
                [],
            )
        ),
    )
    monkeypatch.setattr(main, "_reap_analysis_workspace", AsyncMock())
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    monkeypatch.setattr(main, "_auto_review_matching_failures", AsyncMock())
    monkeypatch.setattr(main, "_auto_assign_metadata", AsyncMock())
    monkeypatch.setattr(
        main, "_validate_catalog_pair", AsyncMock(return_value=("claude", "model"))
    )
    monkeypatch.setattr(
        main,
        "_resolve_ai_config_allow_defer",
        AsyncMock(return_value=("claude", "model")),
    )
    monkeypatch.setattr(main, "_validate_peer_configs", AsyncMock(return_value=None))
    monkeypatch.setattr(storage, "make_classifications_visible", AsyncMock())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, temp_db_path: Path, tmp_path: Path, hold):
    _patch_pipeline(monkeypatch, tmp_path, hold)
    yield from make_app_client(temp_db_path)


@pytest.fixture
def auto_review_client(
    monkeypatch: pytest.MonkeyPatch, temp_db_path: Path, tmp_path: Path, hold
):
    _patch_pipeline(monkeypatch, tmp_path, hold)
    yield from make_app_client(temp_db_path, {"ENABLE_AUTO_REVIEW": "true"})


def _body(name: str, *, failures: bool = True) -> dict:
    body: dict = {
        "type": "raw",
        "name": name,
        "ai_provider": "claude",
        "ai_model": "model",
    }
    if failures:
        body["failures"] = [{"test_name": "t", "error_message": "boom"}]
    else:
        body["passed_tests"] = [{"test_name": "ok", "status": "passed"}]
    return body


def _job(client, job_id: str) -> dict:
    response = client.get(f"/results/{job_id}", headers=AUTH)
    assert response.status_code in (200, 202), response.text
    return response.json()


def _wait_for(client, job_id: str, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _job(client, job_id)
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached the expected state: {last}")


def _posted(client, body: dict) -> str:
    response = client.post("/analyze", json=body, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_a_submission_over_the_cap_is_queued_then_started(
    client, hold, slot_limit
) -> None:
    """T10, T11, T13b: accepted, left ``pending`` with ``queue``, then running."""
    slot_limit(1)
    hold.hold("A")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15), "the first analysis never reached the gate"

    b_id = _posted(client, _body("B"))
    queued = _wait_for(
        client,
        b_id,
        lambda job: job["status"] == "pending" and job.get("queue"),
    )
    assert queued["queue"] == {
        "position": 1,
        "running": 1,
        "limit": 1,
        "message": "waiting for a free analysis slot (1 running, limit 1)",
    }
    # T13b: a job that never waited carries no queue object at all.
    assert "queue" not in _job(client, a_id)
    assert analysis_slots.waiter_count() == 1
    # ... and the queue object is selectable as a sparse field.
    sparse = client.get(f"/results/{b_id}", params={"fields": "queue"}, headers=AUTH)
    assert sparse.status_code in (200, 202), sparse.text
    assert sparse.json() == {"queue": queued["queue"]}

    hold.release("A")
    started = _wait_for(client, b_id, lambda job: job["status"] == "running")
    assert "queue" not in started
    _wait_for(client, b_id, lambda job: job["status"] == "completed")
    _wait_for(client, a_id, lambda job: job["status"] == "completed")
    assert analysis_slots.running_count() == 0
    assert analysis_slots.waiter_count() == 0


def test_an_ingest_only_submit_takes_no_slot(client, hold, slot_limit) -> None:
    """T13: ``POST /submit`` is accepted while the cap is saturated, unqueued."""
    slot_limit(1)
    hold.hold("A")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)

    response = client.post("/submit", json=_body("submitted"), headers=AUTH)
    assert response.status_code == 202, response.text
    submit_id = response.json()["job_id"]

    _wait_for(client, submit_id, lambda job: job["status"] == "completed")
    assert analysis_slots.waiter_count() == 0
    assert analysis_slots.snapshot(submit_id)["position"] is None
    assert analysis_slots.running_count() == 1  # only A

    hold.release_all()
    _wait_for(client, a_id, lambda job: job["status"] == "completed")


def test_a_skip_analysis_job_takes_no_slot(client, hold, slot_limit) -> None:
    """T13d: a job with no failures skips AI, so it never queues for a slot."""
    slot_limit(1)
    hold.hold("A")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)

    clean_id = _posted(client, _body("clean", failures=False))
    done = _wait_for(client, clean_id, lambda job: job["status"] == "completed")
    assert "queue" not in done
    assert analysis_slots.waiter_count() == 0
    assert analysis_slots.running_count() == 1  # still only A

    hold.release_all()
    _wait_for(client, a_id, lambda job: job["status"] == "completed")


def test_aborting_a_slot_holder_frees_it_for_the_waiter(
    client, hold, slot_limit
) -> None:
    """T13e: the production ``CancelledError`` exit hands the slot to the waiter."""
    slot_limit(1)
    hold.hold("A", "B")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)
    b_id = _posted(client, _body("B"))
    _wait_for(client, b_id, lambda job: job["status"] == "pending" and job.get("queue"))

    response = client.post(f"/results/{a_id}/abort", headers=AUTH)
    assert response.status_code == 200, response.text
    assert _job(client, a_id)["status"] == "aborted"

    started = _wait_for(client, b_id, lambda job: job["status"] == "running")
    assert "queue" not in started
    assert analysis_slots.running_count() == 1

    hold.release("B")
    _wait_for(client, b_id, lambda job: job["status"] == "completed")
    assert analysis_slots.running_count() == 0


def test_a_deleted_queued_job_writes_nothing_and_leaks_no_slot(
    client, hold, slot_limit
) -> None:
    """T15: the settled decision — no queue surgery, the writes are refused."""
    slot_limit(1)
    hold.hold("A")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)

    b_id = _posted(client, _body("B"))
    _wait_for(client, b_id, lambda job: job["status"] == "pending" and job.get("queue"))
    assert client.delete(f"/results/{b_id}", headers=AUTH).status_code == 200

    hold.release_all()
    _wait_for(client, a_id, lambda job: job["status"] == "completed")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (
        analysis_slots.running_count() or analysis_slots.waiter_count()
    ):
        time.sleep(0.02)

    assert analysis_slots.running_count() == 0
    assert analysis_slots.waiter_count() == 0
    assert client.get(f"/results/{b_id}", headers=AUTH).status_code == 404


def test_the_queued_write_cannot_revive_an_aborted_job(client) -> None:
    """T15b: ``expect_status`` refuses the queued write, so a boot cannot resume it."""
    job_id = "aborted-while-queued"
    import anyio

    async def _store() -> None:
        await storage.save_result(
            job_id,
            "http://ci/1",
            "pending",
            {"job_name": "aborted-while-queued", "request_params": {"type": "raw"}},
        )
        await storage.update_status(job_id, "aborted", {"error": "user aborted"})

    anyio.run(_store)
    assert client.get(f"/results/{job_id}", headers=AUTH).json()["status"] == "aborted"

    anyio.run(main._mark_queued_for_slot, job_id)

    assert client.get(f"/results/{job_id}", headers=AUTH).json()["status"] == "aborted"


def test_a_slot_is_released_when_the_reap_raises(
    client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T13c: release is the first statement of ``finally``, so a raise cannot leak it."""
    monkeypatch.setattr(
        main,
        "_reap_analysis_workspace",
        AsyncMock(side_effect=RuntimeError("reap failed")),
    )
    job_id = "reap-raises"
    import anyio

    async def _run() -> None:
        await storage.save_result(
            job_id, "http://ci/1", "pending", {"job_name": "reap-raises"}
        )
        with pytest.raises(RuntimeError):
            await main._process_ci_source_analysis(
                job_id=job_id,
                body=main.UnifiedAnalyzeRequest(
                    type="raw", failures=[{"test_name": "t", "error_message": "boom"}]
                ),
                merged=main.get_settings(),
                display_name="reap",
                ai_provider="claude",
                ai_model="model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )

    anyio.run(_run)
    assert analysis_slots.running_count() == 0
    assert analysis_slots.waiter_count() == 0


# -- the two re-analysis paths that must count against the cap -----------------


def test_a_whole_job_re_analysis_waits_for_a_slot(client, hold, slot_limit) -> None:
    """T12: ``POST /re-analyze/{id}`` enqueues a *new* job behind the cap."""
    slot_limit(1)
    hold.hold("A", "re-run")
    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)

    response = client.post(f"/re-analyze/{a_id}", json={}, headers=AUTH)
    assert response.status_code == 202, response.text
    rerun_id = response.json()["job_id"]
    assert rerun_id != a_id

    queued = _wait_for(
        client, rerun_id, lambda job: job["status"] == "pending" and job.get("queue")
    )
    assert queued["queue"]["position"] == 1
    hold.release_all()
    _wait_for(client, rerun_id, lambda job: job["status"] == "completed")


def test_an_in_place_analyse_waits_for_a_slot(client, hold, slot_limit) -> None:
    """T12b: analysing a submitted job in place reuses its id and queues too."""
    slot_limit(1)
    hold.hold("A", "held")
    submitted = client.post("/submit", json=_body("held"), headers=AUTH)
    assert submitted.status_code == 202, submitted.text
    submitted_id = submitted.json()["job_id"]
    _wait_for(client, submitted_id, lambda job: job["status"] == "completed")

    a_id = _posted(client, _body("A"))
    assert hold.enter("A").wait(timeout=15)

    response = client.post(f"/results/{submitted_id}/analyze", json={}, headers=AUTH)
    assert response.status_code in (200, 202), response.text
    queued = _wait_for(
        client,
        submitted_id,
        lambda job: job["status"] == "pending" and job.get("queue"),
    )
    assert queued["queue"]["position"] == 1

    hold.release_all()
    _wait_for(client, submitted_id, lambda job: job["status"] == "completed")
    _wait_for(client, a_id, lambda job: job["status"] == "completed")


# -- a per-failure re-analysis is the third gated path ------------------------

FAILURE_UUID = "123e4567-e89b-12d3-a456-426614174000"


async def _seed_parent(job_id: str, *, status: str = "completed") -> None:
    await storage.save_result(
        job_id,
        "http://ci/1",
        status,
        {
            "job_name": job_id,
            "display_name": job_id,
            "build_number": 1,
            "failure_count": 1,
            "failures": [
                {
                    "id": FAILURE_UUID,
                    "test_name": "t",
                    "error": "boom",
                    "error_signature": "sig",
                    "reanalysis_status": "running",
                    "analysis": {"classification": "CODE ISSUE", "details": "d"},
                }
            ],
            "request_params": {"type": "raw", "submitted_by": "admin"},
        },
    )


@pytest.fixture
def reanalysis_env(monkeypatch: pytest.MonkeyPatch, initialized_db, tmp_path: Path):
    """Fake everything one failure re-analysis does except the slot gate."""
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    manager = MagicMock()
    manager.create_workspace.return_value = tmp_path
    monkeypatch.setattr(main, "RepositoryManager", lambda: manager)
    monkeypatch.setattr(
        main,
        "resolve_repo_analysis_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                ai_provider="claude",
                ai_model="model",
                peer_ai_configs=None,
                additional_repos=[],
                settings=SimpleNamespace(
                    peer_analysis_max_rounds=3,
                    ai_call_timeout=None,
                    max_concurrent_ai_calls=3,
                ),
            )
        ),
    )
    ai = AsyncMock(
        return_value=[AnalysisDetail(classification="CODE ISSUE", details="d")]
    )
    monkeypatch.setattr(main, "analyze_failure_group", ai)
    monkeypatch.setattr(main, "_reap_analysis_workspace", AsyncMock())
    monkeypatch.setattr(main, "notify_job_status_changed", MagicMock())
    return SimpleNamespace(manager=manager, ai=ai)


def _reanalyze(job_id: str):
    return main._reanalyze_failure_background(
        job_id=job_id,
        failure_uuid=FAILURE_UUID,
        failure_dict={"test_name": "t", "error": "boom"},
        ai_provider="claude",
        ai_model="model",
        ai_call_timeout=None,
        raw_prompt="",
        peer_ai_configs=None,
        peer_analysis_max_rounds=3,
        tests_repo_url="",
        tests_repo_ref="",
        tests_repo_token="",
        additional_repos_list=None,
        username="admin",
        max_concurrent_ai_calls=3,
    )


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


def _failure(job: dict) -> dict:
    return job["result"]["failures"][0]


async def test_a_per_failure_re_analysis_waits_for_a_slot(
    reanalysis_env, slot_limit
) -> None:
    """T12c: the third gated path queues, creates no workspace, then runs."""
    slot_limit(1)
    await _seed_parent("parent")
    await analysis_slots.acquire("holder")

    task = asyncio.create_task(_reanalyze("parent"))
    await _until(lambda: analysis_slots.waiter_count() == 1)
    assert reanalysis_env.manager.create_workspace.call_count == 0

    analysis_slots.release("holder")
    await task
    assert reanalysis_env.manager.create_workspace.call_count == 1
    assert reanalysis_env.ai.call_count == 1
    assert analysis_slots.running_count() == 0
    assert analysis_slots.waiter_count() == 0


async def test_a_queued_per_failure_re_analysis_cancelled_is_not_a_spinner(
    reanalysis_env, slot_limit
) -> None:
    """T12d: cancelled before admission ends as ``failed``, so the retry is live."""
    slot_limit(1)
    await _seed_parent("parent")
    await analysis_slots.acquire("holder")

    task = asyncio.create_task(_reanalyze("parent"))
    await _until(lambda: analysis_slots.waiter_count() == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    failure = _failure(await storage.get_result("parent"))
    assert failure["reanalysis_status"] == "failed"
    assert "queued" in failure["reanalysis_error"]
    assert reanalysis_env.ai.call_count == 0


async def test_a_per_failure_re_analysis_of_an_aborted_job_does_no_work(
    reanalysis_env, slot_limit
) -> None:
    """T12e: admitted after the parent was aborted — no AI call, no rewrite."""
    slot_limit(1)
    await _seed_parent("parent")
    await analysis_slots.acquire("holder")

    task = asyncio.create_task(_reanalyze("parent"))
    await _until(lambda: analysis_slots.waiter_count() == 1)
    await storage.update_status("parent", "aborted")

    analysis_slots.release("holder")
    await task

    job = await storage.get_result("parent")
    assert job["status"] == "aborted"
    assert reanalysis_env.ai.call_count == 0
    failure = _failure(job)
    assert failure["reanalysis_status"] == "failed"
    assert "aborted" in failure["reanalysis_error"]
    assert analysis_slots.running_count() == 0


async def test_a_cancel_during_the_parent_reread_frees_the_slot(
    reanalysis_env, slot_limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12f: one acquire, exactly one release, even cancelled after admission."""
    slot_limit(1)
    await _seed_parent("parent")
    entered = asyncio.Event()
    real_get_result = main.get_result
    calls = {"release": 0}
    real_release = analysis_slots.release

    def _counting_release(key: str) -> None:
        calls["release"] += 1
        real_release(key)

    async def _blocking_get_result(job_id: str, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return await real_get_result(job_id, **kwargs)

    monkeypatch.setattr(main, "get_result", _blocking_get_result)
    monkeypatch.setattr(analysis_slots, "release", _counting_release)

    task = asyncio.create_task(_reanalyze("parent"))
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert analysis_slots.running_count() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert analysis_slots.running_count() == 0
    assert calls["release"] == 1
    assert reanalysis_env.ai.call_count == 0


async def _seed_reviewed_history(job_name: str, test_name: str) -> None:
    """A previous analysis of the same test, human-reviewed, same signature."""
    await storage.save_result(
        "previous-run",
        "http://ci/0",
        "completed",
        {
            "job_name": job_name,
            "build_number": 1,
            "failure_count": 1,
            "failures": [
                {
                    "test_name": test_name,
                    "error": "boom",
                    "error_signature": "sig",
                    "error_signature_v2": "sig",
                    "analysis": {"classification": "CODE ISSUE", "details": "known"},
                }
            ],
            "request_params": {},
        },
    )
    await storage.populate_failure_history(
        "previous-run", await _stored("previous-run")
    )
    await storage.set_reviewed("previous-run", test_name, True, username="human")


async def _stored(job_id: str) -> dict:
    return (await storage.get_result(job_id, strip_sensitive=False))["result"]


def test_auto_review_takes_no_slot(
    auto_review_client, hold, slot_limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13f: the auto-review pass runs and the run acquires exactly one slot."""
    import anyio

    slot_limit(1)
    # The real pass, not the fixture's stub: a stubbed call count proves nothing.
    monkeypatch.setattr(main, "_auto_review_matching_failures", _REAL_AUTO_REVIEW)
    anyio.run(_seed_reviewed_history, "autoreview", "t")
    applied = AsyncMock()
    monkeypatch.setattr(main, "_apply_auto_review", applied)
    acquire = AsyncMock(wraps=analysis_slots.acquire)
    monkeypatch.setattr(analysis_slots, "acquire", acquire)
    hold.hold("autoreview")

    job_id = _posted(auto_review_client, _body("autoreview"))
    assert hold.enter("autoreview").wait(timeout=15)
    hold.release_all()
    _wait_for(auto_review_client, job_id, lambda job: job["status"] == "completed")

    assert applied.call_count == 1, "auto-review never ran, so the count proves nothing"
    assert acquire.call_count == 1
    assert analysis_slots.waiter_count() == 0
