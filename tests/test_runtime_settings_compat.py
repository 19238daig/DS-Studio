"""Error sanitization stays safe for workers holding pre-upgrade settings."""
from types import SimpleNamespace

from pydantic import SecretStr

from src.agents import redact
from src.config import Settings


def test_redact_preserves_error_with_legacy_settings_and_masks_existing_key():
    old = SimpleNamespace(api_key=SecretStr('legacy-secret'))
    assert redact('ValueError: original error legacy-secret', SimpleNamespace(settings=old)) == 'ValueError: original error [REDACTED]'


def test_redact_masks_all_current_secrets_without_losing_original_error():
    settings = Settings(_env_file=None, api_key='api-test-secret', db_password='db-test-secret',
                        db_admin_password='admin-test-secret', db_restricted_password='restricted-test-secret')
    result = redact('original error: api-test-secret db-test-secret admin-test-secret restricted-test-secret',
                    SimpleNamespace(settings=settings))
    assert result == 'original error: [REDACTED] [REDACTED] [REDACTED] [REDACTED]'
