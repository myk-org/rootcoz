"""Server Settings metadata is derived from the providers, not hand-listed (#197 Stage 5).

Adding a provider is one module plus a registry entry: its settings rows, its
masked secret and any restart requirement must follow from that declaration. These
tests are the contract — a provider that forgets to declare a field, or a metadata
list that drifts from the registry, fails here instead of leaking a secret into
the settings API.
"""

from __future__ import annotations

import pytest

from rootcoz.auth.sso import PROVIDER_CLASSES
from rootcoz.auth.sso.base import SSOProvider
from rootcoz.config import Settings
from rootcoz.main import (
    _RESTART_REQUIRED_SETTINGS,
    _SENSITIVE_SETTINGS,
    _SETTINGS_CATEGORIES,
)


def test_every_declared_provider_setting_exists() -> None:
    """A typo in a provider's field name would silently never render."""
    fields = Settings.model_fields
    for provider in PROVIDER_CLASSES:
        for name in provider.settings_fields:
            assert name in fields, f"{provider.name} declares unknown setting {name}"


def test_every_provider_setting_is_offered_in_server_settings() -> None:
    """The SSO category is the only place an admin can set these."""
    category = _SETTINGS_CATEGORIES["SSO"]
    for provider in PROVIDER_CLASSES:
        for name in provider.settings_fields:
            assert name in category, f"{name} missing from the SSO category"


def test_every_declared_secret_is_masked() -> None:
    """A secret that is not in the sensitive set is returned in plain text."""
    for provider in PROVIDER_CLASSES:
        for name in provider.sensitive_settings:
            assert name in provider.settings_fields
            assert name in _SENSITIVE_SETTINGS, f"{name} is not masked"


def test_every_provider_secret_is_declared() -> None:
    """The reverse direction: no provider secret is masked by luck alone."""
    declared = {n for p in PROVIDER_CLASSES for n in p.sensitive_settings}
    for provider in PROVIDER_CLASSES:
        secret = getattr(provider, "client_secret_setting", "")
        if secret:
            assert secret in declared, f"{provider.name} does not declare its secret"
            assert secret in _SENSITIVE_SETTINGS


def test_restart_requirements_come_from_the_providers() -> None:
    """Empty for every built-in provider, and still connected to the registry."""
    declared = {n for p in PROVIDER_CLASSES for n in p.restart_required_settings}
    assert declared <= _RESTART_REQUIRED_SETTINGS


def test_a_new_provider_inherits_the_derivation() -> None:
    """The reason this exists: a fifth provider needs no edit in main.py."""

    class FakeProvider(SSOProvider):
        """A provider that only names its settings, like a real one would."""

        name = "fake"
        display_name = "Fake"
        client_id_setting = "sso_fake_client_id"
        client_secret_setting = "sso_fake_client_secret"  # pragma: allowlist secret

        @classmethod
        def from_settings(cls, settings: Settings):
            return None

        async def authorization_url(self, **kwargs):
            return ""

        async def exchange(self, **kwargs):
            raise NotImplementedError

    assert FakeProvider.settings_fields == (
        "sso_fake_client_id",
        "sso_fake_client_secret",
    )
    assert FakeProvider.sensitive_settings == ("sso_fake_client_secret",)
    assert FakeProvider.can_provision_user is True


@pytest.mark.parametrize("provider", PROVIDER_CLASSES, ids=lambda p: p.name)
def test_every_provider_can_provision_a_user(provider: type[SSOProvider]) -> None:
    """Interactive providers create accounts; the approval gate decides access."""
    assert provider.can_provision_user is True
