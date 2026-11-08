"""SSO providers: one module per identity provider.

A provider turns an external login into a :class:`ClaimSet` — it never decides
which local account that is (:mod:`rootcoz.auth.identity`), whether the account
may act (:mod:`rootcoz.auth.approval`), or what credential the request carries
(:mod:`rootcoz.auth`). Its only real responsibilities are the protocol (state,
PKCE, token exchange, claim normalisation) and being **disabled** unless the
deployment configured it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rootcoz.config import Settings


class AuthError(Exception):
    """A login attempt that must be refused, carrying a stable reason code.

    The reason is for the audit trail and the logs, never for the caller: every
    refusal answers the same generic response, so an attacker cannot use the
    difference to probe an account.
    """

    def __init__(
        self, reason: str, message: str = "", *, subject: str | None = None
    ) -> None:
        """Build the error.

        Args:
            reason: Short stable code (``state_mismatch``, ``exchange_failed``).
            message: Operator-facing detail; never sent to the client.
            subject: The local account the refusal was about, when one was
                already resolved — the audit row must name it.
        """
        super().__init__(message or reason)
        self.reason = reason
        self.subject = subject


@dataclass(frozen=True)
class ClaimSet:
    """The provider-agnostic result of a successful external login.

    Attributes:
        subject: The provider's stable identifier for this person. The local
            account is linked to this, never to the username or email — those
            change.
        username_hint: Preferred local username (``preferred_username``,
            GitHub ``login``, or the email local-part). A hint: the identity
            resolver decides, and may fall back when it is unusable.
        email: Email the provider reports, for display and admin search.
        claims: The full claim set, for the audit trail.
    """

    subject: str
    username_hint: str
    email: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


class SSOProvider(ABC):
    """One external identity provider.

    Class attributes:
        name: Machine name (``google``), used in URLs, identities and audit rows.
        display_name: Button label.
    """

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""

    @classmethod
    @abstractmethod
    def from_settings(cls, settings: Settings) -> SSOProvider | None:
        """Build the provider when the deployment configured it.

        Args:
            settings: Application settings for this request.

        Returns:
            A configured provider, or ``None`` when it is not offered.
        """

    @abstractmethod
    async def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str, nonce: str
    ) -> str:
        """Where to send the browser to start the login.

        Args:
            redirect_uri: Callback URL registered with the provider.
            state: CSRF nonce for this attempt.
            code_challenge: PKCE challenge derived from the verifier.
            nonce: OIDC nonce, echoed into the ID token and checked on return —
                the OAuth ``state`` binds the redirect, the nonce binds the
                token itself.

        Returns:
            The provider's authorization URL.
        """

    @abstractmethod
    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str
    ) -> ClaimSet:
        """Turn an authorization code into claims.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: The PKCE verifier issued with the state.
            redirect_uri: The same callback URL used to start the login.
            nonce: The nonce sent with the authorization request; an ID token
                that does not echo it is refused.

        Returns:
            The normalised claims.

        Raises:
            AuthError: When the exchange fails or the response is unusable.
        """

    async def end_session_url(self, post_logout_redirect_uri: str) -> str | None:
        """Where to send the browser so the provider's session ends too.

        Args:
            post_logout_redirect_uri: Where the provider should send the user
                back (a registered value at the provider).

        Returns:
            The logout URL, or ``None`` when the provider has no such endpoint
            (logout is then local only, which is always the safe answer).
        """
        return None
