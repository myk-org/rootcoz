"""Tests for first-party SSO: the provider package and the browser flow.

Stage 3 of issue #197 covers Google (the OIDC path OpenShift and Red Hat SSO
reuse later); Stage 6 adds the first-login choice screen. The IdP itself is
mocked — no test talks to Google — but the ID-token verification runs for real
against locally generated keys.

The rule these tests protect: a **name is never used to find an account**. The
first login asks the person (claim an existing account with its own credential,
or create one), and a `(provider, subject)` link is the only thing that resolves
a login automatically.
"""

import base64
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage
from rootcoz.auth import identity as auth_identity
from rootcoz.auth.sso import AuthError, ClaimSet, configured_providers, get_provider
from rootcoz.auth.sso import state as sso_state
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.config import get_settings
from rootcoz.encryption import encrypt_value
from tests.conftest import build_test_env, make_app_client

_SSO_ENV = {
    "SSO_GOOGLE_CLIENT_ID": "test-client-id",
    "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    "PUBLIC_BASE_URL": "https://rootcoz.example",
}


def _claims(
    subject: str = "sub-1",
    username_hint: str = "alice",
    email: str | None = "alice@example.com",
) -> ClaimSet:
    """A claim set as a provider would produce it."""
    return ClaimSet(
        subject=subject,
        username_hint=username_hint,
        email=email,
        claims={"sub": subject, "email": email, "preferred_username": username_hint},
    )


def _sealed(provider: str = "google", next_path: str = "/report/1") -> tuple[str, str]:
    """Seal a login attempt and return (sealed cookie, state value)."""
    attempt, sealed, _challenge = sso_state.new_attempt(
        provider=provider, next_path=next_path, ttl_seconds=600
    )
    return sealed, attempt.state


async def _ticket(
    client=None,
    *,
    provider: str = "google",
    subject: str = "sub-1",
    email: str | None = "alice@example.com",
    suggested: str = "alice",
) -> str:
    """Mint a first-login ticket as the callback would.

    When ``client`` is given, the browser-binding cookie the callback sets is
    attached too — the ticket alone is deliberately not enough.
    """
    ticket = await storage.create_sso_ticket(
        provider=provider,
        subject=subject,
        email=email,
        suggested_username=suggested,
    )
    if client is not None:
        client.cookies.set(sso_state.TICKET_COOKIE, sso_state.ticket_binding(ticket))
    return ticket


async def _audit_actions(temp_db_path: Path) -> list[dict]:
    """Every audit row written to the test database."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        result = await storage.list_audit_events(limit=200)
    return result["events"]


def test_a_provider_is_offered_only_when_it_is_configured() -> None:
    """A button that cannot work must never be rendered."""
    import os

    with patch.dict(os.environ, build_test_env(), clear=True):
        get_settings.cache_clear()
        assert configured_providers(get_settings()) == []
        assert get_provider("google", get_settings()) is None
        assert get_provider("nope", get_settings()) is None

    with patch.dict(
        os.environ,
        build_test_env(
            SSO_GOOGLE_CLIENT_ID="cid",
            SSO_GOOGLE_CLIENT_SECRET="secret",  # pragma: allowlist secret
        ),
        clear=True,
    ):
        get_settings.cache_clear()
        assert [p.name for p in configured_providers(get_settings())] == ["google"]
        assert get_provider("google", get_settings()) is not None
        assert get_provider("github", get_settings()) is None

    get_settings.cache_clear()


def test_the_registry_and_storage_agree_on_provider_names() -> None:
    """A provider storage does not know would fail at the first link."""
    from rootcoz.auth.sso import PROVIDER_BY_NAME

    assert set(PROVIDER_BY_NAME) <= set(storage.SSO_PROVIDERS)
    assert GoogleProvider.issuer == "https://accounts.google.com"


def test_the_providers_endpoint_lists_only_configured_providers(
    temp_db_path: Path,
) -> None:
    """The login page asks this and renders exactly what comes back."""
    for client in make_app_client(temp_db_path):
        assert client.get("/api/auth/sso/providers").json() == {"providers": []}

    for client in make_app_client(temp_db_path, _SSO_ENV):
        assert client.get("/api/auth/sso/providers").json() == {
            "providers": [{"name": "google", "display_name": "Google"}]
        }


def test_the_state_cookie_is_sealed_and_bound_to_its_provider() -> None:
    """The attempt must not be readable, transferable, or open to another IdP."""
    sealed, state = _sealed()
    attempt = sso_state.open_attempt(sealed, provider="google")
    assert attempt.state == state
    assert attempt.next_path == "/report/1"
    assert attempt.code_verifier not in sealed
    assert attempt.nonce

    with pytest.raises(sso_state.StateError, match="another provider"):
        sso_state.open_attempt(sealed, provider="github")
    with pytest.raises(sso_state.StateError, match="missing"):
        sso_state.open_attempt("", provider="google")


def test_a_forged_plaintext_attempt_is_refused() -> None:
    """An attacker who can set the cookie must not be believed.

    The general decryption helper passes unprefixed values through as legacy
    plaintext; a sealed cookie cannot allow that, or the CSRF nonce and the PKCE
    verifier would both be attacker-chosen.
    """
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "purpose": "rootcoz.sso.login_attempt",
                    "provider": "google",
                    "state": "attacker-nonce",
                    "verifier": "attacker-verifier",
                    "nonce": "attacker-id-token-nonce",
                    "next": "/",
                    "issued_at": int(time.time()),
                    "ttl": 600,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )

    with pytest.raises(sso_state.StateError, match="not sealed"):
        sso_state.open_attempt(forged, provider="google")


def test_a_sealed_value_minted_for_another_purpose_is_refused() -> None:
    """A sealed cookie from another feature must not be replayable here."""
    other = encrypt_value(
        json.dumps(
            {
                "purpose": "something.else",
                "provider": "google",
                "state": "s",
                "verifier": "v",
                "nonce": "n",
                "next": "/",
                "issued_at": int(time.time()),
                "ttl": 600,
            }
        )
    )
    packed = base64.urlsafe_b64encode(other.encode()).decode().rstrip("=")
    with pytest.raises(sso_state.StateError, match="something else"):
        sso_state.open_attempt(packed, provider="google")


def test_an_expired_attempt_is_refused() -> None:
    """A link captured hours ago cannot be replayed."""
    payload = json.dumps(
        {
            "purpose": "rootcoz.sso.login_attempt",
            "provider": "google",
            "state": "s",
            "verifier": "v",
            "nonce": "n",
            "next": "/",
            "issued_at": 1,
            "ttl": 60,
        }
    )
    stale = (
        base64.urlsafe_b64encode(encrypt_value(payload).encode()).decode().rstrip("=")
    )
    with pytest.raises(sso_state.StateError, match="expired"):
        sso_state.open_attempt(stale, provider="google")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/report/1", "/report/1"),
        ("https://evil.example/x", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("", "/"),
        ("/" + "x" * 600, "/"),
    ],
)
def test_the_post_login_destination_cannot_leave_the_site(
    raw: str, expected: str
) -> None:
    """An SSO link must not double as an open redirect."""
    assert sso_state.safe_next(raw) == expected


async def test_an_unlinked_identity_gets_a_suggestion_not_an_account(
    temp_db_path: Path,
) -> None:
    """The resolver never creates anything: it reports that a choice is needed."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        choice = await auth_identity.resolve("google", _claims(), get_settings())

        assert isinstance(choice, auth_identity.LoginChoice)
        assert choice.suggested_username == "alice"
        assert choice.email == "alice@example.com"
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_a_reserved_name_is_never_suggested(temp_db_path: Path) -> None:
    """The screen never proposes a name storage would refuse."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        for hint in ("admin", "rootcoz-ai", "re-analyze"):
            choice = await auth_identity.resolve(
                "google", _claims(username_hint=hint, email=None), get_settings()
            )
            assert isinstance(choice, auth_identity.LoginChoice)
            assert choice.suggested_username == ""

        choice = await auth_identity.resolve(
            "google",
            _claims(username_hint="admin", email="alice@example.com"),
            get_settings(),
        )
        assert isinstance(choice, auth_identity.LoginChoice)
        assert choice.suggested_username == "alice"


async def test_a_linked_account_skips_the_choice(temp_db_path: Path) -> None:
    """The link — never the name — is what resolves a login automatically."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")

        resolved = await auth_identity.resolve(
            "google", _claims(username_hint="alice"), get_settings()
        )
        assert isinstance(resolved, auth_identity.IdentityResolution)
        assert resolved.username == "ruthn"
        assert resolved.created is False


async def test_an_orphaned_link_is_healed_on_the_next_login(
    temp_db_path: Path,
) -> None:
    """A link whose account is gone must not lock that person out forever."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")
        async with storage._connect_db() as db:
            await db.execute("DELETE FROM users WHERE username = 'alice'")
            await db.commit()

        choice = await auth_identity.resolve("google", _claims(), get_settings())

        assert isinstance(choice, auth_identity.LoginChoice)
        # The orphan is gone, so the choice screen can create or link again.
        assert await storage.get_user_identity("google", "sub-1") is None


async def test_a_disabled_linked_account_is_refused(temp_db_path: Path) -> None:
    """A live link to a disabled account does not authenticate."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE users SET disabled = 1 WHERE username = 'ruthn'")

        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", _claims(), get_settings())
        assert excinfo.value.reason == "account_disabled"


async def test_the_callback_redirects_to_the_choice_screen(
    temp_db_path: Path,
) -> None:
    """First login: a ticket, not an account and not a refusal."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login#sso_ticket=")
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_the_ticket_never_reveals_the_address(temp_db_path: Path) -> None:
    """Name + masked email would reconstruct it, so only the domain is returned."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        body = client.post("/api/auth/sso/ticket", json={"ticket": ticket}).json()

        assert body["provider"] == "google"
        assert body["suggested_username"] == "alice"
        assert body["email_domain"] == "example.com"
        assert "alice@example.com" not in json.dumps(body)

        assert (
            client.post("/api/auth/sso/ticket", json={"ticket": "nope"}).status_code
            == 404
        )


async def test_creating_an_account_with_the_chosen_name(temp_db_path: Path) -> None:
    """The person picks the name; it is created, linked and signed in."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "username": "ruthn",
            "status": "active",
            # The ticket's destination rides back so the SPA does not guess it.
            "next": "/",
        }
        assert response.cookies.get("rootcoz_session")
        identity = await storage.get_user_identity("google", "sub-1")
        assert identity is not None
        assert identity["username"] == "ruthn"
        assert client.get("/api/auth/me").json()["username"] == "ruthn"

        replay = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "other"}
        )
        assert replay.status_code == 404


async def test_a_taken_name_is_refused_not_suffixed(temp_db_path: Path) -> None:
    """A name already in use is a bad choice, never a silent rename."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, existing_key = await storage.create_user("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 400
        # No `ruthn-1`, no second row, and the existing account is untouched.
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT username FROM users ORDER BY username")
            assert [row["username"] for row in await cursor.fetchall()] == ["ruthn"]
        assert await storage.get_user_by_key(existing_key) is not None
        # The ticket is still usable with another name.
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "rnetser"}
            ).status_code
            == 200
        )


async def test_a_taken_keyless_name_cannot_be_claimed_by_creating(
    temp_db_path: Path,
) -> None:
    """``create_user``'s "existing account without a key" path must not apply.

    That path mints a key for an existing keyless row; reusing it here would hand
    somebody else's account to whoever completed the IdP login.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        async with storage._connect_db() as db:
            await db.execute(
                "INSERT INTO users (username, role, status) VALUES ('ruthn', 'reviewer', 'active')"
            )
            await db.commit()
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 400
        assert await storage.get_user_identity("google", "sub-1") is None
        # The keyless account did not gain a key behind its owner's back.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["api_key_hash"] is None


async def test_create_records_provider_provenance(temp_db_path: Path) -> None:
    """``created_via`` names the provider, and a later link does not rewrite it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT created_via FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["created_via"] == "google"


async def test_linking_does_not_rewrite_provenance(temp_db_path: Path) -> None:
    """An account created internally keeps ``internal`` after a link."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn", created_via="internal")
        ticket = await _ticket(client)
        # A fresh key the test can use; provenance must survive the link.
        raw_key = await storage.rotate_own_key("ruthn")

        assert (
            client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            ).status_code
            == 200
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT created_via FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["created_via"] == "internal"


async def test_the_choice_keeps_the_requested_destination(temp_db_path: Path) -> None:
    """A deep link survives the detour and rides back so the SPA can use it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed(next_path="/report/42")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )
        # The fragment carries the ticket; the screen reads it and POSTs it back.
        ticket = response.headers["location"].split("sso_ticket=", 1)[1]
        created = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert created.status_code == 200
        assert created.json()["next"] == "/report/42"


async def test_linking_respects_the_allow_list(temp_db_path: Path) -> None:
    """Linking writes a credential, so the write list applies to it."""
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 403
        assert await storage.list_user_identities("ruthn") == []


async def test_a_stolen_ticket_cannot_be_redeemed_in_another_browser(
    temp_db_path: Path,
) -> None:
    """The URL alone must not bind an identity to the thief's own account.

    Otherwise a stolen link lets an attacker attach the victim's IdP identity to
    the attacker's account, so every later victim login lands there.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        # The thief's browser: same ticket, no binding cookie.
        client.cookies.clear()

        assert (
            client.post("/api/auth/sso/ticket", json={"ticket": ticket}).status_code
            == 404
        )

        await storage.create_user("attacker")
        attacker_key = await storage.rotate_own_key("attacker")
        link = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "attacker", "api_key": attacker_key},
        )
        assert link.status_code == 404
        assert await storage.get_user_identity("google", "sub-1") is None

        # The browser that started the flow still holds the binding and can use it.
        client.cookies.set(sso_state.TICKET_COOKIE, sso_state.ticket_binding(ticket))
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
            ).status_code
            == 200
        )


async def test_linking_twice_reports_a_conflict_at_the_endpoint(
    temp_db_path: Path,
) -> None:
    """A subject already owned by another account is a 409, not a silent relink."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")

        await storage.create_user("bob")
        bob_key = await storage.rotate_own_key("bob")
        ticket = await _ticket(client, subject="sub-1")
        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "bob", "api_key": bob_key},
        )

        assert response.status_code == 409
        # The refusal does not name the account that owns the subject.
        assert "alice" not in response.json()["detail"]
        assert await storage.list_user_identities("bob") == []


async def test_a_stale_link_that_raced_a_delete_is_reported_as_missing(
    temp_db_path: Path,
) -> None:
    """A vanished account is 404, never "already linked"."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        with patch(
            "rootcoz.storage.add_user_identity",
            side_effect=ValueError("User 'ruthn' not found"),
        ):
            response = client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            )

        assert response.status_code == 404
        assert "no longer exists" in response.json()["detail"]


async def test_the_bootstrap_admin_is_told_it_has_no_account_to_link(
    temp_db_path: Path,
) -> None:
    """A valid ADMIN_KEY must not be reported as an invalid credential."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={
                "ticket": ticket,
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )

        assert response.status_code == 400
        assert "bootstrap admin" in response.json()["detail"]
        # And the ticket is untouched, so the person can still create an account.
        assert await storage.peek_sso_ticket(ticket) is not None


async def test_a_bad_key_counts_towards_the_lockout(temp_db_path: Path) -> None:
    """The link endpoint verifies the same credential, so it needs the same guard."""
    env = dict(_SSO_ENV, LOGIN_LOCKOUT_ENABLED="true", LOGIN_LOCKOUT_THRESHOLD="2")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        ticket = await _ticket(client)

        for _attempt in range(2):
            response = client.post(
                "/api/auth/sso/link",
                json={
                    "ticket": ticket,
                    "username": "ruthn",
                    # A deliberately wrong credential, not a secret.
                    "api_key": "wrong-key-long",  # pragma: allowlist secret
                },
            )
            assert response.status_code == 401

        # The account is locked, so even the right key cannot claim it now.
        raw_key = await storage.rotate_own_key("ruthn")
        blocked = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )
        assert blocked.status_code == 403


async def test_creating_with_an_invalid_name_keeps_the_ticket(
    temp_db_path: Path,
) -> None:
    """A rejected name must not cost the person another trip through the IdP."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        for bad in ("admin", "rootcoz-ai", "a", "-bad"):
            response = client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": bad}
            )
            assert response.status_code == 400, bad

        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )


async def test_creating_under_approval_lands_on_pending(temp_db_path: Path) -> None:
    """The approval gate applies to a choice-screen account exactly as before."""
    for client in make_app_client(
        temp_db_path, dict(_SSO_ENV, REQUIRE_APPROVAL="true")
    ):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert await storage.get_user_status("alice") == "pending"
        assert client.get("/api/auth/me").status_code == 403


async def test_creating_respects_the_allow_list(temp_db_path: Path) -> None:
    """``ALLOWED_USERS`` bounds who may be created, even by choosing a name."""
    env = dict(_SSO_ENV, ALLOWED_USERS="ruthn")
    for client in make_app_client(temp_db_path, env):
        ticket = await _ticket(client)

        refused = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert refused.status_code == 400

        allowed = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )
        assert allowed.status_code == 200


async def test_linking_an_existing_account_with_its_key(temp_db_path: Path) -> None:
    """The credential proves ownership; the name never does."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, raw_key = await storage.create_user("ruthn")
        before = await storage.get_user_by_username("ruthn")
        assert before is not None
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "username": "ruthn",
            "status": "active",
            # The ticket's destination rides back so the SPA does not guess it.
            "next": "/",
        }
        identity = await storage.get_user_identity("google", "sub-1")
        assert identity is not None
        assert identity["username"] == "ruthn"
        after = await storage.get_user_by_username("ruthn")
        assert after is not None
        assert after["id"] == before["id"]
        assert await storage.get_user_by_key(raw_key) is not None
        assert client.get("/api/auth/me").json()["username"] == "ruthn"


async def test_a_wrong_key_cannot_claim_an_account(temp_db_path: Path) -> None:
    """Without the credential the ticket entitles the holder to nothing."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={
                "ticket": ticket,
                "username": "ruthn",
                # A deliberately wrong credential, not a secret.
                "api_key": "wrong-key-long",  # pragma: allowlist secret
            },
        )

        assert response.status_code == 401
        assert await storage.list_user_identities("ruthn") == []
        assert await storage.peek_sso_ticket(ticket) is not None


async def test_a_disabled_account_cannot_be_claimed(temp_db_path: Path) -> None:
    """A refused account is refused before the link is written."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, raw_key = await storage.create_user("ruthn")
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE users SET disabled = 1 WHERE username = 'ruthn'")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 403
        assert await storage.list_user_identities("ruthn") == []


async def test_a_second_login_uses_the_link_and_skips_the_screen(
    temp_db_path: Path,
) -> None:
    """Once linked, the callback signs in directly and stamps the session."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )
        client.cookies.clear()

        sealed, state = _sealed(next_path="/report/42")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/report/42"
        token = response.cookies.get("rootcoz_session")
        assert token
        session = await storage.get_session(token)
        assert session is not None
        assert session["auth_provider"] == "google"


async def test_a_linked_pending_account_lands_on_pending(temp_db_path: Path) -> None:
    """The approval gate still decides where a linked login lands."""
    env = dict(_SSO_ENV, REQUIRE_APPROVAL="true")
    for client in make_app_client(temp_db_path, env):
        ticket = await _ticket(client)
        created = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert created.json()["status"] == "pending"
        client.cookies.clear()

        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/pending"
        assert client.get("/api/auth/me").status_code == 403


def test_login_redirects_to_the_provider_with_a_sealed_state(
    temp_db_path: Path,
) -> None:
    """The start route hands the browser the provider URL and the state cookie."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            response = client.get(
                "/api/auth/sso/google/login?next=/report/1", follow_redirects=False
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("https://accounts.google.com/")
        sealed = response.cookies.get(sso_state.STATE_COOKIE)
        assert sealed
        assert (
            sso_state.open_attempt(sealed, provider="google").next_path == "/report/1"
        )


def test_login_without_public_base_url_fails_cleanly(temp_db_path: Path) -> None:
    """No registered redirect can be built, so the attempt must be refused."""
    env = {
        "SSO_GOOGLE_CLIENT_ID": "test-client-id",
        "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    }
    for client in make_app_client(temp_db_path, env):
        with patch.object(GoogleProvider, "authorization_url", autospec=True) as start:
            response = client.get("/api/auth/sso/google/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?sso_error=")
        assert start.call_count == 0


def test_an_unknown_provider_sends_the_browser_back_to_login(
    temp_db_path: Path,
) -> None:
    """Unknown and unconfigured answer the same way, so nobody can enumerate."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        start = client.get("/api/auth/sso/github/login", follow_redirects=False)
        assert start.status_code == 303
        assert start.headers["location"] == "/login?sso_error=unknown_provider"

        callback = client.get("/api/auth/sso/github/callback", follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"] == "/login?sso_error=unknown_provider"


def test_callback_refuses_a_state_that_does_not_match(temp_db_path: Path) -> None:
    """The CSRF nonce is compared, and a mismatch authenticates nobody."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, _state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=not-the-state",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_mismatch"


def test_a_non_ascii_state_is_a_mismatch_not_a_crash(temp_db_path: Path) -> None:
    """The state comes from the query string; latin-1 must not raise a 500."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, _state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=%C3%A9",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_mismatch"


def test_callback_refuses_a_missing_state_cookie(temp_db_path: Path) -> None:
    """A callback without our sealed cookie is refused, not trusted."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=whatever",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_invalid"


def test_a_failed_exchange_is_refused_and_creates_nothing(
    temp_db_path: Path,
) -> None:
    """A provider error must not create an account."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider,
            "exchange",
            side_effect=AuthError("exchange_failed", "boom"),
            autospec=True,
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=exchange_failed"
        assert response.cookies.get("rootcoz_session") is None


def test_an_idp_error_parameter_is_refused_without_exchanging(
    temp_db_path: Path,
) -> None:
    """A cancelled consent screen is a refusal, not an exception."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(GoogleProvider, "exchange", autospec=True) as exchange:
            response = client.get(
                f"/api/auth/sso/google/callback?error=access_denied&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=provider_error"
        assert exchange.call_count == 0


async def test_the_first_login_choice_is_audited(temp_db_path: Path) -> None:
    """A held-open ticket is an attempt, so the trail records it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"].startswith("auth.sso")
    ]
    assert [r["action"] for r in rows] == ["auth.sso_choice_required"]
    assert rows[0]["details"]["suggested_username"] == "alice"


async def test_create_and_link_are_audited_without_credentials(
    temp_db_path: Path,
) -> None:
    """The rows name the account and the provider — never a key or a ticket."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        created = await _ticket(client, subject="sub-create", suggested="alice")
        client.post(
            "/api/auth/sso/create", json={"ticket": created, "username": "alice"}
        )

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"] == "auth.sso_create"
    ]
    assert len(rows) == 1
    assert rows[0]["actor"] == "alice"
    assert rows[0]["details"] == {
        "provider": "google",
        "created": True,
        "status": "active",
    }
    assert created not in json.dumps(rows)


async def test_an_unknown_provider_refusal_is_audited(temp_db_path: Path) -> None:
    """A refusal reaches the trail even when no provider exists."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        client.get("/api/auth/sso/github/login", follow_redirects=False)

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"].startswith("auth.sso")
    ]
    assert [(r["action"], r["outcome"]) for r in rows] == [
        ("auth.sso_login_failed", "denied")
    ]
    assert rows[0]["details"]["reason"] == "unknown_provider"


def _signed_id_token(
    *,
    issuer: str = GoogleProvider.issuer,
    audience: str = "test-client-id",
    expires_in: int = 300,
    nonce: str = "attempt-nonce",
) -> tuple[str, object]:
    """Build a real RS256 ID token plus the key set that verifies it.

    The token is genuinely signed, so the verification code under test runs for
    real — the point of these tests is that ``oidc.py``'s strongest claim
    ("verified before any claim is trusted") is executed, not assumed.

    Args:
        issuer: ``iss`` claim.
        audience: ``aud`` claim.
        expires_in: Seconds until ``exp``.
        nonce: ``nonce`` claim — the value the attempt sent.

    Returns:
        The compact JWS and a key set usable by ``joserfc``.
    """
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "sub-1",
        "email": "alice@example.com",
        "nonce": nonce,
        "exp": int(_time.time()) + expires_in,
        "iat": int(_time.time()),
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    return token, KeySet.import_key_set({"keys": [key.as_dict(private=False)]})


async def _verify(token: str, key_set: object, nonce: str = "attempt-nonce") -> dict:
    """Run ``_verify_id_token`` with the JWKS fetch stubbed out."""
    provider = GoogleProvider(
        client_id="test-client-id",
        client_secret="s",
        issuer=GoogleProvider.issuer,
    )
    with patch.object(GoogleProvider, "_jwks_for", return_value=key_set, autospec=True):
        return await provider._verify_id_token(
            {"jwks_uri": "https://accounts.google.com/jwks"}, token, nonce
        )


async def test_a_signed_id_token_from_the_issuer_is_accepted() -> None:
    """A token signed by the issuer's key, for this client, is trusted."""
    token, key_set = _signed_id_token()
    claims = await _verify(token, key_set)
    assert claims["sub"] == "sub-1"
    assert claims["email"] == "alice@example.com"


async def test_a_token_from_another_issuer_is_refused() -> None:
    """A well-signed token from a different issuer must not be trusted."""
    token, key_set = _signed_id_token(issuer="https://evil.example")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "issuer" in str(excinfo.value)


async def test_a_token_for_another_client_is_refused() -> None:
    """Someone else's token for the same issuer must not authenticate here."""
    token, key_set = _signed_id_token(audience="another-client")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "audience" in str(excinfo.value)


async def test_an_expired_token_is_refused() -> None:
    """Expiry is enforced by the same validation that checks the signature."""
    token, key_set = _signed_id_token(expires_in=-60)
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_for_another_attempt_is_refused() -> None:
    """A token minted for a different login must not complete this one."""
    token, key_set = _signed_id_token(nonce="the-other-attempt")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_without_exp_is_refused() -> None:
    """``exp`` is required, not merely checked when present."""
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {"iss": GoogleProvider.issuer, "aud": "test-client-id", "sub": "s"}
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    key_set = KeySet.import_key_set({"keys": [key.as_dict(private=False)]})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_with_several_audiences_needs_azp() -> None:
    """A token minted for a sibling client of the same issuer is refused."""
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {
        "iss": GoogleProvider.issuer,
        "aud": ["test-client-id", "sibling-client"],
        "sub": "s",
        "nonce": "attempt-nonce",
        "exp": int(_time.time()) + 300,
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    key_set = KeySet.import_key_set({"keys": [key.as_dict(private=False)]})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert "another party" in str(excinfo.value)


async def test_a_token_signed_by_a_foreign_key_is_refused() -> None:
    """A valid shape with the wrong signing key is a forgery."""
    token, _key_set = _signed_id_token()
    _other_token, other_keys = _signed_id_token(issuer="https://elsewhere.example")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, other_keys)
    assert excinfo.value.reason == "id_token_invalid"
