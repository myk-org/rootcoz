"""OpenShift as an OAuth2 provider.

A cluster's built-in OAuth server is **not** an OIDC provider: it publishes
``/.well-known/oauth-authorization-server`` (OAuth 2.0 metadata, no JWKS, no
``userinfo_endpoint``) and issues opaque tokens rather than ID tokens. Driving it
through the shared OIDC flow therefore failed at discovery — every login was
refused before the browser left the page.

So this provider speaks the OAuth2 flow the cluster actually offers, the way the
GitHub provider does: authorization code → access token → the cluster's own user
API, which is authenticated by that token. The issuer comes from the deployment
(`SSO_OPENSHIFT_ISSUER`) because it is the cluster's OAuth server origin.
"""

from __future__ import annotations

import httpx

from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.http import request_json
from rootcoz.config import Settings, issuer_is_secure

#: Scope that lets the cluster answer with the caller's own identity.
SCOPES: tuple[str, ...] = ("user:info",)

#: The cluster API that names the account the token belongs to.
_USER_PATH = "/apis/user.openshift.io/v1/users/~"
_IDENTITIES_PATH = "/apis/user.openshift.io/v1/users/{name}/identities"


class OpenShiftProvider(SSOProvider):
    """Sign in with a cluster OpenShift account."""

    name = "openshift"
    display_name = "OpenShift"
    issuer_setting = "sso_openshift_issuer"
    client_id_setting = "sso_openshift_client_id"
    # A settings field name, not a credential: detect-secrets matches the keyword.
    client_secret_setting = "sso_openshift_client_secret"  # pragma: allowlist secret
    # An OpenShift OAuth server publishes no `email_verified` claim, so the domain
    # rule can only check the address itself there.
    asserts_email_verified = False

    def __init__(self, client_id: str, client_secret: str, issuer: str) -> None:
        """Store the OAuth client and the cluster's OAuth server origin.

        Args:
            client_id: OAuth client name registered on the cluster.
            client_secret: Its secret.
            issuer: Origin of the cluster OAuth server, e.g.
                ``https://oauth-openshift.apps.<cluster>.example.com``.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._issuer = issuer.rstrip("/")

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenShiftProvider | None:
        """Build the provider when the deployment configured it fully.

        Args:
            settings: Application settings for this request.

        Returns:
            The provider, or ``None`` when the issuer or either credential is
            missing, or the issuer is not https (loopback excepted).
        """
        client_id = (settings.sso_openshift_client_id or "").strip()
        secret = settings.sso_openshift_client_secret
        client_secret = secret.get_secret_value() if secret is not None else ""
        issuer = (settings.sso_openshift_issuer or "").strip()
        if not client_id or not client_secret or not issuer:
            return None
        if not issuer_is_secure(issuer):
            return None
        return cls(client_id=client_id, client_secret=client_secret, issuer=issuer)

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
            redirect_uri: Callback URL registered on the cluster's OAuth client.
            state: CSRF nonce for this attempt.
            code_challenge: Ignored — the cluster's OAuth server has no PKCE.
            nonce: Ignored — it issues no ID token to bind.

        Returns:
            The authorization URL.
        """
        query = httpx.QueryParams(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
            }
        )
        return f"{self._issuer}/oauth/authorize?{query}"

    async def exchange(
        self, *, code: str, code_verifier: str = "", redirect_uri: str, nonce: str = ""
    ) -> ClaimSet:
        """Exchange the code for the cluster account it belongs to.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: Ignored — no PKCE.
            redirect_uri: The same callback URL used to start the login.
            nonce: Ignored — no ID token.

        Returns:
            The normalised claims, with the account's email when the cluster
            exposes one.

        Raises:
            AuthError: When the exchange or the identity read fails, or the
                cluster returns no account name.
        """
        token = await request_json(
            "POST",
            f"{self._issuer}/oauth/token",
            reason="exchange_failed",
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("exchange_failed", "no access token in the response")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        user = await request_json(
            "GET",
            f"{self._issuer}{_USER_PATH}",
            reason="userinfo_failed",
            headers=headers,
        )
        name = str((user.get("metadata") or {}).get("name") or "").strip()
        if not name:
            raise AuthError("no_subject", "the cluster returned no account name")
        email = await self._email(name, headers)
        return ClaimSet(
            subject=name,
            username_hint=name,
            email=email,
            claims={"user": user},
            asserts_email_verified=False,
        )

    async def _email(self, name: str, headers: dict[str, str]) -> str | None:
        """Read the address the cluster records for an account, when it has one.

        OpenShift keeps identity emails on the identities API rather than on the
        user object, and an admin may have configured none — so a missing address
        is normal and only matters to `SSO_ALLOWED_DOMAINS`.

        Args:
            name: The account name from the user API.
            headers: Authorization headers for the cluster API.

        Returns:
            The first email recorded for the account, or ``None``.
        """
        try:
            identities = await request_json(
                "GET",
                f"{self._issuer}{_IDENTITIES_PATH.format(name=name)}",
                reason="userinfo_failed",
                headers=headers,
                expect="object",
            )
        except AuthError:
            return None
        for item in identities.get("items") or []:
            email = str(item.get("email") or "").strip()
            if email:
                return email
        return None
