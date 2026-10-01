"""Guards for the shared host environment allowlist used by tests (#300)."""

from rootcoz.main import _SENSITIVE_SETTINGS
from tests.conftest import HOST_ENV_ALLOWLIST, host_env


def test_host_env_allowlist_excludes_every_credential():
    """Adding a credential to Settings must not silently leak into tests.

    The suite hands the app under test ``host_env()``. A credential in the real
    environment is only excluded because it is absent from the allowlist, so a
    credential added to ``_SENSITIVE_SETTINGS`` needs no action here to stay out.
    This fails if one is ever added to the allowlist by mistake.
    """
    leaked = {name.upper() for name in _SENSITIVE_SETTINGS} & set(HOST_ENV_ALLOWLIST)
    assert not leaked, f"credentials must not be in the host env allowlist: {leaked}"


def test_host_env_carries_no_credential():
    """The environment handed to the app must not contain a sensitive variable."""
    sensitive = {name.upper() for name in _SENSITIVE_SETTINGS}
    assert not sensitive & set(host_env())


def test_host_env_applies_overrides():
    """Overrides win, and absent host variables are simply omitted."""
    env = host_env(SOME_TEST_VAR="value")
    assert env["SOME_TEST_VAR"] == "value"
    assert "GITHUB_TOKEN" not in env
