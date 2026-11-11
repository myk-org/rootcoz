"""SSO provider package: protocol per provider, one registry.

Adding a provider takes three edits, not one: a module (usually a few class
attributes on :class:`~rootcoz.auth.sso.oidc.OIDCProvider`), an entry in
:data:`PROVIDER_CLASSES`, and (only if it is genuinely new) its name in
``rootcoz.sso_providers.SSO_PROVIDERS``, which ``storage`` validates against and
which this package asserts at import. A name declared without a class — or a class
without a declared name — fails immediately rather than at the first login.
"""

from __future__ import annotations

from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.github import GitHubProvider
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.auth.sso.openshift import OpenShiftProvider
from rootcoz.auth.sso.redhat import RedHatSSOProvider
from rootcoz.config import Settings
from rootcoz.sso_providers import SSO_PROVIDERS as SSO_PROVIDER_NAMES

#: Every known provider, in the order the login page offers them.
PROVIDER_CLASSES: tuple[type[SSOProvider], ...] = (
    GoogleProvider,
    GitHubProvider,
    OpenShiftProvider,
    RedHatSSOProvider,
)

#: Lookup by machine name, for the routes (`/api/auth/sso/{provider}/...`).
PROVIDER_BY_NAME: dict[str, type[SSOProvider]] = {
    provider.name: provider for provider in PROVIDER_CLASSES
}

#: The names are declared once (``rootcoz.sso_providers``) and ``storage``
#: validates against the same tuple. A class for a name nobody declared, or a
#: declared name with no class, would fail at the first login instead of here —
#: so the two are compared at import.
_declared = set(SSO_PROVIDER_NAMES)
if set(PROVIDER_BY_NAME) != _declared:
    raise RuntimeError(
        "SSO provider classes and declared names disagree:"
        f" classes={sorted(PROVIDER_BY_NAME)} declared={sorted(_declared)}"
    )


def configured_providers(settings: Settings) -> list[SSOProvider]:
    """The providers this deployment can actually offer.

    A provider with no client id or secret is simply absent: the login page must
    never show a button that cannot work. The same holds for a deployment with no
    A provider here is *configured*, which is a different question from whether a
    browser login can complete: that also needs ``PUBLIC_BASE_URL``, and the
    providers endpoint reports it separately so a device-only deployment (where
    the CLI joins a relative verification URL itself) keeps working.

    Args:
        settings: Application settings for this request.

    Returns:
        Configured providers, in declaration order.
    """
    providers = []
    for provider_class in PROVIDER_CLASSES:
        provider = provider_class.from_settings(settings)
        if provider is not None:
            providers.append(provider)
    return providers


def get_provider(name: str, settings: Settings) -> SSOProvider | None:
    """Resolve a provider by name, only when it is configured.

    Args:
        name: Machine name from the URL.
        settings: Application settings for this request.

    Returns:
        The provider, or ``None`` when unknown or not configured (both answer the
        same way, so the caller cannot enumerate configured providers).
    """
    provider_class = PROVIDER_BY_NAME.get(name)
    if provider_class is None:
        return None
    return provider_class.from_settings(settings)


__all__ = [
    "PROVIDER_BY_NAME",
    "PROVIDER_CLASSES",
    "AuthError",
    "ClaimSet",
    "SSOProvider",
    "configured_providers",
    "get_provider",
]
