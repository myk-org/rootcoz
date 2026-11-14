"""The authentication chain: which strategies run, in what order."""

from __future__ import annotations

from fastapi import Request

from rootcoz.auth.api_key import ApiKeyStrategy
from rootcoz.auth.base import AuthContext, AuthIdentity, AuthStrategy
from rootcoz.auth.proxy import ProxyStrategy
from rootcoz.auth.session import SessionStrategy
from rootcoz.config import Settings

#: The strategies, in the order they are probed. Which of them may take over
#: from an earlier one is decided by :func:`may_replace`.
AUTH_CHAIN: tuple[type[AuthStrategy], ...] = (
    SessionStrategy,
    ApiKeyStrategy,
    ProxyStrategy,
)

_names = [strategy.name for strategy in AUTH_CHAIN]
if len(set(_names)) != len(_names):
    raise RuntimeError(f"AUTH_CHAIN has duplicate strategy names: {_names}")


def build_chain() -> tuple[AuthStrategy, ...]:
    """Instantiate the chain for a single request.

    Built per request rather than cached: rootcoz reloads settings every
    request, and keeping construction in the request path means no strategy can
    ever start holding settings from an earlier one. Strategies read them from
    :class:`~rootcoz.auth.base.AuthContext` at call time regardless.

    Returns:
        A fresh, ordered tuple of strategies.
    """
    return tuple(strategy() for strategy in AUTH_CHAIN)


def may_replace(identity: AuthIdentity | None, strategy: AuthStrategy) -> bool:
    """Whether ``strategy`` is allowed to replace an identity already resolved.

    The one place the override relation is stated. It is preserved from the
    pre-refactor middleware, where a Bearer branch guarded by
    ``if not authenticated_admin`` replaced whatever the session cookie had
    established: a Bearer credential may replace a non-admin session (CLI and
    AI callers send both, and the key names whose authority the request
    carries), and an admin session is final so an administrator's session is
    never downgraded to a lesser identity.

    Args:
        identity: The identity resolved so far, or ``None``.
        strategy: The strategy about to be probed.

    Returns:
        True when ``strategy`` may establish the identity for this request.
    """
    if identity is None:
        return True
    if identity.is_admin:
        return False
    return (identity.strategy_name, strategy.name) in (
        (SessionStrategy.name, ApiKeyStrategy.name),
    )


async def resolve_identity(request: Request, settings: Settings) -> AuthIdentity | None:
    """Resolve the caller through the ordered chain.

    :func:`may_replace` decides whether a later strategy may take over what an
    earlier one resolved: a non-admin session yields to a Bearer key, an admin
    session is final, and the trusted-proxy header only applies when nothing
    else authenticated the request.

    A strategy may have written to ``request.state`` while resolving —
    ``renew_session_token`` after a session renewal, ``set_proxy_cookie`` for a
    proxy identity — and the caller applies those to the response. A strategy
    that returns ``None`` never clears what an earlier strategy resolved.

    Args:
        request: The incoming request.
        settings: Settings for this request.

    Returns:
        The resolved identity, or ``None`` when no strategy authenticated it.
    """
    ctx = AuthContext(request=request, settings=settings)
    current: AuthIdentity | None = None
    for strategy in build_chain():
        if not may_replace(current, strategy):
            continue
        if not strategy.is_applicable(ctx):
            continue
        identity = await strategy.authenticate(ctx)
        if identity is None:
            # A credential that did not match never clears what already resolved.
            continue
        current = identity
    return current
