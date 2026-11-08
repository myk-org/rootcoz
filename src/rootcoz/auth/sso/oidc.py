"""OIDC implementation shared by every OpenID Connect provider.

Discovery, the authorization URL, the token exchange and the ``end_session``
lookup are the same protocol for Google, OpenShift and Red Hat SSO; only the
issuer and the client credentials differ, so a provider module is a few class
attributes on top of this base.

Two rules keep this safe:

* The **issuer is a constant in code**, never a request parameter — an attacker
  who could point discovery at their own server would be minting identities.
* The ID token is verified against the issuer's JWKS — signature pinned to
  RS256, then ``iss``, ``aud``, ``exp``/``nbf``/``iat`` with a small clock skew —
  before any claim is trusted. The userinfo endpoint is only a fallback for
  providers that return no ``id_token``.
* The OAuth ``state`` binds the *redirect* to this browser attempt; the OIDC
  ``nonce`` binds the *token* to it. A token that does not echo the attempt's
  nonce is refused, so a token minted for one login cannot be replayed into
  another.
"""

from __future__ import annotations

import hmac
import time
from typing import Any, ClassVar

import httpx

from rootcoz import get_child_logger
from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.config import Settings

logger = get_child_logger(__name__)

#: Scopes every provider is asked for. ``openid`` is what makes it OIDC.
SCOPES = ("openid", "email", "profile")

#: How long discovered metadata and JWKS are reused before a refetch. The cache
#: is process-wide and keyed by issuer: providers are built per request, so an
#: instance-level cache would refetch twice per login for documents that change
#: about never.
METADATA_TTL_SECONDS = 3600

#: issuer → (fetched_at, discovery document)
_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

#: jwks_uri → (fetched_at, key-set document)
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _reset_discovery_cache() -> None:
    """Clear the process-wide discovery/JWKS cache (tests, and after a reset)."""
    _METADATA_CACHE.clear()
    _JWKS_CACHE.clear()


#: Ceiling for any call to the provider, so a hung IdP cannot hold a request.
HTTP_TIMEOUT_SECONDS = 10.0

#: Tolerance for clock drift between rootcoz and the provider, applied to
#: ``exp``/``nbf``/``iat``: without it a good token can be refused because two
#: machines disagree by a second.
CLOCK_SKEW_SECONDS = 60

#: The only signature algorithm accepted. Pinning it is what makes an ``alg:
#: none`` or HMAC-confusion token a refusal rather than a decision left to the
#: token's own header.
ID_TOKEN_ALGORITHMS = ("RS256",)


class OIDCProvider(SSOProvider):
    """An OpenID Connect provider, configured from settings.

    Class attributes:
        issuer: The provider's issuer URL when it is fixed in code (Google).
        issuer_setting: Settings field naming the issuer, for providers a
            deployment installs itself (OpenShift, Red Hat SSO). Never taken
            from a request — only from the deployment's own configuration.
        client_id_setting: Settings field holding the client id.
        client_secret_setting: Settings field holding the client secret.
    """

    #: Class default for providers whose issuer is fixed in code; ``__init__``
    #: sets it per instance (OpenShift/Red Hat SSO read it from settings).
    issuer: str = ""
    issuer_setting: ClassVar[str] = ""
    client_id_setting: ClassVar[str] = ""
    client_secret_setting: ClassVar[str] = ""

    def __init__(self, client_id: str, client_secret: str, issuer: str) -> None:
        """Store the client credentials and the issuer they belong to.

        Args:
            client_id: OAuth client id registered at the provider.
            client_secret: OAuth client secret for that client.
            issuer: Issuer URL, from code or from the deployment's settings.
        """
        self.issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret

    @classmethod
    def from_settings(cls, settings: Settings) -> OIDCProvider | None:
        """Build the provider when both credentials are configured.

        Args:
            settings: Application settings for this request.

        Returns:
            The provider, or ``None`` when either credential is missing.
        """
        client_id = getattr(settings, cls.client_id_setting, "") or ""
        secret = getattr(settings, cls.client_secret_setting, None)
        client_secret = secret.get_secret_value() if secret is not None else ""
        if not client_id or not client_secret:
            return None
        issuer = cls.issuer
        if cls.issuer_setting:
            configured = (getattr(settings, cls.issuer_setting, "") or "").strip()
            if not configured.startswith(("https://", "http://")):
                return None
            issuer = configured
        if not issuer:
            return None
        return cls(client_id=client_id, client_secret=client_secret, issuer=issuer)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        reason: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call the provider once and return its JSON object.

        One place for the timeout, the status check, the decode and the
        ``AuthError`` translation, so every provider call fails the same way.

        Args:
            method: HTTP method.
            url: Absolute URL.
            reason: Reason code to report when the call fails.
            **kwargs: Passed to ``httpx`` (``data``, ``headers``, ...).

        Returns:
            The decoded JSON object.

        Raises:
            AuthError: On a transport error, a non-2xx status, or a body that is
                not a JSON object.
        """
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError(reason, f"{reason}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AuthError(reason, f"{reason}: response is not an object")
        return payload

    async def metadata(self) -> dict[str, Any]:
        """Fetch (and cache) the provider's discovery document.

        Returns:
            The discovery document.

        Raises:
            AuthError: When discovery fails or is missing a required endpoint.
        """
        now = time.monotonic()
        cached = _METADATA_CACHE.get(self.issuer)
        if cached is not None and now - cached[0] < METADATA_TTL_SECONDS:
            return cached[1]
        document = await self._request_json(
            "GET",
            f"{self.issuer}/.well-known/openid-configuration",
            reason="discovery_failed",
        )
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not document.get(required):
                raise AuthError(
                    "discovery_failed", f"discovery document has no {required}"
                )
        _METADATA_CACHE[self.issuer] = (now, document)
        return document

    async def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str, nonce: str
    ) -> str:
        """Build the authorization URL.

        Args:
            redirect_uri: Callback URL registered with the provider.
            state: CSRF nonce for this attempt.
            code_challenge: PKCE S256 challenge.
            nonce: OIDC nonce echoed into the ID token.

        Returns:
            The URL to redirect the browser to.

        Raises:
            AuthError: When discovery fails.
        """
        document = await self.metadata()
        query = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        endpoint = str(document["authorization_endpoint"])
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{httpx.QueryParams(query)}"

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str
    ) -> ClaimSet:
        """Exchange an authorization code for claims.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: The PKCE verifier issued with the state.
            redirect_uri: The same callback URL used to start the login.
            nonce: The nonce sent with the authorization request.

        Returns:
            The normalised claims.

        Raises:
            AuthError: When the exchange, verification, or claim parsing fails.
        """
        document = await self.metadata()
        token = await self._post_token(document, code, code_verifier, redirect_uri)
        claims = await self._claims_from_token(document, token, nonce)
        return self._normalise(claims)

    async def _post_token(
        self,
        document: dict[str, Any],
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """POST the code to the token endpoint.

        Args:
            document: The discovery document.
            code: Authorization code.
            code_verifier: PKCE verifier.
            redirect_uri: Callback URL used for the login.

        Returns:
            The token response.

        Raises:
            AuthError: When the provider rejects the exchange.
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        return await self._request_json(
            "POST",
            str(document["token_endpoint"]),
            reason="exchange_failed",
            data=data,
        )

    async def _claims_from_token(
        self, document: dict[str, Any], token: dict[str, Any], nonce: str
    ) -> dict[str, Any]:
        """Get claims from the ID token, falling back to userinfo.

        Args:
            document: The discovery document.
            token: The token response.
            nonce: The nonce this attempt sent; the ID token must echo it.

        Returns:
            The claim set.

        Raises:
            AuthError: When no verified claim set can be produced.
        """
        id_token = token.get("id_token")
        if isinstance(id_token, str) and id_token:
            return await self._verify_id_token(document, id_token, nonce)
        access_token = token.get("access_token")
        userinfo_endpoint = document.get("userinfo_endpoint")
        if isinstance(access_token, str) and access_token and userinfo_endpoint:
            return await self._userinfo(str(userinfo_endpoint), access_token)
        raise AuthError("exchange_failed", "token response carried no usable claims")

    async def _verify_id_token(
        self, document: dict[str, Any], id_token: str, nonce: str
    ) -> dict[str, Any]:
        """Verify an ID token against the issuer's keys.

        Args:
            document: The discovery document.
            id_token: The compact JWS from the token response.
            nonce: The nonce this attempt sent — a token that does not echo it
                belongs to a different attempt (or none) and is refused.

        Returns:
            The verified claims.

        Raises:
            AuthError: When the signature, issuer, audience or expiry is wrong.
        """
        from joserfc import jwt as jose_jwt
        from joserfc.errors import JoseError

        jwks_uri = str(document["jwks_uri"])
        try:
            decoded = jose_jwt.decode(
                id_token,
                await self._jwks_for(jwks_uri),
                algorithms=list(ID_TOKEN_ALGORITHMS),
            )
        except JoseError, ValueError, TypeError:
            # One forced refetch covers a key rotation; if the token still does
            # not verify, it is not ours.
            try:
                decoded = jose_jwt.decode(
                    id_token,
                    await self._jwks_for(jwks_uri, force_refresh=True),
                    algorithms=list(ID_TOKEN_ALGORITHMS),
                )
            except (JoseError, ValueError, TypeError) as exc:
                raise AuthError(
                    "id_token_invalid", f"ID token rejected: {exc}"
                ) from exc
        claims = dict(decoded.claims)
        # joserfc verifies the signature only; the claims are checked here, and
        # each one is a refusal the caller cannot argue with.
        now = time.time()
        expires = claims.get("exp")
        if not isinstance(expires, int | float) or expires + CLOCK_SKEW_SECONDS < now:
            raise AuthError("id_token_invalid", "ID token is expired or has no exp")
        not_before = claims.get("nbf")
        if (
            isinstance(not_before, int | float)
            and not_before - CLOCK_SKEW_SECONDS > now
        ):
            raise AuthError("id_token_invalid", "ID token is not valid yet")
        issued_at = claims.get("iat")
        if isinstance(issued_at, int | float) and issued_at - CLOCK_SKEW_SECONDS > now:
            raise AuthError("id_token_invalid", "ID token was issued in the future")
        issuer = claims.get("iss")
        if issuer not in {self.issuer, self.issuer.rstrip("/")}:
            raise AuthError("id_token_invalid", f"unexpected issuer {issuer!r}")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        if self._client_id not in audiences:
            raise AuthError("id_token_invalid", "audience does not name this client")
        if len(audiences) > 1 and claims.get("azp") != self._client_id:
            # With several audiences, OIDC requires azp to name the client the
            # token was issued to — otherwise a token minted for a sibling client
            # of the same issuer would be accepted here.
            raise AuthError("id_token_invalid", "token is authorized for another party")
        echoed = claims.get("nonce")
        if (
            not isinstance(echoed, str)
            or not nonce
            or not hmac.compare_digest(echoed.encode(), nonce.encode())
        ):
            raise AuthError("id_token_invalid", "ID token does not echo this attempt")
        return claims

    async def _jwks_for(self, jwks_uri: str, *, force_refresh: bool = False) -> Any:
        """Fetch (and cache) the issuer's key set.

        Args:
            jwks_uri: The ``jwks_uri`` from discovery.
            force_refresh: Refetch even when the cache is warm. Used when a
                signature fails to verify: the issuer may have rotated keys, and
                without this every login stays broken until the TTL expires.

        Returns:
            A key set usable by :mod:`joserfc`.

        Raises:
            AuthError: When the keys cannot be fetched.
        """
        from joserfc.jwk import KeySet

        now = time.monotonic()
        cached = _JWKS_CACHE.get(jwks_uri)
        if force_refresh or cached is None or now - cached[0] >= METADATA_TTL_SECONDS:
            document = await self._request_json("GET", jwks_uri, reason="jwks_failed")
            cached = (now, document)
            _JWKS_CACHE[jwks_uri] = cached
        return KeySet.import_key_set(cached[1])

    async def _userinfo(self, endpoint: str, access_token: str) -> dict[str, Any]:
        """Read claims from the userinfo endpoint.

        Args:
            endpoint: The ``userinfo_endpoint`` from discovery.
            access_token: Bearer token from the token response.

        Returns:
            The claim set.

        Raises:
            AuthError: When userinfo fails or is not an object.
        """
        return await self._request_json(
            "GET",
            endpoint,
            reason="userinfo_failed",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def _normalise(self, claims: dict[str, Any]) -> ClaimSet:
        """Turn provider claims into the provider-agnostic shape.

        Args:
            claims: Verified claims.

        Returns:
            The normalised claim set.

        Raises:
            AuthError: When the provider did not identify the person.
        """
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError("no_subject", "claims carried no subject")
        email = claims.get("email")
        email = email if isinstance(email, str) and email else None
        return ClaimSet(
            subject=subject,
            username_hint=username_hint(claims, email),
            email=email,
            claims=claims,
        )

    async def end_session_url(self, post_logout_redirect_uri: str) -> str | None:
        """Where to end the provider's session.

        Discovery is fetched here when the cache is cold, because logout is a
        separate request from the login that populated it.

        Args:
            post_logout_redirect_uri: Where the provider should send the user
                back (a value registered at the provider).

        Returns:
            The logout URL, or ``None`` when the provider advertises no
            ``end_session_endpoint`` or discovery fails — a local logout is then
            the safe answer.
        """
        try:
            document = await self.metadata()
        except AuthError as exc:
            logger.debug("No end-session URL for %s: %s", self.issuer, exc.reason)
            return None
        endpoint = document.get("end_session_endpoint")
        if not endpoint:
            return None
        query = httpx.QueryParams(
            {"post_logout_redirect_uri": post_logout_redirect_uri}
        )
        return f"{endpoint}?{query}"


def username_hint(claims: dict[str, Any], email: str | None) -> str:
    """Pick the best local-username hint from a claim set.

    Args:
        claims: Verified claims.
        email: Email from the claims, when present.

    Returns:
        ``preferred_username``, else the email local-part, else an empty string.
    """
    for key in ("preferred_username", "nickname"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if email:
        return email.split("@", 1)[0]
    return ""


__all__ = ["SCOPES", "OIDCProvider", "username_hint"]
