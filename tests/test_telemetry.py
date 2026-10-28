from datetime import datetime, timezone

from src.telemetry import summarize_task


def record(**changes):
    return dict(status='succeeded', created_at='2026-10-23T10:00:00Z',
                started_at='2026-10-23T10:00:03Z', finished_at='2026-10-23T10:00:10Z', **changes)


def usage(role, call_id=None, **changes):
    return dict(type='model_usage', role=role, call_id=call_id,
                usage=dict(input_tokens=10, output_tokens=5, total_tokens=15,
                           input_token_details={'cache_read': 4}, output_token_details={'reasoning': 2}), **changes)


def test_all_agents_totals_exclude_detail_subsets_and_duplicate_call_records():
    events = []
    for role in ('main', 'analyst', 'report_designer'):
        events += [dict(type='model_started', role=role, call_id=role), usage(role, role), usage(role, role)]
    events += [dict(type='agent_assigned', role='main', target='analyst', created_at='stamp')]
    result = summarize_task(record(), events)
    assert (result['input_tokens'], result['output_tokens'], result['total_tokens']) == (30, 15, 45)
    assert result['usage_status'] == 'complete' and result['model_calls'] == 3
    assert result['elapsed_seconds'] == 10 and result['queue_seconds'] == 3 and result['execution_seconds'] == 7
    assert result['routes'][0]['target'] == 'analyst'
    assert all(item['total_tokens'] == 15 for item in result['by_agent'])


def test_missing_partial_and_explicit_zero_usage_are_distinct():
    assert summarize_task(record(), [])['total_tokens'] is None
    events = [dict(type='model_started', role='main', call_id='one'), usage('main', 'one'),
              dict(type='model_started', role='main', call_id='two')]
    result = summarize_task(record(), events)
    assert result['usage_status'] == 'partial' and result['total_tokens'] == 15
    zero = usage('main', 'one')
    zero['usage'] = dict(input_tokens=0, output_tokens=0, total_tokens=0)
    result = summarize_task(record(), events[:1] + [zero])
    assert result['usage_status'] == 'complete' and result['total_tokens'] == 0


def test_running_clock_and_old_terminal_tasks_stop_at_recorded_finish():
    running = record()
    running.update(status='running', finished_at=None)
    now = datetime(2026, 10, 23, 10, 0, 20, tzinfo=timezone.utc)
    assert summarize_task(running, [], now=now)['elapsed_seconds'] == 20
    running.update(status='cancelled', updated_at='2026-10-23T10:00:12Z')
    assert summarize_task(running, [], now=now)['elapsed_seconds'] == 12
    old = record()
    old['started_at'] = None
    summary = summarize_task(old, [], now=now)
    assert summary['elapsed_seconds'] == 10 and summary['queue_seconds'] is None
    assert summarize_task(record(), [usage('main', seq=1)])['usage_status'] == 'recorded'
