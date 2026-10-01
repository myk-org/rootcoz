"""Shared rootcoz issue attribution: one footer, one server-owned provenance.

Every rootcoz-created issue ends with exactly one attribution footer naming
the AI provider/model that generated the content, or stating that no model
did.  The footer is built from *preview-time* provenance, not from whatever
the server is configured with when the issue is finally created.

Because the preview body round-trips through the browser, provenance travels
inside it as an HMAC-signed token (server secret = the Fernet key secret).
The create step re-verifies the signature, so a client can neither change the
credited model nor forge one, and any *rootcoz* attribution line it adds anywhere
in the body is stripped before the single server-owned footer is appended.  Prose
that merely resembles a footer — a user's own feedback quoting another tool's
"Generated using AI by ..." — is content, not a claim, and survives untouched
(issue #301).

**Who decides the attribution.**  Only the caller does, and only through an
explicit ``AiProvenance``: :func:`read_provenance` returns a token's provenance
when the token verifies, and :func:`apply_ai_attribution` / the shared issue
creator apply exactly one footer from it.  Body text never decides anything —
pattern-matching it is exactly how a client-supplied line used to suppress the
server's footer and get published as its own claim.

**Why the token is bound to the content.**  The signed payload carries a digest
of the body *excluding* the attribution region, so the token describes the issue
it was minted for.  A user may edit the attribution region itself (that is the
only region the footer and token occupy) and the credit survives; any other
body — a pasted token from someone else's AI preview or from an already
published issue, attached to unrelated text — fails the digest check and is
credited to no model.  The price is deliberate: edits *outside* the attribution
region change the content and therefore lose the model credit, because nothing
the server can verify still says who wrote them.

One body class is exempt from that price.  The strip was tightened to
rootcoz's own markers (issue #301), which moves a *foreign* lookalike from the
ignored attribution region into the digested content, so a token minted before
the change no longer matches the body it describes.  :func:`read_provenance`
therefore also accepts the digest computed under the old, looser strip
(:data:`_LEGACY_ATTRIBUTION_RE`).  That widens which *digest* a token may carry,
never whether its HMAC is genuine, and it accepts only digests this code already
accepted — so the set of verifiable bodies is exactly today's, not larger.

**Line endings never decide anything.**  A client posts text from a browser, a
CLI on Windows or a pasted file, so the same content arrives with LF, CRLF or
lone-CR separators.  Stripping therefore matches any line break
(:data:`ATTRIBUTION_RE` / :data:`_PROVENANCE_RE`), and the digest normalizes
*only* CRLF/CR to LF before hashing (:func:`_content_digest`) so a preview
round-trips through a CRLF round-trip unchanged while a *changed* body still
fails the digest check.  Every other character — including the Unicode line
separators ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, U+2028 and U+2029 — is left
exactly as it is, deliberately: were they folded to LF, a client could swap one
for a plain newline and the digest would still verify, keeping the model credit
on a body whose text changed.  ``str.splitlines`` and friends fold all of
them, so they are never used here.
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from rootcoz.ai_client import public_provider_name
from rootcoz.encryption import get_hmac_secret

# Attribution footer for issues submitted as raw feedback (no model wrote them).
NO_AI_ATTRIBUTION = (
    "\n\n---\n*No AI model generated this issue — submitted as raw feedback "
    "via [rootcoz](https://github.com/myk-org/rootcoz)*"
)

# Shared "Generated using AI with [rootcoz](...)" footer text (optionally
# followed by " (provider / model)").  The bare form is bug_creation's
# legacy footer; the model-specific form comes from a resolved AI pair.
AI_ATTRIBUTION_PREFIX = (
    "Generated using AI with [rootcoz](https://github.com/myk-org/rootcoz)"
)

# Matches a *rootcoz* attribution line wherever it appears in a body (not only
# a trailing one), so a client-supplied fake rootcoz footer cannot survive
# creation.  Placement stays unanchored for exactly that reason; the *text* is
# anchored, because the bare shape ("---" then an italic line starting "Generated
# using AI") also matches a user's own prose quoting another tool's footer, and
# stripping that deletes their words from the preview and from the issue created
# from it (issue #301).  Every footer rootcoz itself writes carries the project
# link — except the no-AI fallback sentence, which carries the literal "No AI
# model generated this issue" — so keying on those two covers all three footers
# and no foreign one.  The separator/line breaks are any run of CR/LF, never a
# bare LF: a CRLF (or lone-CR) footer must not slip past an LF-shaped pattern
# and get published.
ATTRIBUTION_RE = re.compile(
    r"[\r\n]*---[\r\n]*\*[^\r\n]*(?:github\.com/myk-org/rootcoz|No AI model generated this issue)[^\r\n]*\*"
)

# The pre-#301 pattern: any italic "Generated using AI"/"No AI model generated"
# line, no matter whose footer it really is.  Retained deliberately as a
# read_provenance digest fallback for tokens minted before the anchor, so an
# already-published AI issue does not silently lose its credit to the no-AI
# fallback.  Never used to strip what gets published — that is ATTRIBUTION_RE
# alone; keeping the two in one module is what stops the shim from creeping
# back into the strip path.
_LEGACY_ATTRIBUTION_RE = re.compile(
    r"[\r\n]*---[\r\n]*\*(?:Generated using AI|No AI model generated)[^\r\n]*\*"
)

_PROVENANCE_PREFIX = "<!--rootcoz-ai:"
# One line only, under either line-ending style, so a CRLF-embedded token is
# stripped exactly like an LF one.
_PROVENANCE_RE = re.compile(r"<!--rootcoz-ai:[^\r\n]*-->")

# Genuine line endings only — CRLF and lone CR to LF.  Deliberately NOT
# str.splitlines()/re.split(r"\s+"), which also break on \v, \f, \x1c-\x1e,
# U+2028 and U+2029 and would let a client swap those for a newline without
# invalidating the signature (see _content_digest).
_LINE_ENDING_RE = re.compile(r"\r\n?")


@dataclass(frozen=True, slots=True)
class AiProvenance:
    """Preview-time AI provenance for an issue body.

    ``ai_used`` is False when no model generated the content (fallback
    template or hand-written), regardless of the current AI configuration.
    """

    ai_used: bool
    provider: str = ""
    model: str = ""


def _pair(provenance: AiProvenance) -> tuple[str, str]:
    """Return the creditable (provider, model) pair — empty when AI was unused.

    The provider is the catalog's internal ID; the public API only ever exposes
    ``claude``/``gemini``/``cursor``, so it is mapped through ai_client's one
    provider table before it reaches a footer or a signed payload.
    """
    if not provenance.ai_used:
        return "", ""
    return public_provider_name(provenance.provider.strip()), provenance.model.strip()


def _sign(payload: str) -> str:
    """Return the HMAC-SHA256 hex digest of *payload* (server secret)."""
    return hmac.new(
        get_hmac_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _strip_attribution(body: str, pattern: re.Pattern[str] = ATTRIBUTION_RE) -> str:
    """Return *body* without any attribution footer or provenance token.

    *pattern* exists so the legacy-digest fallback reuses this one strip (see
    :data:`_LEGACY_ATTRIBUTION_RE`) instead of duplicating it; publishing always
    takes the default.
    """
    return _PROVENANCE_RE.sub("", pattern.sub("", body)).rstrip()


def _content_digest(body: str, pattern: re.Pattern[str] = ATTRIBUTION_RE) -> str:
    """Digest the content a token describes: *body* minus its attribution.

    ONLY CRLF and lone CR are normalized (to LF), so the digest depends on the
    text and not on the client's line-ending style — a CRLF round-trip of the
    same body still verifies, a changed body still does not.  No other
    character is touched: ``\v``, ``\f``, ``\x1c``-``\x1e``, U+2028 and U+2029
    are *not* line breaks here even though ``str.splitlines`` treats them as
    such, because folding them into LF would let a client replace one with a
    plain newline and keep the model credit on a body whose text changed.
    """
    return hashlib.sha256(
        _LINE_ENDING_RE.sub("\n", _strip_attribution(body, pattern)).encode()
    ).hexdigest()


def _encode(body: str, provenance: AiProvenance) -> str:
    """Render *provenance* as a token signed over the body it credits."""
    ai_used, provider, model = (
        provenance.ai_used,
        *_pair(provenance),
    )
    # The digest is inside the signed payload: the signature covers the content
    # binding, so a token cannot be re-pointed at another body.
    payload = json.dumps(
        [ai_used, provider, model, _content_digest(body)], separators=(",", ":")
    )
    return f"{_PROVENANCE_PREFIX}{_sign(payload)}:{payload}-->"


def read_provenance(body: str) -> AiProvenance | None:
    """Return the server-signed provenance carried by *body*, or ``None``.

    Unsigned, tampered or content-mismatched tokens are ignored: the caller
    must fall back to a safe (no-AI) attribution rather than trust client text.
    """
    digest = _content_digest(body)
    for match in _PROVENANCE_RE.finditer(body):
        token = match.group(0)[len(_PROVENANCE_PREFIX) : -len("-->")]
        signature, _, payload = token.partition(":")
        if not hmac.compare_digest(signature, _sign(payload)):
            continue
        try:
            parts = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(parts, list) or len(parts) != 4:
            continue
        ai_used, provider, model, token_digest = parts
        if not hmac.compare_digest(str(token_digest), digest):
            # Token predates the anchored strip (#301): accept the digest the old
            # pattern produced for this body.  Signature is already verified, so
            # this widens the accepted digest, not the attacker's reach.
            # ponytail: one extra regex pass + sha256, only on the rare fallback
            # path — drop this branch once pre-#301 tokens can no longer exist.
            legacy = _content_digest(body, _LEGACY_ATTRIBUTION_RE)
            if not hmac.compare_digest(str(token_digest), legacy):
                continue
        return AiProvenance(bool(ai_used), str(provider), str(model))
    return None


def build_ai_attribution(provenance: AiProvenance) -> str:
    """Return the footer for *provenance*: the AI pair, plain AI, or no AI."""
    if not provenance.ai_used:
        return NO_AI_ATTRIBUTION
    provider, model = _pair(provenance)
    if provider and model:
        return f"\n\n---\n*{AI_ATTRIBUTION_PREFIX} ({provider} / {model})*"
    return f"\n\n---\n*{AI_ATTRIBUTION_PREFIX}*"


def apply_ai_attribution(body: str, provenance: AiProvenance) -> str:
    """Strip every attribution line from *body* and append exactly one footer.

    The result carries one visible footer plus a signed provenance token bound
    to this exact content, so the create step re-verifies the model instead of
    re-resolving it — and cannot credit a different body.
    """
    stripped = _strip_attribution(body)
    return (
        stripped + build_ai_attribution(provenance) + "\n" + _encode(body, provenance)
    )
