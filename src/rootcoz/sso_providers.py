"""The SSO provider names, in one place (`#197`).

This is a leaf module on purpose. ``storage`` validates a provider name against
this tuple when it writes an identity or a ``created_via``, and ``auth.sso``
declares one class per name — the two used to keep separate hand-edited lists,
which meant adding a provider was two edits that could disagree. Importing either
from a third module keeps the dependency acyclic: ``auth.sso`` imports ``storage``,
so ``storage`` cannot import it back.
"""

from __future__ import annotations

#: Provider names, in the order the login page offers them. A provider is offered
#: only when its settings are complete, so this is a ceiling, not a promise.
SSO_PROVIDERS: tuple[str, ...] = ("google", "github", "openshift", "redhat")
