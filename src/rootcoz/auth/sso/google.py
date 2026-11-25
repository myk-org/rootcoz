"""Google as an OpenID Connect provider."""

from __future__ import annotations

from rootcoz.auth.sso.oidc import OIDCProvider
from rootcoz.url_utils import origin_of


class GoogleProvider(OIDCProvider):
    """Sign in with a Google account.

    Google publishes its discovery document under a fixed issuer, so the only
    deployment-specific configuration is the OAuth client pair.
    """

    name = "google"
    display_name = "Google"
    issuer = "https://accounts.google.com"
    client_id_setting = "sso_google_client_id"
    # A settings field name, not a credential: detect-secrets matches the keyword.
    client_secret_setting = "sso_google_client_secret"  # pragma: allowlist secret

    #: Google's documented endpoint origins — token exchange, JWKS and
    #: userinfo each live on a different host.
    _GOOGLE_ENDPOINT_ORIGINS: frozenset[str] = frozenset(
        {
            "https://accounts.google.com",
            "https://oauth2.googleapis.com",
            "https://www.googleapis.com",
            "https://openidconnect.googleapis.com",
        }
    )

    def trusted_endpoint_origins(self) -> frozenset[tuple[str, str, int]]:
        """Google distributes endpoints across several ``googleapis.com`` hosts.

        Returns:
            The origins Google's discovery document may legitimately point at.
        """
        return frozenset(
            o
            for url in self._GOOGLE_ENDPOINT_ORIGINS
            if (o := origin_of(url)) is not None
        )

    def accepted_issuers(self) -> frozenset[str]:
        """Google's two documented ``iss`` forms: with and without a scheme.

        Google documents the bare ``accounts.google.com`` as equally valid in
        its ID tokens, so a correctly signed token carrying it must not be
        refused. The discovery pin stays on the configured issuer: discovery and
        the JWKS are fetched from ``https://accounts.google.com``, and only the
        claim varies.

        Returns:
            The configured issuer plus the bare host form.
        """
        return super().accepted_issuers() | {"accounts.google.com"}
