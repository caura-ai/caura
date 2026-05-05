"""Shared security constants used by the OSS settings validator and
the enterprise auth-enforcement layer (CAURA-652).

Single source of truth so a future widening only touches one place.
The OSS settings validator caps ``security.session_idle_timeout_minutes``
to this range; the enterprise platform-auth-api applies the same bound
when it computes the idle window.
"""

from __future__ import annotations

# ── Session idle timeout (CAURA-652) ──
# Inclusive minute bounds applied at two boundaries: the org-settings
# PUT validator (this OSS service) and the enterprise auth middleware.
SESSION_IDLE_TIMEOUT_MIN_MINUTES = 5
SESSION_IDLE_TIMEOUT_MAX_MINUTES = 120

# Used by ``ResolvedConfig.session_idle_timeout_minutes`` when an org
# hasn't overridden the value. 30 min matches the OWASP idle-session
# guidance for medium-sensitivity admin consoles.
SESSION_IDLE_TIMEOUT_DEFAULT_MINUTES = 30
