"""Google as an OpenID Connect provider."""

from __future__ import annotations

from rootcoz.auth.sso.oidc import OIDCProvider


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
