"""Red Hat SSO (Keycloak) as an OpenID Connect provider.

Keycloak serves discovery under the realm issuer, so the deployment supplies it
(`SSO_REDHAT_ISSUER`) and the shared OIDC flow does the rest.
"""

from __future__ import annotations

from rootcoz.auth.sso.oidc import OIDCProvider


class RedHatSSOProvider(OIDCProvider):
    """Sign in with a Red Hat SSO realm account."""

    name = "redhat"
    display_name = "Red Hat SSO"
    issuer_setting = "sso_redhat_issuer"
    client_id_setting = "sso_redhat_client_id"
    # A settings field name, not a credential: detect-secrets matches the keyword.
    client_secret_setting = "sso_redhat_client_secret"  # pragma: allowlist secret
