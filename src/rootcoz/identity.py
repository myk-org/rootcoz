"""Server-side identities that are not settings.

This module is deliberately dependency-free. ``rootcoz.config`` needs the
AI system identity while the ``Settings`` model class body is being built,
and at that point importing ``rootcoz.storage`` closes an import cycle
(``storage`` -> ``comment_enrichment`` -> ``jira`` -> ``config``). Keeping
the constant in a leaf module makes it reachable from both sides.

``rootcoz.storage`` re-exports it, so ``from rootcoz.storage import
AI_SYSTEM_USERNAME`` stays the supported import everywhere else.
"""

from __future__ import annotations

#: Reserved identity for every AI-originated action (auto-review,
#: classification, auto-generated waivers). Blocked from user
#: registration; stored as ``created_by`` on rows the AI writes.
AI_SYSTEM_USERNAME = "rootcoz-ai"
