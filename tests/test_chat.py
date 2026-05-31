"""Tests for the chat feature."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz import storage
from rootcoz.engine.chat import build_chat_prompt, build_system_prompt

_TEST_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_TEST_ENCRYPTION_KEY = "test-encryption-key-for-hmac"  # pragma: allowlist secret
_ADMIN_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Mock settings for endpoint tests."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
        "ADMIN_KEY": _TEST_ADMIN_KEY,  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY,  # pragma: allowlist secret
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(mock_settings, temp_db_path: Path):
    """Create a test client with admin auth."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from rootcoz.main import app

        with TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client:
            yield client


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


# ---------------------------------------------------------------------------
# build_system_prompt tests
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    """Tests for build_system_prompt."""

    def test_basic_prompt_structure(self):
        prompt = build_system_prompt(
            job_name="test-job",
            build_number=42,
            job_id="job-123",
            available_scripts=["rootcoz-chat-job"],
        )
        assert "test-job" in prompt
        assert "#42" in prompt
        assert "job-123" in prompt
        assert "rootcoz-chat-job" in prompt
        assert "read-only" in prompt.lower()
        assert "./bin/rootcoz-chat-job" in prompt

    def test_all_scripts_listed(self):
        prompt = build_system_prompt(
            job_name="test-job",
            build_number=1,
            job_id="j1",
            available_scripts=[
                "rootcoz-chat-job",
                "rootcoz-chat-jira",
                "rootcoz-chat-github",
            ],
        )
        assert "./bin/rootcoz-chat-job" in prompt
        assert "./bin/rootcoz-chat-jira" in prompt
        assert "./bin/rootcoz-chat-github" in prompt
        assert "Jira" in prompt
        assert "GitHub" in prompt

    def test_no_scripts(self):
        prompt = build_system_prompt(
            job_name="clean",
            build_number=1,
            job_id="job-789",
            available_scripts=[],
        )
        assert "clean" in prompt
        assert "#1" in prompt
        # No script lines but prompt still valid
        assert "Available Tools" in prompt

    def test_repos_available_note(self):
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job"],
            repos_available=True,
        )
        assert "Source repositories are cloned" in prompt

    def test_repos_not_available(self):
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job"],
            repos_available=False,
        )
        assert "Source repositories are cloned" not in prompt

    def test_only_job_script(self):
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job"],
        )
        assert "./bin/rootcoz-chat-job" in prompt
        assert "./bin/rootcoz-chat-jira" not in prompt
        assert "./bin/rootcoz-chat-github" not in prompt

    def test_unavailable_jira_notice(self):
        """When jira is not configured, system prompt includes unavailable notice."""
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job", "rootcoz-chat-github"],
        )
        assert "Unavailable Tools" in prompt
        assert "Jira search" in prompt
        assert "User Settings" in prompt
        assert "GitHub search" not in prompt  # GitHub IS configured

    def test_unavailable_github_notice(self):
        """When github is not configured, system prompt includes unavailable notice."""
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job", "rootcoz-chat-jira"],
        )
        assert "Unavailable Tools" in prompt
        assert "GitHub search" in prompt
        assert "User Settings" in prompt
        assert "Jira search" not in prompt  # Jira IS configured

    def test_both_unavailable_notices(self):
        """When neither jira nor github configured, both notices appear."""
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=["rootcoz-chat-job"],
        )
        assert "Unavailable Tools" in prompt
        assert "Jira search" in prompt
        assert "GitHub search" in prompt

    def test_no_unavailable_when_all_configured(self):
        """When both are configured, no unavailable section."""
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            available_scripts=[
                "rootcoz-chat-job",
                "rootcoz-chat-jira",
                "rootcoz-chat-github",
            ],
        )
        assert "Unavailable Tools" not in prompt


# ---------------------------------------------------------------------------
# build_chat_prompt tests
# ---------------------------------------------------------------------------


class TestBuildChatPrompt:
    """Tests for build_chat_prompt."""

    def test_empty_history(self):
        prompt = build_chat_prompt("SYSTEM", [], "hello")
        assert "SYSTEM" in prompt
        assert "**User:** hello" in prompt
        assert "**Assistant:**" in prompt

    def test_with_history(self):
        history = [
            {"role": "user", "content": "what failed?"},
            {"role": "assistant", "content": "test_foo failed"},
        ]
        prompt = build_chat_prompt("SYS", history, "why?")
        assert "**User:** what failed?" in prompt
        assert "**Assistant:** test_foo failed" in prompt
        assert "**User:** why?" in prompt

    def test_history_order_preserved(self):
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
        prompt = build_chat_prompt("SYS", history, "fifth")
        assert prompt.index("first") < prompt.index("second")
        assert prompt.index("second") < prompt.index("third")
        assert prompt.index("third") < prompt.index("fourth")
        assert prompt.index("fourth") < prompt.index("fifth")


# ---------------------------------------------------------------------------
# Chat storage tests
# ---------------------------------------------------------------------------


class TestChatStorage:
    """Tests for chat message storage functions."""

    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, setup_test_db):
        msg_id = await storage.add_chat_message(
            job_id="test-job-chat",
            role="user",
            content="hello",
            username="testuser",
        )
        assert msg_id > 0

        messages = await storage.get_chat_messages("test-job-chat", username="testuser")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[0]["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_count_messages(self, setup_test_db):
        await storage.add_chat_message("count-job", "user", "msg1", username="u1")
        await storage.add_chat_message("count-job", "assistant", "msg2", username="u1")
        count = await storage.count_chat_messages("count-job", username="u1")
        assert count == 2

    @pytest.mark.asyncio
    async def test_delete_messages(self, setup_test_db):
        await storage.add_chat_message("del-job", "user", "msg", username="u1")
        deleted = await storage.delete_chat_messages("del-job", username="u1")
        assert deleted == 1
        messages = await storage.get_chat_messages("del-job", username="u1")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, setup_test_db):
        for i in range(5):
            await storage.add_chat_message(
                "page-job", "user", f"msg-{i}", username="u1"
            )

        messages = await storage.get_chat_messages(
            "page-job", limit=2, offset=0, username="u1"
        )
        assert len(messages) == 2
        assert messages[0]["content"] == "msg-0"
        assert messages[1]["content"] == "msg-1"

        messages = await storage.get_chat_messages(
            "page-job", limit=2, offset=3, username="u1"
        )
        assert len(messages) == 2
        assert messages[0]["content"] == "msg-3"
        assert messages[1]["content"] == "msg-4"

    @pytest.mark.asyncio
    async def test_get_messages_empty(self, setup_test_db):
        messages = await storage.get_chat_messages("nonexistent-job")
        assert messages == []

    @pytest.mark.asyncio
    async def test_count_messages_empty(self, setup_test_db):
        count = await storage.count_chat_messages("nonexistent-job")
        assert count == 0

    @pytest.mark.asyncio
    async def test_add_message_with_ai_fields(self, setup_test_db):
        msg_id = await storage.add_chat_message(
            job_id="ai-job",
            role="assistant",
            content="AI response",
            username="u1",
            ai_provider="claude",
            ai_model="sonnet-4",
        )
        assert msg_id > 0

        messages = await storage.get_chat_messages("ai-job", username="u1")
        assert len(messages) == 1
        assert messages[0]["ai_provider"] == "claude"
        assert messages[0]["ai_model"] == "sonnet-4"

    @pytest.mark.asyncio
    async def test_delete_returns_zero_when_empty(self, setup_test_db):
        deleted = await storage.delete_chat_messages("nothing-here")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_messages_scoped_by_username(self, setup_test_db):
        """Messages are isolated per user when username is provided."""
        await storage.add_chat_message(
            "scope-job", "user", "from alice", username="alice"
        )
        await storage.add_chat_message("scope-job", "user", "from bob", username="bob")

        alice_msgs = await storage.get_chat_messages("scope-job", username="alice")
        assert len(alice_msgs) == 1
        assert alice_msgs[0]["content"] == "from alice"

        bob_msgs = await storage.get_chat_messages("scope-job", username="bob")
        assert len(bob_msgs) == 1
        assert bob_msgs[0]["content"] == "from bob"

        # Without username filter, all messages are returned
        all_msgs = await storage.get_chat_messages("scope-job")
        assert len(all_msgs) == 2

    @pytest.mark.asyncio
    async def test_delete_scoped_by_username(self, setup_test_db):
        """Deleting with username only removes that user's messages."""
        await storage.add_chat_message(
            "del-scope-job", "user", "alice msg", username="alice"
        )
        await storage.add_chat_message(
            "del-scope-job", "user", "bob msg", username="bob"
        )

        deleted = await storage.delete_chat_messages("del-scope-job", username="alice")
        assert deleted == 1

        remaining = await storage.get_chat_messages("del-scope-job")
        assert len(remaining) == 1
        assert remaining[0]["username"] == "bob"

    @pytest.mark.asyncio
    async def test_count_scoped_by_username(self, setup_test_db):
        """Count with username only counts that user's messages."""
        await storage.add_chat_message("cnt-scope-job", "user", "a", username="alice")
        await storage.add_chat_message("cnt-scope-job", "user", "b", username="alice")
        await storage.add_chat_message("cnt-scope-job", "user", "c", username="bob")

        assert await storage.count_chat_messages("cnt-scope-job", username="alice") == 2
        assert await storage.count_chat_messages("cnt-scope-job", username="bob") == 1
        assert await storage.count_chat_messages("cnt-scope-job") == 3


# ---------------------------------------------------------------------------
# Chat API endpoint tests
# ---------------------------------------------------------------------------


async def _save_job(temp_db_path: Path, job_id: str, result: dict | None = None):
    """Save a result via the storage layer with DB_PATH patched."""
    default_result = {"status": "completed", "summary": "test", "failures": []}
    if result:
        default_result.update(result)

    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.save_result(job_id, "", "completed", default_result)


class TestChatEndpoints:
    """Tests for chat API endpoints."""

    def test_get_chat_history_404(self, test_client):
        response = test_client.get("/api/chat/nonexistent-job")
        assert response.status_code == 404

    async def test_get_chat_history_empty(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-test-job")
        response = test_client.get("/api/chat/chat-test-job")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []
        assert data["total"] == 0

    async def test_send_chat_message(self, test_client, temp_db_path: Path):
        await _save_job(
            temp_db_path,
            "chat-send-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "AI response here", None)
            response = test_client.post(
                "/api/chat/chat-send-job",
                json={"message": "what failed?", "ai_provider": "claude"},
            )
        assert response.status_code == 202
        data = response.json()
        assert data["user_message"]["content"] == "what failed?"
        assert data["user_message"]["status"] == "completed"
        assert "assistant_message" not in data
        # Background task runs: verify the assistant message was completed
        history = test_client.get("/api/chat/chat-send-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[0]["content"] == "AI response here"
        assert assistant_msgs[0]["status"] == "completed"

    async def test_send_chat_message_saves_history(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(
            temp_db_path,
            "chat-hist-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "first response", None)
            test_client.post(
                "/api/chat/chat-hist-job",
                json={"message": "hello", "ai_provider": "claude"},
            )
        # Verify messages by fetching chat history
        response = test_client.get("/api/chat/chat-hist-job")
        data = response.json()
        assert data["total"] == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"
        assert data["messages"][1]["role"] == "assistant"
        assert data["messages"][1]["content"] == "first response"

    async def test_delete_chat_history(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-del-job")
        # Add a message via POST
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "resp", None)
            test_client.post(
                "/api/chat/chat-del-job",
                json={"message": "hello", "ai_provider": "claude"},
            )
        response = test_client.delete("/api/chat/chat-del-job")
        assert response.status_code == 200
        assert response.json()["deleted"] == 2  # user + assistant

    def test_delete_chat_history_404(self, test_client):
        response = test_client.delete("/api/chat/nonexistent-job")
        assert response.status_code == 404

    async def test_send_message_no_provider_queues_and_fails(
        self, test_client, temp_db_path: Path
    ):
        """Without a provider, message is queued (202) but background processing fails."""
        await _save_job(temp_db_path, "chat-noprov-job")
        response = test_client.post(
            "/api/chat/chat-noprov-job",
            json={"message": "hello"},
        )
        assert response.status_code == 202
        # Background task fails — assistant message should be marked failed
        history = test_client.get("/api/chat/chat-noprov-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[0]["status"] == "failed"

    def test_send_message_404_for_missing_job(self, test_client):
        response = test_client.post(
            "/api/chat/nonexistent-job",
            json={"message": "hello", "ai_provider": "claude"},
        )
        assert response.status_code == 404

    async def test_send_message_ai_failure_marks_failed(
        self, test_client, temp_db_path: Path
    ):
        """AI failure is handled in background — message is queued (202), then marked failed."""
        await _save_job(
            temp_db_path,
            "chat-fail-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (False, "AI CLI timed out", None)
            response = test_client.post(
                "/api/chat/chat-fail-job",
                json={"message": "hello", "ai_provider": "claude"},
            )
        assert response.status_code == 202
        # Background task processes failure — check history
        history = test_client.get("/api/chat/chat-fail-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[0]["status"] == "failed"
        assert "AI CLI timed out" in assistant_msgs[0]["content"]

    async def test_get_chat_with_pagination(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-page-job")
        # Add messages via multiple POST calls
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "resp", None)
            for i in range(3):
                test_client.post(
                    "/api/chat/chat-page-job",
                    json={"message": f"msg-{i}", "ai_provider": "claude"},
                )
        # 3 user + 3 assistant = 6 total
        response = test_client.get("/api/chat/chat-page-job?limit=2&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2
        assert data["total"] == 6
