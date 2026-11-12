"""OpenShift as an OAuth2 provider.

A cluster's built-in OAuth server is **not** an OIDC provider: it publishes
``/.well-known/oauth-authorization-server`` (OAuth 2.0 metadata, no JWKS, no
``userinfo_endpoint``) and issues opaque tokens rather than ID tokens. Driving it
through the shared OIDC flow therefore failed at discovery — every login was
refused before the browser left the page.

So this provider speaks the OAuth2 flow the cluster actually offers, the way the
GitHub provider does: authorization code → access token → the cluster's own user
API, which is authenticated by that token. **Two origins are involved and they are
not interchangeable.** The OAuth server (``SSO_OPENSHIFT_ISSUER``) serves only
``/oauth/*``; the ``user.openshift.io`` endpoints that name the account live on the
cluster API server (``SSO_OPENSHIFT_API_URL``, ``https://api.<cluster>:6443``), so
reading the user from the OAuth origin fails every login.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.http import request_json
from rootcoz.config import Settings, issuer_is_secure

#: Scope that lets the cluster answer with the caller's own identity.
SCOPES: tuple[str, ...] = ("user:info",)

#: Paths on the cluster API server: the signed-in account, and one identity object
#: per name in that account's ``identities`` list.
_USER_PATH = "/apis/user.openshift.io/v1/users/~"
_IDENTITY_PATH = "/apis/user.openshift.io/v1/identities/{name}"


def _identity_email(identity: Mapping[str, Any]) -> str | None:
    """The address an identity object carries, when it carries one.

    An OpenShift identity records the provider's claims under ``extra`` (an OIDC
    claim mapping puts ``email`` there, as a list of values), and some clusters
    also keep a top-level ``email``. A hand-curated mapping has neither, which is
    normal — only ``SSO_ALLOWED_DOMAINS`` cares.

    Args:
        identity: One identity object from the cluster API.

    Returns:
        The address, or ``None`` when the object carries none.
    """
    candidates: list[Any] = [identity.get("email")]
    extra = identity.get("extra")
    if isinstance(extra, Mapping):
        claim = extra.get("email")
        candidates.extend(claim if isinstance(claim, list) else [claim])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


class OpenShiftProvider(SSOProvider):
    """Sign in with a cluster OpenShift account.

    The account's address is read from its identity objects, which a cluster
    admin (or the IdP mapping the admin configured) writes: the end user cannot
    set it. It is therefore authoritative, and an allow-list accepts it without
    a verification claim the cluster never publishes.
    """

    name = "openshift"
    display_name = "OpenShift"
    issuer_setting = "sso_openshift_issuer"
    api_url_setting = "sso_openshift_api_url"
    client_id_setting = "sso_openshift_client_id"
    # A settings field name, not a credential: detect-secrets matches the keyword.
    client_secret_setting = "sso_openshift_client_secret"  # pragma: allowlist secret
    # An OpenShift OAuth server publishes no `email_verified` claim, so there is
    # nothing to assert here — the address still counts, because an identity
    # object is written by the cluster admin, never by the end user.
    asserts_email_verified = False
    email_is_authoritative = True

    def __init__(
        self, client_id: str, client_secret: str, issuer: str, api_url: str
    ) -> None:
        """Store the OAuth client and the cluster's two origins.

        Args:
            client_id: OAuth client name registered on the cluster.
            client_secret: Its secret.
            issuer: Origin of the cluster OAuth server, e.g.
                ``https://oauth-openshift.apps.<cluster>.example.com``. Only
                ``/oauth/*`` is served there.
            api_url: Origin of the cluster API server, e.g.
                ``https://api.<cluster>.example.com:6443``, which serves the
                ``user.openshift.io`` endpoints the account is read from.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._issuer = issuer.rstrip("/")
        self._api_url = api_url.rstrip("/")

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenShiftProvider | None:
        """Build the provider when the deployment configured it fully.

        Args:
            settings: Application settings for this request.

        Returns:
            The provider, or ``None`` when the issuer, the API URL or either
            credential is missing, or one of the two URLs is not https (loopback
            excepted).
        """
        client_id = (settings.sso_openshift_client_id or "").strip()
        secret = settings.sso_openshift_client_secret
        client_secret = secret.get_secret_value() if secret is not None else ""
        issuer = (settings.sso_openshift_issuer or "").strip()
        api_url = (settings.sso_openshift_api_url or "").strip()
        if not client_id or not client_secret or not issuer or not api_url:
            return None
        if not issuer_is_secure(issuer) or not issuer_is_secure(api_url):
            return None
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            issuer=issuer,
            api_url=api_url,
        )

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
            The authorization URL, on the OAuth server (the only origin that
            serves ``/oauth/authorize``).
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

        The account is read from the cluster API server: the OAuth server that
        issued the token serves no user endpoint.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: Ignored — no PKCE.
            redirect_uri: The same callback URL used to start the login.
            nonce: Ignored — no ID token.

        Returns:
            The normalised claims. The subject is the account's immutable ``uid``
            — the name can be reassigned to a different person — with the name
            kept only as a username hint, and the account's email when the
            cluster records one.

        Raises:
            AuthError: When the exchange or the identity read fails, or the
                cluster returns no usable account object or no ``uid``.
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
            f"{self._api_url}{_USER_PATH}",
            reason="userinfo_failed",
            headers=headers,
        )
        metadata = user.get("metadata")
        if not isinstance(metadata, Mapping):
            raise AuthError("no_subject", "the cluster returned no account metadata")
        subject = str(metadata.get("uid") or "").strip()
        if not subject:
            raise AuthError("no_subject", "the cluster returned no account id")
        email = await self._email(user.get("identities"), headers)
        return ClaimSet(
            subject=subject,
            username_hint=str(metadata.get("name") or "").strip(),
            email=email,
            claims={"user": user},
            asserts_email_verified=self.asserts_email_verified,
            email_is_authoritative=self.email_is_authoritative,
        )

    async def _email(self, names: Any, headers: dict[str, str]) -> str | None:
        """Read the address the cluster records for an account, when it has one.

        The user object carries the *names* of its identities, and the address
        lives on those identity objects; an admin may have configured none — so a
        missing or unreadable identity, or one without an address, is normal and
        only matters to `SSO_ALLOWED_DOMAINS`.

        Args:
            names: The ``identities`` value from the user object.
            headers: Authorization headers for the cluster API.

        Returns:
            The first email recorded for the account, or ``None``.
        """
        if not isinstance(names, list):
            return None
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            try:
                identity = await request_json(
                    "GET",
                    f"{self._api_url}{_IDENTITY_PATH.format(name=name)}",
                    reason="userinfo_failed",
                    headers=headers,
                )
            except AuthError:
                continue
            email = _identity_email(identity)
            if email:
                return email
        return None
