"""SSO provider package: protocol per provider, one registry.

Adding a provider takes three edits, not one: a module (usually a few class
attributes on :class:`~rootcoz.auth.sso.oidc.OIDCProvider`), an entry in
:data:`PROVIDER_CLASSES`, and the provider name in ``storage.SSO_PROVIDERS``
(which ``VALID_CREATED_VIA`` reuses). A name missing from storage is refused at
the first link or account creation, so the routes are the only thing that stays
untouched.
"""

from __future__ import annotations

from rootcoz import storage
from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.config import Settings

#: Every known provider, in the order the login page offers them.
PROVIDER_CLASSES: tuple[type[SSOProvider], ...] = (GoogleProvider,)

#: Lookup by machine name, for the routes (`/api/auth/sso/{provider}/...`).
PROVIDER_BY_NAME: dict[str, type[SSOProvider]] = {
    provider.name: provider for provider in PROVIDER_CLASSES
}

#: A provider name is stored in ``user_identities``/``users.created_via``, so
#: storage validates it against its own list. A class missing there would fail at
#: the first link instead of here, so the two are checked at import.
_unknown = {name for name in PROVIDER_BY_NAME} - set(storage.SSO_PROVIDERS)
if _unknown:
    raise RuntimeError(
        f"SSO providers missing from storage.SSO_PROVIDERS: {sorted(_unknown)}"
    )


def configured_providers(settings: Settings) -> list[SSOProvider]:
    """The providers this deployment can actually offer.

    A provider with no client id or secret is simply absent: the login page must
    never show a button that cannot work.

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
