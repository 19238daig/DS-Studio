"""Single source for secret redaction in any text bound for users or streams.

A newly added secret Settings field must be registered in SECRET_FIELDS here;
the per-module wrappers keep their historical call sites and limits.
"""
from __future__ import annotations

import re
from typing import Any

SECRET_FIELDS = ('api_key', 'db_password', 'db_admin_password', 'db_restricted_password')
SK_PATTERN = re.compile(r'sk-[A-Za-z0-9_-]{8,}')


def redact_text(value: Any, settings: Any = None, limit: int = 4000) -> str:
    """Mask configured secrets and key-like strings, then clamp the length."""
    text = str(value or '')
    for name in SECRET_FIELDS:
        configured = getattr(settings, name, None)
        secret = configured.get_secret_value() if hasattr(configured, 'get_secret_value') else ''
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return SK_PATTERN.sub('[REDACTED]', text)[:limit]
