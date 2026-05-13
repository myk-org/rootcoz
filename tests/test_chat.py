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
        result_data = {
            "job_name": "test-job",
            "build_number": 42,
            "summary": "2 failures analyzed",
            "ai_provider": "claude",
            "ai_model": "sonnet-4",
            "jenkins_url": "http://jenkins/job/test/42",
            "failures": [
                {
                    "id": "uuid-1",
                    "test_name": "test_foo",
                    "analysis": {"classification": "PRODUCT BUG"},
                },
                {
                    "id": "uuid-2",
                    "test_name": "test_bar",
                    "analysis": {"classification": "INFRASTRUCTURE"},
                },
            ],
        }
        prompt = build_system_prompt(result_data, "job-123", "http://localhost:8000")
        assert "test-job" in prompt
        assert "#42" in prompt
        assert "uuid-1" in prompt
        assert "uuid-2" in prompt
        assert "test_foo" in prompt
        assert "test_bar" in prompt
        assert "PRODUCT BUG" in prompt
        assert "INFRASTRUCTURE" in prompt
        assert "read-only" in prompt.lower()
        assert "http://localhost:8000" in prompt

    def test_child_job_failures_included(self):
        result_data = {
            "job_name": "pipeline",
            "build_number": 1,
            "summary": "pipeline failed",
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "child-a",
                    "build_number": 10,
                    "failures": [
                        {
                            "id": "child-uuid-1",
                            "test_name": "test_child",
                            "analysis": {"classification": "CODE ISSUE"},
                        }
                    ],
                    "failed_children": [],
                }
            ],
        }
        prompt = build_system_prompt(result_data, "job-456", "")
        assert "child-a#10" in prompt
        assert "child-uuid-1" in prompt
        assert "test_child" in prompt

    def test_no_failures(self):
        result_data = {
            "job_name": "clean",
            "build_number": 1,
            "summary": "ok",
            "failures": [],
        }
        prompt = build_system_prompt(result_data, "job-789", "")
        assert "(no failures)" in prompt

    def test_no_server_url_omits_api_section(self):
        result_data = {
            "job_name": "j",
            "build_number": 1,
            "summary": "s",
            "failures": [],
        }
        prompt = build_system_prompt(result_data, "j1", "")
        assert "curl" not in prompt

    def test_server_url_includes_api_endpoints(self):
        result_data = {
            "job_name": "j",
            "build_number": 1,
            "summary": "s",
            "failures": [],
        }
        prompt = build_system_prompt(result_data, "j1", "http://srv:8000")
        assert "curl" in prompt
        assert "/api/results/j1" in prompt
        assert "/api/failures/" in prompt

    def test_nested_child_failures(self):
        result_data = {
            "job_name": "top",
            "build_number": 1,
            "summary": "nested",
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "child",
                    "build_number": 5,
                    "failures": [],
                    "failed_children": [
                        {
                            "job_name": "grandchild",
                            "build_number": 3,
                            "failures": [
                                {
                                    "id": "nested-uuid",
                                    "test_name": "test_nested",
                                    "analysis": {"classification": "ENVIRONMENT"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        prompt = build_system_prompt(result_data, "top-1", "")
        assert "grandchild#3" in prompt
        assert "nested-uuid" in prompt
        assert "test_nested" in prompt

    def test_missing_optional_fields(self):
        """Prompt builds safely when optional fields are absent."""
        result_data = {"failures": []}
        prompt = build_system_prompt(result_data, "x", "")
        assert "unknown" in prompt
        assert "#0" in prompt


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

        messages = await storage.get_chat_messages("test-job-chat")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[0]["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_count_messages(self, setup_test_db):
        await storage.add_chat_message("count-job", "user", "msg1")
        await storage.add_chat_message("count-job", "assistant", "msg2")
        count = await storage.count_chat_messages("count-job")
        assert count == 2

    @pytest.mark.asyncio
    async def test_delete_messages(self, setup_test_db):
        await storage.add_chat_message("del-job", "user", "msg")
        deleted = await storage.delete_chat_messages("del-job")
        assert deleted == 1
        messages = await storage.get_chat_messages("del-job")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, setup_test_db):
        for i in range(5):
            await storage.add_chat_message("page-job", "user", f"msg-{i}")

        messages = await storage.get_chat_messages("page-job", limit=2, offset=0)
        assert len(messages) == 2
        assert messages[0]["content"] == "msg-0"
        assert messages[1]["content"] == "msg-1"

        messages = await storage.get_chat_messages("page-job", limit=2, offset=3)
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
            ai_provider="claude",
            ai_model="sonnet-4",
        )
        assert msg_id > 0

        messages = await storage.get_chat_messages("ai-job")
        assert len(messages) == 1
        assert messages[0]["ai_provider"] == "claude"
        assert messages[0]["ai_model"] == "sonnet-4"

    @pytest.mark.asyncio
    async def test_delete_returns_zero_when_empty(self, setup_test_db):
        deleted = await storage.delete_chat_messages("nothing-here")
        assert deleted == 0


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
            mock_chat.return_value = (True, "AI response here")
            response = test_client.post(
                "/api/chat/chat-send-job",
                json={"message": "what failed?", "ai_provider": "claude"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["user_message"]["content"] == "what failed?"
        assert data["assistant_message"]["content"] == "AI response here"

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
            mock_chat.return_value = (True, "first response")
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
            mock_chat.return_value = (True, "resp")
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

    async def test_send_message_no_provider_returns_400(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-noprov-job")
        response = test_client.post(
            "/api/chat/chat-noprov-job",
            json={"message": "hello"},
        )
        assert response.status_code == 400

    def test_send_message_404_for_missing_job(self, test_client):
        response = test_client.post(
            "/api/chat/nonexistent-job",
            json={"message": "hello", "ai_provider": "claude"},
        )
        assert response.status_code == 404

    async def test_send_message_ai_failure_returns_502(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(
            temp_db_path,
            "chat-fail-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (False, "AI CLI timed out")
            response = test_client.post(
                "/api/chat/chat-fail-job",
                json={"message": "hello", "ai_provider": "claude"},
            )
        assert response.status_code == 502

    async def test_get_chat_with_pagination(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-page-job")
        # Add messages via multiple POST calls
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "resp")
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
