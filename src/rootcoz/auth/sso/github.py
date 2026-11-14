"""GitHub as an OAuth2 provider.

GitHub is not OIDC: there is no discovery document, no ID token and no JWKS. The
flow is OAuth2 plus two API calls — ``/user`` for the account, ``/user/emails``
for the address — and the *verified* flag on that address is what lets the
deployment's ``SSO_ALLOWED_DOMAINS`` rule trust it.

Two consequences worth knowing:

* The OAuth app must be granted the ``user:email`` scope. Without it GitHub
  answers ``/user/emails`` with a 403, which is a hard failure of the login
  itself (``userinfo_failed``) — not a degradation that only matters when a
  domain allow-list is set.
* GitHub has no PKCE and no end-session endpoint, so the ``code_verifier`` and
  ``nonce`` are unused here. The sealed state cookie still binds the redirect to
  this browser; the token never passes through it.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.http import request_json
from rootcoz.config import Settings

#: Everything the app is allowed to read: the profile, and the address that the
#: domain allow-list checks.
SCOPES = ("read:user", "user:email")


class GitHubProvider(SSOProvider):
    """Sign in with a GitHub account."""

    name = "github"
    display_name = "GitHub"
    # GitHub is OAuth2, not OIDC, so it names its two settings directly instead
    # of inheriting the issuer/client pair from the OIDC base.
    client_id_setting = "sso_github_client_id"
    client_secret_setting = "sso_github_client_secret"  # pragma: allowlist secret

    #: GitHub.com's endpoints. A GitHub Enterprise Server installation serves its
    #: own and is therefore not supported by this provider (documented).
    authorize_endpoint: ClassVar[str] = "https://github.com/login/oauth/authorize"
    token_endpoint: ClassVar[str] = "https://github.com/login/oauth/access_token"
    user_endpoint: ClassVar[str] = "https://api.github.com/user"
    emails_endpoint: ClassVar[str] = "https://api.github.com/user/emails"

    def __init__(self, client_id: str, client_secret: str) -> None:
        """Store the OAuth App credentials.

        Args:
            client_id: OAuth App client id.
            client_secret: OAuth App client secret.
        """
        self._client_id = client_id
        self._client_secret = client_secret

    @classmethod
    def from_settings(cls, settings: Settings) -> GitHubProvider | None:
        """Build the provider when the deployment configured it.

        Args:
            settings: Application settings for this request.

        Returns:
            The provider, or ``None`` when either credential is missing.
        """
        client_id = settings.sso_github_client_id
        secret = settings.sso_github_client_secret
        client_secret = secret.get_secret_value() if secret is not None else ""
        if not client_id or not client_secret:
            return None
        return cls(client_id=client_id, client_secret=client_secret)

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str = "",
        nonce: str = "",
    ) -> str:
        """Build the authorize URL.

        Args:
            redirect_uri: Callback URL registered with the OAuth App.
            state: CSRF nonce for this attempt.
            code_challenge: Ignored — GitHub supports neither PKCE nor OIDC.
            nonce: Ignored, for the same reason.

        Returns:
            The URL to redirect the browser to.
        """
        query = httpx.QueryParams(
            {
                # GitHub has no PKCE, so no code_challenge is sent; encoding the
                # whole query through one helper keeps the client id quoted too.
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                "allow_signup": "true",
            }
        )
        return f"{self.authorize_endpoint}?{query}"

    async def exchange(
        self, *, code: str, code_verifier: str = "", redirect_uri: str, nonce: str = ""
    ) -> ClaimSet:
        """Exchange the code for the account and its verified address.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: Ignored — GitHub has no PKCE.
            redirect_uri: The same callback URL used to start the login.
            nonce: Ignored — GitHub issues no ID token.

        Returns:
            The normalised claims.

        Raises:
            AuthError: When the token exchange, the profile read or the address
                lookup fails, or the account has no stable id.
        """
        token = await request_json(
            "POST",
            self.token_endpoint,
            reason="exchange_failed",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("exchange_failed", "GitHub returned no access token")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        user = await request_json(
            "GET", self.user_endpoint, reason="userinfo_failed", headers=headers
        )
        subject = user.get("id")
        if not isinstance(subject, int | str) or str(subject) == "":
            raise AuthError("no_subject", "GitHub returned no account id")
        login = user.get("login")
        email, verified = await self._primary_email(headers, fallback=user.get("email"))
        claims: dict[str, Any] = {
            "sub": str(subject),
            "login": login if isinstance(login, str) else "",
            "email": email,
            # Only a verified address may satisfy an allow-list, and the absence
            # of one is an absence, not a pass.
            "email_verified": verified,
        }
        return ClaimSet(
            subject=str(subject),
            username_hint=login if isinstance(login, str) else "",
            # Only a verified address is worth keeping on the identity: an
            # unverified profile address is one its holder can change at will.
            # The raw value stays in `claims` for the audit trail.
            email=email if verified else None,
            claims=claims,
        )

    async def _primary_email(
        self, headers: dict[str, str], *, fallback: Any
    ) -> tuple[str | None, bool]:
        """Find the address GitHub will vouch for.

        Args:
            headers: Authorized request headers.
            fallback: ``user.email``, used only when the emails API is silent.

        Returns:
            ``(email, verified)`` — the primary verified address when there is
            one, else any verified address, else the profile address unverified.

        Raises:
            AuthError: When the emails endpoint itself fails.
        """
        # `/user/emails` answers with an array, not an object: ask for the shape
        # the endpoint actually returns, or every GitHub login fails here.
        rows = await request_json(
            "GET",
            self.emails_endpoint,
            reason="userinfo_failed",
            expect="array",
            headers=headers,
        )
        verified = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("verified") is True
            and row.get("email")
        ]
        primary = next((row for row in verified if row.get("primary") is True), None)
        chosen = primary or (verified[0] if verified else None)
        if chosen is not None:
            return str(chosen["email"]), True
        # No verified address: hand back whatever the profile carries, unverified,
        # so the domain rule refuses it rather than passing it silently.
        if isinstance(fallback, str) and fallback:
            return fallback, False
        return None, False
