"""OpenShift as an OpenID Connect provider.

The issuer is the cluster's own OAuth server, so it comes from the deployment's
settings (`SSO_OPENSHIFT_ISSUER`) rather than from code — everything else is the
shared OIDC flow.
"""

from __future__ import annotations

from rootcoz.auth.sso.oidc import OIDCProvider


class OpenShiftProvider(OIDCProvider):
    """Sign in with a cluster OpenShift account."""

    name = "openshift"
    display_name = "OpenShift"
    issuer_setting = "sso_openshift_issuer"
    # An OpenShift OAuth server publishes no `email_verified` claim, so the
    # domain rule can only check the address itself there.
    asserts_email_verified = False
    client_id_setting = "sso_openshift_client_id"
    # A settings field name, not a credential: detect-secrets matches the keyword.
    client_secret_setting = "sso_openshift_client_secret"  # pragma: allowlist secret
