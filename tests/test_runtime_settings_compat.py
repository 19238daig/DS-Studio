"""Error sanitization stays safe for workers holding pre-upgrade settings."""
from types import SimpleNamespace

from unittest.mock import Mock

import pytest
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


def test_temperature_setting_parses_off_as_omitted_and_defaults_to_deterministic():
    settings = Settings(_env_file=None)
    assert settings.main_temperature == settings.analyst_temperature == settings.report_temperature == 0.1
    hosted = Settings(_env_file=None, report_temperature='off', main_temperature='none', analyst_temperature='  ')
    assert hosted.report_temperature is None and hosted.main_temperature is None
    assert hosted.analyst_temperature is None
    assert Settings(_env_file=None, report_temperature='0.3').report_temperature == 0.3


def test_build_model_omits_temperature_when_disabled():
    from src.llm import build_model
    settings = Settings(_env_file=None, report_temperature='off', api_key='sk-test')
    assert build_model(settings, 'report_designer').temperature is None
    assert build_model(settings, 'analyst').temperature == 0.1


def test_friendly_failure_maps_provider_dumps_and_keeps_business_text():
    from src.agents import friendly_failure
    raw = ("Error code: 400 - {'error': {'message': \"litellm.BadRequestError: OpenAIException - "
           "<400> InternalError.Algo.InvalidParameter: Parameter 'temperature'=0.1\"}}")
    mapped = friendly_failure(raw)
    assert mapped.startswith('模型服务调用失败')
    assert 'litellm' not in mapped and 'InternalError' not in mapped
    business = '回答含未绑定数值：14.48；统计数值必须改用{{metric:analysis_id:metric_key}}占位符。'
    assert friendly_failure(business) == business


def test_transient_model_failure_is_retried_with_escalating_backoff(monkeypatch):
    from src import agents
    monkeypatch.setattr(agents.time, 'sleep', lambda seconds: sleeps.append(seconds))
    sleeps = []
    runtime = Mock()
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) <= 2:
            raise RuntimeError('Error code: 429 - rate limit exceeded')
        return 'ok'

    assert agents._model_call_with_retry(runtime, 'main', 'c1', flaky, 'req') == 'ok'
    assert len(calls) == 3 and sleeps == [2, 8]
    retry_events = [c for c in runtime.event.call_args_list if c[0][0] == 'model_retry']
    assert len(retry_events) == 2

    def broken(request):
        calls.append(request)
        raise RuntimeError('工具参数校验失败')

    with pytest.raises(RuntimeError):
        agents._model_call_with_retry(runtime, 'main', 'c2', broken, 'req')
    assert len(calls) == 4  # non-transient failures are never retried

    def hopeless(request):
        calls.append(request)
        raise RuntimeError('Connection error.')

    with pytest.raises(RuntimeError):
        agents._model_call_with_retry(runtime, 'main', 'c3', hopeless, 'req')
    assert len(calls) == 7  # 1 + 3 retries, then the failure surfaces
