"""Tests for the @mentions feature (storage, API endpoints).

Cross-stack parity tests at the bottom must stay in sync with:
  frontend/src/pages/report/__tests__/CommentsSection.test.tsx
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from jenkins_job_insight import storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Mock settings for endpoint tests."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
    }
    with patch.dict(os.environ, env, clear=True):
        from jenkins_job_insight.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(mock_settings, temp_db_path: Path):
    """Create a test client with mocked dependencies."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from jenkins_job_insight.main import app

        with TestClient(app) as client:
            yield client


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _add_comment(
    db_path: Path,
    comment: str,
    username: str = "author",
    job_id: str = "job-1",
    test_name: str = "test_one",
) -> int:
    """Insert a comment and return its id."""
    with patch.object(storage, "DB_PATH", db_path):
        return await storage.add_comment(
            job_id=job_id,
            test_name=test_name,
            comment=comment,
            username=username,
        )


# ===========================================================================
# Storage tests
# ===========================================================================


class TestGetMentionsForUser:
    """Tests for storage.get_mentions_for_user."""

    async def test_get_mentions_for_user(self, setup_test_db: Path) -> None:
        """Comments mentioning a user appear in results."""
        db = setup_test_db
        await _add_comment(db, "Hey @alice please look", username="bob")
        await _add_comment(db, "No mention here", username="bob")

        with patch.object(storage, "DB_PATH", db):
            result = await storage.get_mentions_for_user("alice")

        assert result["total"] == 1
        assert result["mentions"][0]["comment"] == "Hey @alice please look"
        assert result["mentions"][0]["username"] == "bob"

    async def test_get_mentions_includes_self(self, setup_test_db: Path) -> None:
        """Self-mentions (user mentioning themselves) are included."""
        db = setup_test_db
        await _add_comment(db, "I am @alice", username="alice")
        await _add_comment(db, "Hey @alice", username="bob")

        with patch.object(storage, "DB_PATH", db):
            result = await storage.get_mentions_for_user("alice")

        assert result["total"] == 2
        usernames = {m["username"] for m in result["mentions"]}
        assert "alice" in usernames
        assert "bob" in usernames

    async def test_get_mentions_pagination(self, setup_test_db: Path) -> None:
        """Offset and limit control pagination correctly."""
        db = setup_test_db
        for i in range(5):
            await _add_comment(
                db, f"Comment {i} cc @alice", username="bob", job_id=f"job-{i}"
            )

        with patch.object(storage, "DB_PATH", db):
            page1 = await storage.get_mentions_for_user("alice", offset=0, limit=2)
            page2 = await storage.get_mentions_for_user("alice", offset=2, limit=2)
            page3 = await storage.get_mentions_for_user("alice", offset=4, limit=2)

        assert page1["total"] == 5
        assert len(page1["mentions"]) == 2
        assert len(page2["mentions"]) == 2
        assert len(page3["mentions"]) == 1

    async def test_word_boundary_filtering(self, setup_test_db: Path) -> None:
        """LIKE '%@al%' should not match @alice when querying for 'al'."""
        db = setup_test_db
        await _add_comment(db, "Hi @alice", username="bob")
        await _add_comment(db, "Hi @al", username="bob")

        with patch.object(storage, "DB_PATH", db):
            result = await storage.get_mentions_for_user("al")

        # Only '@al' is an exact mention, '@alice' should not match 'al'
        assert result["total"] == 1
        assert "@al" in result["mentions"][0]["comment"]

    async def test_get_mentions_unread_only_filters_read(
        self, setup_test_db: Path
    ) -> None:
        """unread_only=True excludes already-read mentions."""
        db = setup_test_db
        cid_read = await _add_comment(db, "Hey @alice read", username="bob")
        await _add_comment(db, "Hey @alice unread", username="charlie")

        with patch.object(storage, "DB_PATH", db):
            await storage.mark_mentions_read("alice", [cid_read])
            result = await storage.get_mentions_for_user("alice", unread_only=True)

        assert result["total"] == 1
        assert result["mentions"][0]["comment"] == "Hey @alice unread"
        assert result["mentions"][0]["is_read"] is False


class TestMarkMentionsRead:
    """Tests for storage.mark_mentions_read."""

    async def test_mark_mentions_read(self, setup_test_db: Path) -> None:
        """Marking a mention read sets is_read to True."""
        db = setup_test_db
        cid = await _add_comment(db, "Hey @alice check this", username="bob")

        with patch.object(storage, "DB_PATH", db):
            before = await storage.get_mentions_for_user("alice")
            assert before["mentions"][0]["is_read"] is False

            await storage.mark_mentions_read("alice", [cid])

            after = await storage.get_mentions_for_user("alice")
            assert after["mentions"][0]["is_read"] is True

    async def test_mark_mentions_read_idempotent(self, setup_test_db: Path) -> None:
        """Marking already-read mentions again does not error."""
        db = setup_test_db
        cid = await _add_comment(db, "Hey @alice", username="bob")

        with patch.object(storage, "DB_PATH", db):
            await storage.mark_mentions_read("alice", [cid])
            # Second call should not raise
            await storage.mark_mentions_read("alice", [cid])

            result = await storage.get_mentions_for_user("alice")
            assert result["mentions"][0]["is_read"] is True

    async def test_mark_empty_list_is_noop(self, setup_test_db: Path) -> None:
        """Passing an empty list is a safe no-op."""
        db = setup_test_db
        with patch.object(storage, "DB_PATH", db):
            await storage.mark_mentions_read("alice", [])  # should not raise


class TestGetUnreadMentionCount:
    """Tests for storage.get_unread_mention_count."""

    async def test_get_unread_mention_count(self, setup_test_db: Path) -> None:
        """Count matches number of unread mentions."""
        db = setup_test_db
        cid1 = await _add_comment(db, "Hey @alice first", username="bob")
        await _add_comment(db, "Hey @alice second", username="charlie")

        with patch.object(storage, "DB_PATH", db):
            assert await storage.get_unread_mention_count("alice") == 2

            await storage.mark_mentions_read("alice", [cid1])
            assert await storage.get_unread_mention_count("alice") == 1

    async def test_unread_count_includes_self(self, setup_test_db: Path) -> None:
        """Self-mentions are counted."""
        db = setup_test_db
        await _add_comment(db, "I mention @alice myself", username="alice")
        await _add_comment(db, "Hey @alice", username="bob")

        with patch.object(storage, "DB_PATH", db):
            assert await storage.get_unread_mention_count("alice") == 2


# ===========================================================================
# API endpoint tests
# ===========================================================================


class TestGetMentionsEndpoint:
    """Tests for GET /api/users/mentions."""

    async def test_get_mentions_endpoint(self, test_client, temp_db_path: Path) -> None:
        """Authenticated user gets their mentions."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _add_comment(temp_db_path, "Hey @testuser look", username="bob")

        test_client.cookies.set("jji_username", "testuser")
        response = test_client.get("/api/users/mentions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["mentions"][0]["comment"] == "Hey @testuser look"
        assert "unread_count" in data
        test_client.cookies.clear()

    async def test_get_mentions_requires_username(self, test_client) -> None:
        """Request without cookie returns 401."""
        response = test_client.get("/api/users/mentions")
        assert response.status_code == 401

    async def test_get_mentions_unread_only_forwarded(
        self, test_client, temp_db_path: Path
    ) -> None:
        """unread_only query flag is forwarded to storage as True."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
        test_client.cookies.set("jji_username", "testuser")
        with patch.object(
            storage,
            "get_mentions_for_user",
            new_callable=AsyncMock,
            return_value={"mentions": [], "total": 0, "unread_count": 0},
        ) as mock_get:
            response = test_client.get("/api/users/mentions?unread_only=true")
            assert response.status_code == 200
            assert mock_get.call_args.kwargs["unread_only"] is True
        test_client.cookies.clear()


class TestMarkReadEndpoint:
    """Tests for POST /api/users/mentions/read."""

    async def test_mark_read_endpoint(self, test_client, temp_db_path: Path) -> None:
        """POST with valid comment_ids returns ok."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            cid = await _add_comment(temp_db_path, "Hey @testuser", username="bob")

        test_client.cookies.set("jji_username", "testuser")
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": [cid]},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        test_client.cookies.clear()

    async def test_mark_read_rejects_empty_list(self, test_client) -> None:
        """POST with empty comment_ids returns 400."""
        test_client.cookies.set("jji_username", "testuser")
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": []},
        )
        assert response.status_code == 400
        test_client.cookies.clear()

    async def test_mark_read_rejects_non_int(self, test_client) -> None:
        """POST with non-integer comment_ids returns 400."""
        test_client.cookies.set("jji_username", "testuser")
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": ["abc"]},
        )
        assert response.status_code == 400
        test_client.cookies.clear()

    async def test_mark_read_rejects_booleans(self, test_client, temp_db_path) -> None:
        """POST with boolean comment_ids returns 400."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
        test_client.cookies.set("jji_username", "testuser")
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": [True, 2]},
        )
        assert response.status_code == 400
        test_client.cookies.clear()

    async def test_mark_read_requires_username(self, test_client) -> None:
        """POST without auth returns 401."""
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": [1]},
        )
        assert response.status_code == 401

    async def test_mark_read_non_mentioned_comment_no_effect(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Marking a comment that doesn't mention the user has no visible effect."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            # Comment mentions @other, NOT @testuser
            cid = await _add_comment(temp_db_path, "Hey @other look", username="bob")

        test_client.cookies.set("jji_username", "testuser")
        # Marking succeeds (INSERT OR IGNORE) but creates a junk row
        response = test_client.post(
            "/api/users/mentions/read",
            json={"comment_ids": [cid]},
        )
        assert response.status_code == 200

        # But the comment never appears in testuser's mentions
        response = test_client.get("/api/users/mentions")
        assert response.status_code == 200
        assert response.json()["total"] == 0
        test_client.cookies.clear()


class TestMarkAllMentionsRead:
    """Tests for storage.mark_all_mentions_read."""

    async def test_mark_all_mentions_read(self, setup_test_db: Path) -> None:
        """All unread mentions are marked as read, returns count."""
        db = setup_test_db
        await _add_comment(db, "Hey @alice first", username="bob")
        await _add_comment(db, "Hey @alice second", username="charlie")
        await _add_comment(db, "No mention here", username="dave")

        with patch.object(storage, "DB_PATH", db):
            assert await storage.get_unread_mention_count("alice") == 2

            count = await storage.mark_all_mentions_read("alice")
            assert count == 2

            assert await storage.get_unread_mention_count("alice") == 0

    async def test_mark_all_read_idempotent(self, setup_test_db: Path) -> None:
        """Calling mark_all again returns 0 when already read."""
        db = setup_test_db
        await _add_comment(db, "Hey @alice", username="bob")

        with patch.object(storage, "DB_PATH", db):
            first = await storage.mark_all_mentions_read("alice")
            assert first == 1
            second = await storage.mark_all_mentions_read("alice")
            assert second == 0

    async def test_mark_all_read_no_mentions(self, setup_test_db: Path) -> None:
        """Returns 0 when user has no mentions."""
        db = setup_test_db
        with patch.object(storage, "DB_PATH", db):
            count = await storage.mark_all_mentions_read("nobody")
            assert count == 0

    async def test_mark_all_read_partial(self, setup_test_db: Path) -> None:
        """Only marks unread mentions; already-read ones are unaffected."""
        db = setup_test_db
        cid1 = await _add_comment(db, "Hey @alice one", username="bob")
        await _add_comment(db, "Hey @alice two", username="charlie")

        with patch.object(storage, "DB_PATH", db):
            await storage.mark_mentions_read("alice", [cid1])
            assert await storage.get_unread_mention_count("alice") == 1

            count = await storage.mark_all_mentions_read("alice")
            assert count == 1
            assert await storage.get_unread_mention_count("alice") == 0


class TestMarkAllReadEndpoint:
    """Tests for POST /api/users/mentions/read-all."""

    async def test_mark_all_read_endpoint(
        self, test_client, temp_db_path: Path
    ) -> None:
        """POST returns marked_read count."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _add_comment(temp_db_path, "Hey @testuser one", username="bob")
            await _add_comment(temp_db_path, "Hey @testuser two", username="charlie")

        test_client.cookies.set("jji_username", "testuser")
        response = test_client.post("/api/users/mentions/read-all")
        assert response.status_code == 200
        data = response.json()
        assert data["marked_read"] == 2
        test_client.cookies.clear()

    async def test_mark_all_read_requires_username(self, test_client) -> None:
        """POST without auth returns 401."""
        response = test_client.post("/api/users/mentions/read-all")
        assert response.status_code == 401


class TestGetMentionsEndpointValidation:
    """Tests for input validation on GET /api/users/mentions."""

    async def test_invalid_offset(self, test_client) -> None:
        """Non-numeric offset returns 400."""
        test_client.cookies.set("jji_username", "testuser")
        response = test_client.get("/api/users/mentions?offset=abc")
        assert response.status_code == 400
        test_client.cookies.clear()

    async def test_invalid_limit(self, test_client) -> None:
        """Non-numeric limit returns 400."""
        test_client.cookies.set("jji_username", "testuser")
        response = test_client.get("/api/users/mentions?limit=xyz")
        assert response.status_code == 400
        test_client.cookies.clear()

    async def test_negative_offset_clamped(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Negative offset is clamped to 0."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
        test_client.cookies.set("jji_username", "testuser")
        with patch.object(
            storage,
            "get_mentions_for_user",
            new_callable=AsyncMock,
            return_value={"mentions": [], "total": 0, "unread_count": 0},
        ) as mock_get:
            response = test_client.get("/api/users/mentions?offset=-5")
            assert response.status_code == 200
            mock_get.assert_called_once()
            assert mock_get.call_args.kwargs["offset"] == 0
        test_client.cookies.clear()

    async def test_limit_clamped_to_max(self, test_client, temp_db_path: Path) -> None:
        """Limit > 200 is clamped to 200."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
        test_client.cookies.set("jji_username", "testuser")
        with patch.object(
            storage,
            "get_mentions_for_user",
            new_callable=AsyncMock,
            return_value={"mentions": [], "total": 0, "unread_count": 0},
        ) as mock_get:
            response = test_client.get("/api/users/mentions?limit=9999")
            assert response.status_code == 200
            mock_get.assert_called_once()
            assert mock_get.call_args.kwargs["limit"] == 200
        test_client.cookies.clear()


class TestGetMentionsUnreadCount:
    """Tests that get_mentions_for_user returns unread_count directly."""

    async def test_unread_count_in_result(self, setup_test_db: Path) -> None:
        """get_mentions_for_user returns unread_count in result dict."""
        db = setup_test_db
        cid = await _add_comment(db, "Hey @alice first", username="bob")
        await _add_comment(db, "Hey @alice second", username="charlie")

        with patch.object(storage, "DB_PATH", db):
            result = await storage.get_mentions_for_user("alice")
            assert result["unread_count"] == 2

            await storage.mark_mentions_read("alice", [cid])
            result = await storage.get_mentions_for_user("alice")
            assert result["unread_count"] == 1


class TestUnreadCountEndpoint:
    """Tests for GET /api/users/mentions/unread-count."""

    async def test_unread_count_endpoint(self, test_client, temp_db_path: Path) -> None:
        """Returns unread count for authenticated user."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _add_comment(temp_db_path, "Hey @testuser", username="bob")

        test_client.cookies.set("jji_username", "testuser")
        response = test_client.get("/api/users/mentions/unread-count")
        assert response.status_code == 200
        assert response.json()["count"] == 1
        test_client.cookies.clear()

    async def test_unread_count_requires_username(self, test_client) -> None:
        """Request without auth returns 401."""
        response = test_client.get("/api/users/mentions/unread-count")
        assert response.status_code == 401


# ===========================================================================
# Cross-stack parity tests (Python ↔ Frontend)
# ===========================================================================


class TestMentionRegexParity:
    """Shared mention regex test cases — MUST match frontend CommentsSection.test.tsx.

    These test cases are shared with frontend/src/pages/report/__tests__/CommentsSection.test.tsx
    If you change these, update the frontend side too.
    """

    # Pattern: (?<![a-zA-Z0-9.])@([a-zA-Z0-9_-]+)
    PARITY_CASES = (
        ("hello @alice", ["alice"]),
        ("@bob test", ["bob"]),
        ("cc @alice @bob", ["alice", "bob"]),
        ("email user@domain.com", []),  # email — no match
        ("no mentions here", []),
        ("@alice-bob", ["alice-bob"]),  # hyphens allowed
        ("@alice_bob", ["alice_bob"]),  # underscores allowed
        ("@alice123", ["alice123"]),  # digits allowed
        (".@alice", []),  # preceded by dot — no match
        ("x@alice", []),  # preceded by letter — no match
        ("1@alice", []),  # preceded by digit — no match
        ("(@alice)", ["alice"]),  # parens ok
        ("@alice.", ["alice"]),  # trailing dot ok
        ("@alice ping @alice", ["alice"]),  # deduplication — same user only once
    )

    @pytest.mark.parametrize("text,expected", PARITY_CASES)
    def test_detect_mentions_parity(self, text, expected):
        """Verify Python detect_mentions matches shared test cases."""
        from jenkins_job_insight.comment_enrichment import detect_mentions

        result = detect_mentions(text)
        assert result == expected, f"Input: {text!r}, expected {expected}, got {result}"
