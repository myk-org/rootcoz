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
        asserts_email_verified: Whether this provider publishes an
            ``email_verified`` claim at all. Google and GitHub do; an OpenShift
            OAuth server does not.
        email_is_authoritative: Whether the address came from a store only the
            deployment's IdP or cluster admin can write (see
            :attr:`SSOProvider.email_is_authoritative`).
    """

    subject: str
    username_hint: str
    email: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    asserts_email_verified: bool = True
    email_is_authoritative: bool = False


class SSOProvider(ABC):
    """One external identity provider.

    Class attributes:
        name: Machine name (``google``), used in URLs, identities and audit rows.
        display_name: Button label.
        settings_fields: The Settings field names this provider reads, in
            display order. Server Settings, the sensitive-value list and the
            restart-required list are all derived from these — a provider that
            declares them needs no edit in ``main.py``.
        sensitive_settings: The subset of ``settings_fields`` that holds a
            secret. Values are masked in the settings API and never returned.
        restart_required_settings: The subset that needs a restart to take
            effect. Every built-in provider is runtime-configurable, so this is
            empty by default — but a future provider that binds a socket at
            startup can say so without touching the settings metadata.
        can_provision_user: Whether a login through this provider may create an
            account. True for every interactive provider: the approval gate, not
            the provider, decides when the account may act.
    """

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    settings_fields: ClassVar[tuple[str, ...]] = ()
    sensitive_settings: ClassVar[tuple[str, ...]] = ()
    restart_required_settings: ClassVar[tuple[str, ...]] = ()
    can_provision_user: ClassVar[bool] = True
    #: True when this provider's address comes from a store only the deployment's
    #: IdP or cluster admin can write, so it is accepted for
    #: ``SSO_ALLOWED_DOMAINS`` without a verification claim.
    email_is_authoritative: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Fill in ``settings_fields``/``sensitive_settings`` for a new provider.

        A provider already names its Settings fields (``client_id_setting`` and
        friends); deriving the declared lists here means Server Settings, the
        sensitive-value list and the restart list are all computed from that one
        description. A provider that wants to be explicit can still set them.
        """
        super().__init_subclass__(**kwargs)
        if "settings_fields" not in cls.__dict__:
            declared = tuple(
                getattr(cls, attribute, "")
                for attribute in (
                    # The order the settings rows have always appeared in.
                    "client_id_setting",
                    "client_secret_setting",
                    "issuer_setting",
                    # A provider spanning two origins (OpenShift: OAuth server and
                    # API server) names its second URL here, after the issuer.
                    "api_url_setting",
                )
            )
            cls.settings_fields = tuple(name for name in declared if name)
        if "sensitive_settings" not in cls.__dict__:
            secret = getattr(cls, "client_secret_setting", "")
            cls.sensitive_settings = (secret,) if secret else ()

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
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str = "",
        nonce: str = "",
    ) -> str:
        """Where to send the browser to start the login.

        Args:
            redirect_uri: Callback URL registered with the provider.
            state: CSRF nonce for this attempt.
            code_challenge: PKCE challenge derived from the verifier. A provider
                whose protocol has no PKCE (GitHub) ignores it.
            nonce: OIDC nonce, echoed into the ID token and checked on return —
                the OAuth ``state`` binds the redirect, the nonce binds the
                token itself. A non-OIDC provider ignores it.

        Returns:
            The provider's authorization URL.
        """

    @abstractmethod
    async def exchange(
        self, *, code: str, code_verifier: str = "", redirect_uri: str, nonce: str = ""
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
