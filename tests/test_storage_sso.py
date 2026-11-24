"""Tests for the SSO storage layer (identities, device codes, provenance).

Stage 2 of issue #197: the schema an SSO login needs. Nothing here decides
whether an account may act — approval lives in ``rootcoz.auth.approval`` — so
these tests pin storage behaviour only.
"""

import asyncio
import contextlib
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
            (past, storage._hash_session_token(device_code)),
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
        # A later call naming the same account is refused, and the recorded
        # provenance keeps what creation wrote.
        with pytest.raises(ValueError, match="already exists"):
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


async def test_the_approver_column_migration_backfills_existing_codes(
    setup_test_db: Path,
) -> None:
    """A code approved before ``approved_username`` existed stays redeemable.

    Dropping the column reproduces the old shape on a real database: the row is
    approved (``user_id`` set) and the account is named nowhere else, so without
    the backfill it would read as unapproved and the terminal would wait forever.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        async with storage._connect_db() as db:
            await db.execute(
                "ALTER TABLE sso_device_codes DROP COLUMN approved_username"
            )
            await db.execute(
                "UPDATE sso_device_codes SET user_id ="
                " (SELECT id FROM users WHERE username = 'alice')"
                " WHERE user_code = ?",
                (code["user_code"],),
            )
            await db.commit()

        await storage.init_db()

        approved = await storage.get_device_code_by_user_code(code["user_code"])
        assert approved is not None and approved["authorized"] is True
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT approved_username FROM sso_device_codes WHERE user_code = ?",
                (code["user_code"],),
            )
            assert (await cursor.fetchone())["approved_username"] == "alice"
        # The backfilled name is what redemption names, so the flow completes.
        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "approved_provider": None,
        }


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

        # A keyed user can unlink its sole identity: local login remains.
        assert await storage.user_has_api_key("alice") is True
        assert await storage.unlink_user_identity("alice", "google") == "deleted"
        assert len(await storage.list_user_identities("alice")) == 0

        # Re-link two providers, then unlink one at a time.
        await storage.add_user_identity("alice", "google", "sub-2")
        await storage.add_user_identity("alice", "github", "gh-1")
        assert await storage.unlink_user_identity("alice", "google") == "deleted"
        assert [i["provider"] for i in await storage.list_user_identities("alice")] == [
            "github"
        ]
        assert await storage.unlink_user_identity("alice", "google") == "not_found"


async def test_keyless_user_cannot_unlink_last_identity(setup_test_db: Path) -> None:
    """A keyless account's sole SSO identity is its only sign-in method."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user("keyless-alice", created_via="proxy")
        await storage.add_user_identity("keyless-alice", "google", "sub-k1")

        assert await storage.user_has_api_key("keyless-alice") is False
        assert await storage.unlink_user_identity("keyless-alice", "google") == "last"
        assert len(await storage.list_user_identities("keyless-alice")) == 1

        # With two identities, unlinking one is allowed even without a key.
        await storage.add_user_identity("keyless-alice", "github", "gh-k1")
        assert (
            await storage.unlink_user_identity("keyless-alice", "google") == "deleted"
        )
        assert [
            i["provider"] for i in await storage.list_user_identities("keyless-alice")
        ] == ["github"]
        # Now the remaining one is the last — still refused.
        assert await storage.unlink_user_identity("keyless-alice", "github") == "last"


async def test_a_link_reads_its_row_inside_the_link_transaction(
    setup_test_db: Path,
) -> None:
    """A concurrent unlink must not turn the return into an IndexError.

    Reading the row again on a second connection after the commit could see the
    identity already gone and index an empty list. The patched helper stands in
    for that vanishing row: it reports nothing outside a write transaction, so
    the return has to come from the transaction that made the row.
    """
    real_select = storage._select_identities

    async def _vanishes_outside_a_transaction(db, where, params, *, order=""):
        if not db.in_transaction:
            return []
        return await real_select(db, where, params, order=order)

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch.object(storage, "_select_identities", _vanishes_outside_a_transaction),
    ):
        await storage.create_user("alice")
        created = await storage.add_user_identity(
            "alice", "google", "sub-1", email="alice@example.com"
        )
        # A repeat link returns the row it already holds, the same way.
        again = await storage.add_user_identity("alice", "google", "sub-1")

    assert created["subject"] == "sub-1"
    assert created["email"] == "alice@example.com"
    assert again == created


@pytest.mark.parametrize("block", ["pending", "disabled"])
async def test_a_blocked_account_cannot_be_handed_a_link(
    setup_test_db: Path, block: str
) -> None:
    """A block committed after the route's check still refuses the link write.

    The route applies the same gate before it gets here; this pins the write
    itself, which is what a caller added later would otherwise trust. Nothing is
    stored either way.
    """
    from rootcoz.config import Settings

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch("rootcoz.config._cached_settings", Settings(require_approval=True)),
    ):
        await storage.create_user("alice")
        if block == "disabled":
            assert await storage.set_user_disabled("alice", True) is not None
        else:
            async with storage._connect_db() as db:
                await db.execute(
                    "UPDATE users SET status = ? WHERE username = 'alice'", (block,)
                )
                await db.commit()

        with pytest.raises(ValueError, match=f"^account_{block}$"):
            await storage.add_user_identity("alice", "google", "sub-1")

        assert await _raw_identity_rows() == []


async def test_an_admin_is_not_held_by_its_own_approval_status_when_linking(
    setup_test_db: Path,
) -> None:
    """An administrator is exempt by role, not by status, in the link write too."""
    from rootcoz.config import Settings

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch("rootcoz.config._cached_settings", Settings(require_approval=True)),
    ):
        await storage.create_admin_user("boss")
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET status = 'pending' WHERE username = ?", ("boss",)
            )
            await db.commit()

        created = await storage.add_user_identity("boss", "google", "sub-1")

    assert created["username"] == "boss"
    assert created["subject"] == "sub-1"


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
    succeed, leaving a keyless SSO-created account with no way in at all.
    """
    await storage.create_keyless_user("alice", created_via="proxy")
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


async def test_approval_records_the_approver_not_the_requested_provider(
    setup_test_db: Path,
) -> None:
    """A flow that fixed no provider learns it at approval; a conflict is refused.

    And a code fixed to github, approved from a username-and-key session, keeps
    github as what it was opened for while leaving the session with no issuer —
    nobody signed in through github.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        open_code = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=5
        )
        assert (
            await storage.authorize_device_code(
                open_code["user_code"], username="alice", provider="google"
            )
            == "approved"
        )
        assert await storage.redeem_device_code(open_code["device_code"]) == {
            "username": "alice",
            "approved_provider": "google",
        }

        fixed = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=5, provider="github"
        )
        # Approving a github flow from a google session must not relabel it.
        assert (
            await storage.authorize_device_code(
                fixed["user_code"], username="alice", provider="google"
            )
            == "provider_mismatch"
        )
        assert (
            await storage.authorize_device_code(fixed["user_code"], username="alice")
            == "approved"
        )
        requested = await storage.get_device_code_by_user_code(fixed["user_code"])
        assert requested is not None and requested["provider"] == "github"
        assert await storage.redeem_device_code(fixed["device_code"]) == {
            "username": "alice",
            "approved_provider": None,
        }


async def test_a_first_login_ticket_is_single_use(setup_test_db: Path) -> None:
    """A leaked choice URL must not be replayable."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="sub-1",
            email="alice@example.com",
            suggested_username="alice",
            binding="browser-1",
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
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-1",
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
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-1",
        )
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT * FROM sso_link_tickets")
            row = dict(await cursor.fetchone())

    assert ticket not in " ".join(str(value) for value in row.values())
    assert row["ticket_hash"] == storage._hash_session_token(ticket)


async def test_an_expired_ticket_cannot_be_used(setup_test_db: Path) -> None:
    """Both reads check the clock; the purge is housekeeping, not the gate."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-1",
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


async def test_the_ticket_binding_is_stored_and_is_not_derivable(
    setup_test_db: Path,
) -> None:
    """The ticket travels in a URL fragment, so it cannot be the credential.

    The binding is minted separately by the browser that started the login, and
    the row keeps only its digest — a digest nobody can compute from the ticket.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-secret",
        )
        stored = await storage.sso_ticket_binding_hash(ticket)
        unknown = await storage.sso_ticket_binding_hash("not-a-ticket")

    assert stored == storage._hash_session_token("browser-secret")
    # Anyone holding the ticket can hash it; that must not be what the row holds.
    assert stored != storage._hash_session_token(ticket)
    assert stored != storage._hash_session_token("another-browser")
    assert unknown is None


async def test_an_expired_ticket_yields_no_binding(setup_test_db: Path) -> None:
    """The binding is checked against a live row only, like every other read."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-secret",
        )
        past = (datetime.now(UTC) - timedelta(minutes=1)).strftime(
            storage.SQL_TIMESTAMP_FORMAT
        )
        async with storage._connect_db() as db:
            await db.execute("UPDATE sso_link_tickets SET expires_at = ?", (past,))
            await db.commit()

        assert await storage.sso_ticket_binding_hash(ticket) is None


async def test_a_ticket_without_a_binding_yields_none(setup_test_db: Path) -> None:
    """A row minted before the column existed cannot be trusted, so it is refused."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        ticket = await storage.create_sso_ticket(
            provider="google",
            subject="s",
            email=None,
            suggested_username="alice",
            binding="browser-secret",
        )
        async with storage._connect_db() as db:
            await db.execute("UPDATE sso_link_tickets SET binding_hash = NULL")
            await db.commit()

        # The ticket itself is still live; only the binding is missing.
        assert await storage.peek_sso_ticket(ticket) is not None
        assert await storage.sso_ticket_binding_hash(ticket) is None


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
            == "approved"
        )

        # The approval page reads by the code a human typed, in any case.
        approval_view = await storage.get_device_code_by_user_code(
            f"  {code['user_code'].lower()} "
        )
        assert approval_view is not None
        assert approval_view["authorized"] is True

        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "approved_provider": None,
        }
        # A second claim is refused — the code is claimed, exactly once — but the
        # row survives so a failed session can be retried without the terminal
        # being told its code expired.
        assert await storage.redeem_device_code(code["device_code"]) is None
        assert await storage.get_device_code(code["device_code"]) is not None

        assert await storage.release_device_code(code["device_code"]) is True
        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "approved_provider": None,
        }

        # The exchange is the step that ends the flow: one commit writes the
        # session and spends the code.
        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )
        assert minted is not None
        # The rights come from the row, not from the caller.
        assert minted["role"] == "reviewer"
        assert minted["is_admin"] is False
        assert (await storage.get_session(minted["token"]))["username"] == "alice"
        assert await storage.get_device_code(code["device_code"]) is None
        assert await storage.get_device_code_by_user_code(code["user_code"]) is None
        # Nothing left to exchange: a second exchange cannot mint a second token.
        assert (
            await storage.exchange_device_code_for_session(
                code["device_code"], username="alice"
            )
            is None
        )


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
    assert row["device_code_hash"] == storage._hash_session_token(code["device_code"])
    assert "session_token_hash" not in row


async def test_an_authorized_code_cannot_be_approved_for_someone_else(
    setup_test_db: Path,
) -> None:
    """A second approval must not swap the account behind a pending code."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)

        assert (
            await storage.authorize_device_code(code["user_code"], username="alice")
            == "approved"
        )
        # A second approval is named as what it is, not as a lost race.
        assert (
            await storage.authorize_device_code(code["user_code"], username="bob")
            == "already_approved"
        )
        assert await storage.redeem_device_code(code["device_code"]) == {
            "username": "alice",
            "approved_provider": None,
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
        assert (
            await storage.authorize_device_code(code["user_code"], username="alice")
            == "expired"
        )
        assert await storage.redeem_device_code(code["device_code"]) is None


async def test_concurrent_redemption_has_exactly_one_winner(
    setup_test_db: Path,
) -> None:
    """Eight pollers, one code: one CLI gets the account, seven get nothing.

    ``redeem_device_code`` holds BEGIN IMMEDIATE across the read and the claim,
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
    assert winners == [{"username": "alice", "approved_provider": None}]


async def test_a_failed_session_write_spends_nothing(setup_test_db: Path) -> None:
    """The session and the spend are one commit: neither lands without the other."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="alice")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        with (
            patch.object(
                storage, "_insert_session_in_tx", side_effect=RuntimeError("storage")
            ),
            pytest.raises(RuntimeError),
        ):
            await storage.exchange_device_code_for_session(
                code["device_code"], username="alice"
            )

        # Nothing was written, and the code is still there to retry with.
        assert await storage.get_device_code(code["device_code"]) is not None
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE username = 'alice'"
            )
            assert (await cursor.fetchone())["n"] == 0

        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )
        assert minted is not None
        assert await storage.get_device_code(code["device_code"]) is None


async def test_a_claim_cannot_be_exchanged_for_another_account(
    setup_test_db: Path,
) -> None:
    """The approval names the account: a claim is not transferable."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="alice")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        assert (
            await storage.exchange_device_code_for_session(
                code["device_code"], username="bob"
            )
            is None
        )
        assert (
            await storage.exchange_device_code_for_session(
                code["device_code"], username="alice"
            )
            is not None
        )


async def test_a_downgraded_account_cannot_redeem_its_admin_session(
    setup_test_db: Path,
) -> None:
    """A downgrade that commits while the code is pending must win.

    Approval happens minutes before redemption. If the exchange took the rights
    the poll read, the demotion's own session invalidation would lose: it would
    mint a fresh administrator session from the rights it had before.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_admin_user("boss")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="boss")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        # Demoted (which also drops every session) before the terminal polls.
        await storage.change_user_role("boss", "reviewer")

        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="boss"
        )
        assert minted is not None
        assert minted["role"] == "reviewer"
        assert minted["is_admin"] is False
        session = await storage.get_session(minted["token"])
        assert session is not None
        assert session["role"] == "reviewer"
        assert not session["is_admin"]


async def test_the_exchange_reads_the_role_at_mint_time(setup_test_db: Path) -> None:
    """The minted rights are the row's at mint, in either direction, not the poll's.

    The signature no longer accepts ``role``/``is_admin``: a stale value read
    before the exchange is unrepresentable. A promotion committed while the code
    waited is honoured the same way a demotion wins — the row is the only source.
    """
    import inspect

    parameters = inspect.signature(storage.exchange_device_code_for_session).parameters
    assert "role" not in parameters
    assert "is_admin" not in parameters

    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="alice")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        # Promoted after the approval, before the terminal redeemed the claim.
        await storage.change_user_role("alice", "admin")

        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )
        assert minted is not None
        assert minted["role"] == "admin"
        assert minted["is_admin"] is True
        session = await storage.get_session(minted["token"])
        assert session is not None
        assert session["role"] == "admin"
        assert bool(session["is_admin"]) is True


async def test_a_row_less_bootstrap_admin_is_minted_as_an_admin(
    setup_test_db: Path,
) -> None:
    """The bootstrap admin has no users row: it is an administrator by construction."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="admin")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="admin"
        )
        assert minted is not None
        assert minted["role"] == "admin"
        assert minted["is_admin"] is True


async def test_an_account_that_lost_its_row_gets_no_session(
    setup_test_db: Path,
) -> None:
    """Only the reserved name may be row-less; anything else is a deleted account."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await storage.authorize_device_code(code["user_code"], username="alice")
        assert await storage.redeem_device_code(code["device_code"]) is not None

        await storage.delete_user("alice")

        assert (
            await storage.exchange_device_code_for_session(
                code["device_code"], username="alice"
            )
            is None
        )


async def test_purge_removes_only_expired_device_codes(setup_test_db: Path) -> None:
    """Housekeeping never eats a code a human is still about to approve."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        live = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        stale = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
        await _expire_device_code(stale["device_code"])

        assert await storage.purge_expired_device_codes() == 1
        assert await storage.get_device_code(live["device_code"]) is not None
        assert await storage.get_device_code(stale["device_code"]) is None


async def test_a_pre_sso_database_upgrades_through_the_username_merge(
    temp_db_path: Path,
) -> None:
    """A database from before SSO must still start up.

    The username merge deletes the rows keyed by a duplicate's ``users.id``,
    including its SSO rows — so it may only run once those tables exist. On a
    deployment that predates SSO the tables do not exist when the old database
    is first reopened, which a fresh test database (and therefore CI) never
    reproduces: the merge must run after the table creation, not before it.
    """
    with patch.object(storage, "DB_PATH", temp_db_path):
        # A modern database, made to look like one from before SSO: the three SSO
        # tables and the username index are gone, and two case variants are back.
        await storage.init_db()
        async with storage._connect_db() as db:
            for table in ("user_identities", "sso_device_codes", "sso_link_tickets"):
                await db.execute(f"DROP TABLE {table}")
            # The index is what tells the merge it already ran.
            await db.execute("DROP INDEX idx_users_username_lower")
            await db.execute(
                "INSERT INTO users (username, role, created_at)"
                " VALUES ('Frank', 'operator', '2024-01-01 00:00:00')"
            )
            await db.execute(
                "INSERT INTO users (username, role, created_at)"
                " VALUES ('FRANK', 'admin', '2024-06-01 00:00:00')"
            )
            await db.commit()

        # The upgrade path, to completion: the merge deletes rows from the SSO
        # tables, so it may only run once they exist again.
        await storage.init_db()

        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT username, role FROM users")
            users = [dict(row) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row["name"] for row in await cursor.fetchall()}

    assert users == [{"username": "frank", "role": "admin"}]
    assert {"user_identities", "sso_device_codes", "sso_link_tickets"} <= tables


async def _approve_and_claim(username: str) -> dict:
    """Take a device code all the way to a claimed, unspent exchange."""
    code = await storage.create_device_code(ttl_seconds=600, interval_seconds=5)
    await storage.authorize_device_code(code["user_code"], username=username)
    assert await storage.redeem_device_code(code["device_code"]) is not None
    return code


@pytest.mark.parametrize("block", ["disabled", "locked", "pending", "rejected"])
async def test_a_block_committed_while_the_code_waited_refuses_the_mint(
    setup_test_db: Path, block: str
) -> None:
    """Approval is minutes old; the account may be blocked by the time it mints.

    The block already dropped the account's sessions, so a token minted after it
    would outlive that invalidation. The exchange re-reads the row inside its own
    transaction and refuses, naming the block so the poll can audit it.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        code = await _approve_and_claim("alice")

        if block == "disabled":
            assert await storage.set_user_disabled("alice", True) is not None
        else:
            column = "locked_at" if block == "locked" else "status"
            value = storage._now_str() if block == "locked" else block
            async with storage._connect_db() as db:
                await db.execute(
                    f"UPDATE users SET {column} = ? WHERE username = 'alice'",
                    (value,),
                )
                await db.commit()

        refused = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )
        # Named, not silent: the poll reports it as a refusal with this reason.
        assert refused == {"refused": f"account_{block}"}

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE username = 'alice'"
            )
            assert (await cursor.fetchone())["n"] == 0
        # The claim is spent, so no poll can mint while the block stands; an
        # admin who lifts it does not hand the terminal a token it never asked
        # for again — the person signs in afresh.
        assert await storage.get_device_code(code["device_code"]) is not None
        again = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )
        assert again is not None and "token" not in again


async def test_below_require_approval_a_pending_row_still_mints(
    setup_test_db: Path,
) -> None:
    """With approval off a pending row is an ordinary account, exactly as at login."""
    from rootcoz.config import Settings

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch("rootcoz.config._cached_settings", Settings(require_approval=False)),
    ):
        await storage.create_user("alice", status="pending")
        code = await _approve_and_claim("alice")

        minted = await storage.exchange_device_code_for_session(
            code["device_code"], username="alice"
        )

        assert minted is not None
        assert minted["role"] == "reviewer"
        assert (await storage.get_session(minted["token"]))["username"] == "alice"


class _MovableClock:
    """Stand in for ``storage.datetime`` and hand out a time the test controls.

    Patching the module's ``datetime`` (not the system clock) lets a call span
    more time than a real poll ever would, without the test sleeping.
    """

    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self, _tz: object | None = None) -> datetime:
        return self.current

    # ``record_device_code_poll`` parses the column it just formatted.
    strptime = staticmethod(datetime.strptime)


async def test_a_contended_poll_stamps_the_moment_it_decides(
    setup_test_db: Path,
) -> None:
    """A lock wait must not buy the next poll a free interval.

    The first poll is held between the call and the write lock for longer than
    the interval — what contention with another writer looks like. Stamped
    before that wait, the row says the poll happened an interval ago, so the
    poll that arrives right after it commits is let through. Stamped once the
    lock is held, the wait costs the flooder nothing.
    """
    interval = 5
    with patch.object(storage, "DB_PATH", setup_test_db):
        code = await storage.create_device_code(
            ttl_seconds=600, interval_seconds=interval
        )
        clock = _MovableClock(datetime.now(UTC))
        real_connect_db = storage._connect_db
        waiting = True

        @contextlib.asynccontextmanager
        async def contended_connect_db(  # type: ignore[no-untyped-def]
            db_path: Path | None = None,
        ):
            nonlocal waiting
            async with real_connect_db(db_path) as db:
                if waiting:
                    waiting = False
                    # The write lock was held elsewhere for longer than the
                    # interval before this poll got it.
                    clock.current += timedelta(seconds=interval + 5)
                yield db

        with (
            patch.object(storage, "datetime", clock),
            patch.object(storage, "_connect_db", contended_connect_db),
        ):
            assert await storage.record_device_code_poll(code["device_code"]) is True

            async with real_connect_db() as db:
                cursor = await db.execute(
                    "SELECT last_polled_at FROM sso_device_codes"
                    " WHERE device_code_hash = ?",
                    (storage._hash_session_token(code["device_code"]),),
                )
                stamped = (await cursor.fetchone())["last_polled_at"]
            # The stored instant is the one the decision used: after the wait.
            assert stamped == clock.current.strftime(storage.SQL_TIMESTAMP_FORMAT)

            # The poll that lands immediately after the delayed one commits is
            # still inside the window.
            assert await storage.record_device_code_poll(code["device_code"]) is False


async def test_a_keyless_row_is_created_without_a_key(
    setup_test_db: Path,
) -> None:
    """Provisioning records the account, never a secret nobody was shown."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user(
            "sso-provisioned", status="pending", created_via="proxy"
        )

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash, role, status, created_via, can_view_reports"
                " FROM users WHERE username = 'sso-provisioned'"
            )
            row = dict(await cursor.fetchone())

        # Same shape ``create_user`` leaves, minus the phantom key hash.
        assert row == {
            "api_key_hash": None,
            "role": "reviewer",
            "status": "pending",
            "created_via": "proxy",
            "can_view_reports": 0,
        }
        # Nothing to authenticate with until somebody issues a key.
        assert await storage.user_has_key("sso-provisioned") is False

        with pytest.raises(ValueError, match="already exists"):
            await storage.create_keyless_user("sso-provisioned", created_via="proxy")


async def test_a_provisioned_name_cannot_be_claimed_by_registering_it(
    setup_test_db: Path,
) -> None:
    """A name is not a credential: registration must not take the row over."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user("sso-provisioned", created_via="proxy")

        with pytest.raises(ValueError, match="already exists"):
            await storage.create_user("sso-provisioned")

        # Still keyless: the refused claim wrote nothing.
        assert await storage.user_has_key("sso-provisioned") is False


async def test_registering_an_existing_keyless_row_reveals_no_more_than_a_keyed_one(
    setup_test_db: Path,
) -> None:
    """One refusal for both shapes: the reply must not fingerprint the row.

    A keyless row is what ``create_keyless_user`` leaves behind (the proxy's
    accounts), so ``created_via``/``api_key_hash`` must not steer the answer —
    the caller learns only that the name is taken.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user("sso-provisioned", created_via="proxy")
        _name, existing_key = await storage.create_user("keyed")

        errors = []
        for username in ("sso-provisioned", "keyed"):
            with pytest.raises(ValueError) as refusal:
                await storage.create_user(username)
            errors.append(str(refusal.value))

        # Same wording apart from the name it echoes.
        assert errors[0] == storage._REGISTRATION_NAME_TAKEN.format(
            username="sso-provisioned"
        )
        assert errors[1] == storage._REGISTRATION_NAME_TAKEN.format(username="keyed")

        # Neither attempt wrote a key: the keyless row stays keyless and the
        # keyed row keeps the one key its owner already has.
        assert await storage.user_has_key("sso-provisioned") is False
        assert (await storage.get_user_by_key(existing_key))["username"] == "keyed"


async def test_an_administrator_rotation_gives_a_keyless_account_a_key(
    setup_test_db: Path,
) -> None:
    """The documented recovery: an audited rotation, not a name claim."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user("sso-provisioned", created_via="proxy")

        raw_key = await storage.rotate_user_key("sso-provisioned")

        assert (await storage.get_user_by_key(raw_key))["username"] == (
            "sso-provisioned"
        )


async def test_a_new_name_still_registers_and_gets_its_key(
    setup_test_db: Path,
) -> None:
    """Refusing every existing name must not refuse the names that are free."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        username, raw_key = await storage.create_user("newcomer")

        assert username == "newcomer"
        assert (await storage.get_user_by_key(raw_key))["username"] == "newcomer"


async def test_list_identities_for_users_empty_input(setup_test_db: Path) -> None:
    """An empty username list returns an empty dict without touching the DB."""
    # No DB patch needed — the function must short-circuit before connecting.
    assert await storage.list_identities_for_users([]) == {}


async def test_list_identities_for_users_mixed(
    setup_test_db: Path,
) -> None:
    """Batched lookup returns grouped, provider-ordered identities."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.create_user("bob")
        await storage.create_user("carol")

        await storage.add_user_identity(
            "alice", "google", "g-alice", email="alice@g.com"
        )
        await storage.add_user_identity(
            "alice", "github", "gh-alice", email="alice@gh.com"
        )
        await storage.add_user_identity(
            "carol", "google", "g-carol", email="carol@g.com"
        )
        # bob has no identities

        result = await storage.list_identities_for_users(["alice", "bob", "carol"])

        # bob is absent (no identities)
        assert "bob" not in result

        # alice: two identities, ordered by provider
        assert [i["provider"] for i in result["alice"]] == ["github", "google"]
        assert result["alice"][0]["email"] == "alice@gh.com"
        assert result["alice"][1]["email"] == "alice@g.com"

        # carol: one identity
        assert len(result["carol"]) == 1
        assert result["carol"][0]["provider"] == "google"
