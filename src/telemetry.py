"""Read-only per-question summaries rebuilt from the durable event journal."""
from datetime import datetime, timezone

ROLES = {'main': '主 Agent', 'analyst': '数据分析 Agent', 'report_designer': '报告生成 Agent'}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted', 'waiting_user'}


def _date(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (ValueError, TypeError, AttributeError):
        return None


def summarize_task(task: dict, events: list[dict], *, now=None) -> dict:
    """Count provider totals once; cache/reasoning details are subsets, not extras."""
    terminal = task.get('status') in TERMINAL
    end = _date(task.get('finished_at'))
    if terminal and end is None:
        end = next((_date(e.get('created_at')) for e in reversed(events)
                    if e.get('type') in TERMINAL | {'cancel_requested'}), None)
        end = end or _date(task.get('updated_at'))
    end = end or (now or datetime.now(timezone.utc))
    created, started = _date(task.get('created_at')), _date(task.get('started_at'))
    seconds = lambda start, stop: round(max(0, (stop - start).total_seconds()), 3) if start and stop else None
    totals = dict(input_tokens=0, output_tokens=0, total_tokens=0)
    by_role, routes, seen_usage, calls, reported, failed_calls = {}, [], set(), set(), set(), set()
    samples, legacy = 0, False
    for event in events:
        role, kind, call_id = event.get('role'), event.get('type'), event.get('call_id')
        if role in ROLES:
            by_role.setdefault(role, dict(role=role, **dict.fromkeys(totals, 0), usage_samples=0))
        if kind == 'agent_assigned' and event.get('target') in ROLES:
            routes.append({k: event.get(k) for k in ('created_at', 'role', 'target')})
        if kind == 'model_started' and call_id:
            calls.add(call_id)
        if kind == 'model_failed' and call_id:
            failed_calls.add(call_id)
        if kind != 'model_usage':
            continue
        usage = event.get('usage') or {}
        # Missing counters are unknown. An explicitly returned zero is valid.
        if not all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0 for k in totals):
            continue
        identity = (call_id, event.get('usage_index', 0)) if call_id else ('legacy', event.get('seq', id(event)))
        if identity in seen_usage:
            continue
        seen_usage.add(identity)
        samples += 1
        if call_id:
            reported.add(call_id)
        else:
            legacy = True
        for key in totals:
            totals[key] += usage[key]
            if role in by_role:
                by_role[role][key] += usage[key]
        if role in by_role:
            by_role[role]['usage_samples'] += 1
    missing = len(calls - reported)
    coverage = ('partial' if missing or failed_calls else 'recorded' if legacy else 'complete') if samples else 'unavailable'
    return dict(elapsed_seconds=seconds(created, end), execution_seconds=seconds(started, end),
                queue_seconds=seconds(created, started) if started else seconds(created, end) if task.get('status') == 'queued' else None,
                finished=terminal,
                **{key: value if samples else None for key, value in totals.items()},
                usage_status=coverage, usage_samples=samples, model_calls=len(calls),
                unreported_calls=missing, agents=list(by_role), by_agent=list(by_role.values()), routes=routes)
