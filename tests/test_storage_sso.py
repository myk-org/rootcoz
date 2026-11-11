"""Tests for the SSO storage layer (identities, device codes, provenance).

Stage 2 of issue #197: the schema an SSO login needs. Nothing here decides
whether an account may act — approval lives in ``rootcoz.auth.approval`` — so
these tests pin storage behaviour only.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


async def _expire_device_code(device_code: str) -> None:
    """Move a device code into the past without waiting for its TTL."""
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime(
        storage.SQL_TIMESTAMP_FORMAT
    )
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE sso_device_codes SET expires_at = ? WHERE device_code_hash = ?",
            (past, storage._hash_token(device_code)),
        )
        await db.commit()


async def _raw_identity_rows() -> list[dict]:
    """Read ``user_identities`` directly, bypassing the users join."""
    async with storage._connect_db() as db:
        cursor = await db.execute("SELECT * FROM user_identities")
        return [dict(row) for row in await cursor.fetchall()]


async def test_created_via_is_recorded_once_and_defaults_to_internal(
    setup_test_db: Path,
) -> None:
    """Provenance is set at creation and never rewritten by a later call."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob", created_via="github")
        # The "existing account, no key" path (register / SSO attaching a key)
        # must leave the recorded provenance alone.
        async with storage._connect_db() as db:
            await db.execute("DELETE FROM users WHERE username = 'bob'")
            await db.execute(
                "INSERT INTO users (username, role, status, created_via)"
                " VALUES ('bob', 'reviewer', 'active', 'github')"
            )
            await db.commit()
        await storage.create_user("bob", created_via="google")

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT username, created_via FROM users ORDER BY username"
            )
            rows = [dict(row) for row in await cursor.fetchall()]

    assert rows == [
        {"username": "alice", "created_via": "internal"},
        {"username": "bob", "created_via": "github"},
    ]


async def test_an_unknown_created_via_is_refused(setup_test_db: Path) -> None:
    """A typo must not silently become a provenance value nothing understands."""
    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        pytest.raises(ValueError, match="Invalid created_via"),
    ):
        await storage.create_user("alice", created_via="ldap")


async def test_linking_an_identity_does_not_change_provenance(
    setup_test_db: Path,
) -> None:
    """``created_via`` describes creation; a link is not a creation."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice", created_via="github")
        await storage.add_user_identity("alice", "github", "42")

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT created_via FROM users WHERE username = 'alice'"
            )
            assert (await cursor.fetchone())["created_via"] == "github"


async def test_the_schema_migration_is_idempotent(setup_test_db: Path) -> None:
    """Re-opening an existing database must not fail or rewrite its rows."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice", created_via="github")
        await storage.add_user_identity("alice", "google", "sub-1")
        token = await storage.create_session("alice", auth_provider="google")

        await storage.init_db()

        assert (await storage.get_user_identity("google", "sub-1")) is not None
        session = await storage.get_session(token)
        assert session is not None
        assert session["auth_provider"] == "google"


async def test_identity_links_lookup_list_and_unlink(setup_test_db: Path) -> None:
    """The stable key is (provider, subject), not the username or email."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        created = await storage.add_user_identity(
            "alice", "google", "sub-1", email="alice@example.com"
        )

        assert created["username"] == "alice"
        assert created["provider"] == "google"
        assert created["subject"] == "sub-1"
        assert created["email"] == "alice@example.com"
        assert created["raw_claims_json"] is None

        assert await storage.get_user_identity("google", "sub-1") == created
        assert await storage.get_user_identity("google", "other") is None
        assert await storage.list_user_identities("alice") == [created]

        # The only identity is not removable: an SSO-created account never saw
        # its API key, so this would be a lockout.
        assert await storage.unlink_user_identity("alice", "google") == "last"
        assert len(await storage.list_user_identities("alice")) == 1

        await storage.add_user_identity("alice", "github", "gh-1")
        assert await storage.unlink_user_identity("alice", "google") == "deleted"
        assert [i["provider"] for i in await storage.list_user_identities("alice")] == [
            "github"
        ]
        assert await storage.unlink_user_identity("alice", "google") == "not_found"


async def test_create_with_identity_returns_the_api_key(setup_test_db: Path) -> None:
    """The key is part of creating the account: nothing else is ever shown."""
    raw_key = await storage.create_user_with_identity(
        "ssokey",
        provider="google",
        subject="sub-key",
        email="k@example.com",
        status="active",
        role="reviewer",
        created_via="google",
    )

    assert raw_key.startswith("rootcoz_")
    user = await storage.get_user_by_key(raw_key)
    assert user is not None and user["username"] == "ssokey"


async def test_create_with_identity_refuses_a_reserved_name(
    setup_test_db: Path,
) -> None:
    """The name rule is the store's, not whichever caller remembered it."""
    for name in ("rootcoz-ai", "admin"):
        with pytest.raises(ValueError):
            await storage.create_user_with_identity(
                name,
                provider="google",
                subject="sub-reserved",
                email=None,
                status="active",
                role="reviewer",
                created_via="google",
            )


async def test_a_user_code_is_matched_with_or_without_its_hyphen(
    setup_test_db: Path,
) -> None:
    """People read the code off a terminal: separators and case are not the code."""
    record = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
    stored = record["user_code"]
    compact = stored.replace("-", "")

    for typed in (stored, stored.lower(), compact, compact.lower(), f" {compact} "):
        found = await storage.get_device_code_by_user_code(typed)
        assert found is not None, typed
        assert found["user_code"] == stored


async def test_concurrent_unlinks_cannot_leave_an_account_without_a_method(
    setup_test_db: Path,
) -> None:
    """The last-method guard is part of the write, not a check before it.

    Two removals issued together used to each see two identities and both
    succeed, leaving an SSO-created account with no way in at all.
    """
    await storage.create_user("alice")
    await storage.add_user_identity("alice", "google", "sub-1")
    await storage.add_user_identity("alice", "github", "gh-1")

    results = await asyncio.gather(
        storage.unlink_user_identity("alice", "google"),
        storage.unlink_user_identity("alice", "github"),
    )

    assert sorted(results) == ["deleted", "last"]
    assert len(await storage.list_user_identities("alice")) == 1


async def test_identity_claims_are_scrubbed_and_readable(
    setup_test_db: Path,
) -> None:
    """Claims are audit data — readable, and never a place for a credential."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        claims = (
            '{"login": "alice", "access_token": "secret-1", "code": "oauth-code",'
            ' "nested": {"id_token": "secret-2", "email_verified": true}}'
        )
        created = await storage.add_user_identity(
            "alice", "github", "42", raw_claims_json=claims
        )

        stored = created["raw_claims_json"]
        assert stored is not None
        assert "secret-1" not in stored
        assert "secret-2" not in stored
        assert "oauth-code" not in stored
        assert '"login": "alice"' in stored
        assert '"email_verified": true' in stored
        assert (await storage.get_user_identity("github", "42"))[
            "raw_claims_json"
        ] == stored


async def test_an_empty_identity_subject_is_refused(setup_test_db: Path) -> None:
    """A subject that identifies nobody would consume the unique slot."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        with pytest.raises(ValueError, match="non-empty subject"):
            await storage.add_user_identity("alice", "google", "   ")


async def test_a_subject_already_linked_to_another_account_is_refused(
    setup_test_db: Path,
) -> None:
    """Two local accounts cannot claim the same external subject."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        await storage.add_user_identity("alice", "github", "12345")

        # The refusal must not name the account that holds the subject.
        with pytest.raises(ValueError, match=r"already linked$") as excinfo:
            await storage.add_user_identity("bob", "github", "12345")
        assert "alice" not in str(excinfo.value)

        assert [i["provider"] for i in await storage.list_user_identities("bob")] == []


async def test_one_identity_per_provider_per_account(setup_test_db: Path) -> None:
    """Linking a second Google account requires unlinking the first."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")

        with pytest.raises(ValueError, match="already has a google identity"):
            await storage.add_user_identity("alice", "google", "sub-2")


async def test_the_database_enforces_both_uniqueness_rules(
    setup_test_db: Path,
) -> None:
    """The constraints are real, not only a pre-check that could race."""
    import sqlite3

    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        await storage.add_user_identity("alice", "google", "sub-1")
        alice = await storage.get_user_by_username("alice")
        assert alice is not None

        async with storage._connect_db() as db:
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, subject, created_at)"
                    " VALUES (?, 'google', 'sub-1', 'now')",
                    (alice["id"],),
                )
            await db.rollback()

        bob = await storage.get_user_by_username("bob")
        assert bob is not None
        async with storage._connect_db() as db:
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, subject, created_at)"
                    " VALUES (?, 'google', 'sub-2', 'now')",
                    (alice["id"],),
                )
            await db.rollback()


async def test_identity_requires_a_known_provider_and_account(
    setup_test_db: Path,
) -> None:
    """Provider names are a closed set and the account must exist."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")

        with pytest.raises(ValueError, match="Invalid SSO provider"):
            await storage.get_user_identity("ldap", "sub-1")
        with pytest.raises(ValueError, match="Invalid SSO provider"):
            await storage.add_user_identity("alice", "ldap", "sub-1")
        with pytest.raises(ValueError, match="not found"):
            await storage.add_user_identity("nobody", "google", "sub-1")


async def test_deleting_an_account_removes_its_identities(
    setup_test_db: Path,
) -> None:
    """No orphan identity: it would hold its unique slot and block a re-link."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")

        await storage.delete_user("alice")

        assert await storage.get_user_identity("google", "sub-1") is None
        # The join already hides an orphan, so check the table itself.
        assert await _raw_identity_rows() == []

        # The consequence the orphan would have caused: the subject is free again.
        await storage.create_user("bob")
        assert (await storage.add_user_identity("bob", "google", "sub-1"))[
            "username"
        ] == "bob"


async def test_an_orphan_identity_can_be_healed(setup_test_db: Path) -> None:
    """A row whose account is gone holds the unique slot; deleting it frees it."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")
        # Simulate a row that outlived its account (a crashed delete, or history).
        async with storage._connect_db() as db:
            await db.execute("DELETE FROM users WHERE username = 'alice'")
            await db.commit()

        assert await storage.delete_orphan_identity("google", "sub-1") is True
        # A live identity is never touched by the healing path.
        await storage.create_user("bob")
        await storage.add_user_identity("bob", "google", "sub-2")
        assert await storage.delete_orphan_identity("google", "sub-2") is False
        assert await storage.get_user_identity("google", "sub-2") is not None


async def test_a_session_records_the_provider_that_issued_it(
    setup_test_db: Path,
) -> None:
    """API-key sessions carry no provider; SSO sessions name theirs."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        key_session = await storage.create_session("alice")
        sso_session = await storage.create_session("alice", auth_provider="google")

        key_row = await storage.get_session(key_session)
        sso_row = await storage.get_session(sso_session)

    assert key_row is not None
    assert sso_row is not None
    assert key_row["auth_provider"] is None
    assert sso_row["auth_provider"] == "google"


async def test_admin_created_accounts_record_admin_provenance(
    setup_test_db: Path,
) -> None:
    """The one path that is unambiguously ``admin`` must say so."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_admin_user("boss", created_via="admin")
        # The default is the admin value, so a caller cannot forget it.
        await storage.create_admin_user("boss2")

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT username, created_via FROM users ORDER BY username"
            )
            rows = {
                row["username"]: row["created_via"] for row in await cursor.fetchall()
            }

    assert rows == {"boss": "admin", "boss2": "admin"}


async def test_approval_can_record_the_provider_it_was_made_with(
    setup_test_db: Path,
) -> None:
    """A flow that fixed no provider learns it at approval; a conflict is refused."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        open_code = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=5
        )
        assert await storage.authorize_device_code(
            open_code["user_code"], username="alice", provider="google"
        )
        assert await storage.redeem_device_code(open_code["device_code"]) == {
            "username": "alice",
            "provider": "google",
        }

        fixed = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=5, provider="github"
        )
        # Approving a github flow from a google session must not relabel it.
        assert not await storage.authorize_device_code(
            fixed["user_code"], username="alice", provider="google"
        )
        assert await storage.authorize_device_code(fixed["user_code"], username="alice")
        assert await storage.redeem_device_code(fixed["device_code"]) == {
            "username": "alice",
            "provider": "github",
        }


async def test_a_first_login_ticket_is_single_use(setup_test_db: Path) -> None:
    """A leaked choice URL must not be replayable."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="sub-1",
            email="alice@example.com",
            suggested_username="alice",
        )

        peeked = await storage.peek_sso_ticket(ticket)
        assert peeked is not None
        assert peeked["suggested_username"] == "alice"
        # Peeking never burns it, so a page reload still works.
        assert await storage.peek_sso_ticket(ticket) is not None

        consumed = await storage.consume_sso_ticket(ticket)
        assert consumed is not None
        assert consumed["subject"] == "sub-1"
        assert await storage.consume_sso_ticket(ticket) is None
        assert await storage.peek_sso_ticket(ticket) is None


async def test_concurrent_ticket_consumption_has_exactly_one_winner(
    setup_test_db: Path,
) -> None:
    """Eight clients, one ticket: one redemption, seven refusals.

    ``consume_sso_ticket`` holds BEGIN IMMEDIATE across the read and the delete,
    which is what makes the handover exactly-once under concurrency.
    """
    import asyncio

    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google", subject="s", email=None, suggested_username="alice"
        )

        results = await asyncio.gather(
            *(storage.consume_sso_ticket(ticket) for _ in range(8))
        )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1


async def test_only_the_hashed_ticket_is_stored(setup_test_db: Path) -> None:
    """The URL value is a credential; the table must not hold it."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google", subject="s", email=None, suggested_username="alice"
        )
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT * FROM sso_link_tickets")
            row = dict(await cursor.fetchone())

    assert ticket not in " ".join(str(value) for value in row.values())
    assert row["ticket_hash"] == storage._hash_token(ticket)


async def test_an_expired_ticket_cannot_be_used(setup_test_db: Path) -> None:
    """Both reads check the clock; the purge is housekeeping, not the gate."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google", subject="s", email=None, suggested_username="alice"
        )
        past = (datetime.now(UTC) - timedelta(minutes=1)).strftime(
            storage.SQL_TIMESTAMP_FORMAT
        )
        async with storage._connect_db() as db:
            await db.execute("UPDATE sso_link_tickets SET expires_at = ?", (past,))
            await db.commit()

        assert await storage.peek_sso_ticket(ticket) is None
        assert await storage.consume_sso_ticket(ticket) is None
        assert await storage.purge_expired_sso_tickets() == 1


async def test_a_device_code_is_approved_then_redeemed_exactly_once(
    setup_test_db: Path,
) -> None:
    """Approval names an account; redemption hands that name over once."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)

        pending = await storage.get_device_code(code["device_code"])
        assert pending is not None
        assert pending["authorized"] is False
        assert pending["user_code"] == code["user_code"]

        assert (
            await storage.authorize_device_code(code["user_code"], username="alice")
            is True
        )

        # The approval page reads by the code a human typed, in any case.
        approval_view = await storage.get_device_code_by_user_code(
            f"  {code['user_code'].lower()} "
        )
        assert approval_view is not None
        assert approval_view["authorized"] is True

        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "provider": None,
        }
        # A second redemption is refused — the code is claimed, exactly once — but
        # the row survives so a failed session can be retried without the terminal
        # being told its code expired.
        assert await storage.redeem_device_code(code["device_code"]) is None
        assert await storage.get_device_code(code["device_code"]) is not None

        assert await storage.release_device_code(code["device_code"]) is True
        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "provider": None,
        }

        # Consumption is the step that ends the flow.
        assert await storage.consume_device_code(code["device_code"]) is True
        assert await storage.get_device_code(code["device_code"]) is None
        assert await storage.get_device_code_by_user_code(code["user_code"]) is None


async def test_only_the_hashed_device_code_is_stored(setup_test_db: Path) -> None:
    """The table holds a hash and an account — never a usable credential."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=5, client_name="rootcoz-cli on cnv-host"
        )
        await storage.authorize_device_code(code["user_code"], username="alice")

        # The approval page must be able to show a human what they authorize.
        approval_view = await storage.get_device_code_by_user_code(code["user_code"])
        assert approval_view is not None
        assert approval_view["client_name"] == "rootcoz-cli on cnv-host"

        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT * FROM sso_device_codes")
            row = dict(await cursor.fetchone())

    stored = " ".join(str(value) for value in row.values())
    assert code["device_code"] not in stored
    assert row["device_code_hash"] == storage._hash_token(code["device_code"])
    assert "session_token_hash" not in row


async def test_an_authorized_code_cannot_be_approved_for_someone_else(
    setup_test_db: Path,
) -> None:
    """A second approval must not swap the account behind a pending code."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)

        assert await storage.authorize_device_code(code["user_code"], username="alice")
        assert not await storage.authorize_device_code(
            code["user_code"], username="bob"
        )
        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "provider": None,
        }


async def test_expired_device_codes_cannot_be_approved_or_redeemed(
    setup_test_db: Path,
) -> None:
    """Both paths check the clock, not just the purge."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)

        await _expire_device_code(code["device_code"])

        assert await storage.get_device_code(code["device_code"]) is None
        assert await storage.get_device_code_by_user_code(code["user_code"]) is None
        assert not await storage.authorize_device_code(
            code["user_code"], username="alice"
        )
        assert await storage.redeem_device_code(code["device_code"]) is None


async def test_concurrent_redemption_has_exactly_one_winner(
    setup_test_db: Path,
) -> None:
    """Eight pollers, one code: one CLI gets the account, seven get nothing.

    ``redeem_device_code`` holds BEGIN IMMEDIATE across the read and the delete,
    which is what makes the handover exactly-once under concurrency.
    """
    import asyncio

    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="alice")

        results = await asyncio.gather(
            *(storage.redeem_device_code(code["device_code"]) for _ in range(8))
        )

    winners = [result for result in results if result is not None]
    assert winners == [{"username": "alice", "provider": None}]


async def test_purge_removes_only_expired_device_codes(setup_test_db: Path) -> None:
    """Housekeeping never eats a code a human is still about to approve."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        live = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        stale = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await _expire_device_code(stale["device_code"])

        assert await storage.purge_expired_device_codes() == 1
        assert await storage.get_device_code(live["device_code"]) is not None
        assert await storage.get_device_code(stale["device_code"]) is None
