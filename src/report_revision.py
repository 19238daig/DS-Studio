"""General, artifact-bound report revisions; no templates or executable model code.

Stable slots describe presentation independently of immutable analysis lineage.
New analysis evidence is appended only when a patch explicitly uses it.
"""
from __future__ import annotations

import hashlib
import json

from src.storage import StoreError


def clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def initial_state(store, analyses, layout, overrides=()):
    by_chart = {item['chart_id']: item for item in overrides}
    state = {'charts': [], 'tables': []}
    for analysis in analyses:
        for kind, ids, key in [('chart', analysis.chart_ids, 'charts'), ('table', analysis.table_ids, 'tables')]:
            for aid in ids:
                if any(slot['component_id'] == aid for slot in state[key]):
                    continue  # Existing report rendering deduplicates shared artifacts.
                override = by_chart.get(aid) if kind == 'chart' else None
                current = override['artifact_id'] if override else aid
                state[key].append(dict(component_id=aid, artifact_id=current,
                    analysis_id=analysis.analysis_id, hash=store.get_artifact(current).hash,
                    visible=bool(layout['include_charts' if kind == 'chart' else 'include_tables'])))
    return state


def validate_state(store, task, analyses, state, overrides=()):
    if not isinstance(state, dict) or set(state) != {'charts', 'tables'}:
        raise StoreError('Report component state is invalid', 422, 'invalid_report_base')
    indexed = {a.analysis_id: a for a in analyses}
    by_chart = {o['chart_id']: o for o in overrides}
    required = {'component_id', 'artifact_id', 'analysis_id', 'hash', 'visible'}
    for kind, key in [('chart', 'charts'), ('table', 'tables')]:
        entries = state[key]
        if not isinstance(entries, list) or len(entries) > 500:
            raise StoreError('Report component state exceeds limits', 422, 'invalid_report_base')
        seen = set()
        active_artifacts = set()
        original = {aid for a in analyses for aid in (a.chart_ids if kind == 'chart' else a.table_ids)}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != required or not isinstance(entry['visible'], bool):
                raise StoreError('Report component state is invalid', 422, 'invalid_report_base')
            if entry['component_id'] in seen or entry['component_id'] not in original:
                raise StoreError('Report component identity is invalid', 422, 'invalid_report_base')
            seen.add(entry['component_id'])
            if entry['artifact_id'] in active_artifacts:
                raise StoreError('Duplicate report component artifact', 422, 'invalid_report_edit')
            active_artifacts.add(entry['artifact_id'])
            owner = indexed.get(entry['analysis_id'])
            artifact = store.get_artifact(entry['artifact_id'])
            if owner is None or artifact.session_id != task.session_id or artifact.hash != entry['hash']:
                raise StoreError('Report component provenance mismatch', 422, 'invalid_report_base')
            direct = entry['artifact_id'] in (owner.chart_ids if kind == 'chart' else owner.table_ids)
            override = by_chart.get(entry['component_id']) if kind == 'chart' else None
            derived = override and override['artifact_id'] == entry['artifact_id'] and override['analysis_id'] == owner.analysis_id
            if not (direct or derived) or (direct and (artifact.task_id != owner.task_id or artifact.kind != kind)):
                raise StoreError('Report component is not a registered analysis result', 422, 'invalid_report_base')
            source = store.get_task(owner.task_id)
            # A failed report task can still have a completed, explicitly
            # recovered analysis. _analyses authorizes and validates that
            # immutable evidence; the report itself must be delivered.
            if source['session_id'] != task.session_id:
                raise StoreError('Report component analysis is outside this session', 422, 'analysis_not_authorized')
            store.resolve_artifact(entry['artifact_id'])
    return clone(state)


def apply_components(store, task, analyses, state, edits):
    state = clone(state)
    indexed = {a.analysis_id: a for a in analyses}
    changes = []
    seen = set()
    for edit in edits:
        key = 'charts' if edit.kind == 'chart' else 'tables'
        entries = state[key]
        identity = (key, edit.target_id or edit.artifact_id)
        if identity in seen:
            raise StoreError('A component may only be edited once per patch', 422, 'invalid_report_edit')
        seen.add(identity)
        if edit.operation == 'add':
            if edit.target_id is not None:
                raise StoreError('Adding a component must not name an old target', 422, 'invalid_report_edit')
            old = None
        else:
            matches = [entry for entry in entries if edit.target_id in {entry['component_id'], entry['artifact_id']}]
            if len(matches) != 1:
                raise StoreError('Component target is not unique in the base report', 422, 'component_not_found')
            old = matches[0]
        if edit.operation in {'add', 'replace'}:
            owner = indexed.get(edit.analysis_id)
            if owner is not None and edit.kind == 'chart' and edit.artifact_id in owner.table_ids:
                raise StoreError('该artifact_id是CSV结果表，不是图表。先request_analysis生成所需SVG；仅用新分析chart_ids中的真实图表ID添加，不要把table_ids作为chart。',
                                 422, 'chart_not_generated')
            if owner is None or edit.artifact_id not in (owner.chart_ids if edit.kind == 'chart' else owner.table_ids):
                raise StoreError('Replacement must belong to an authorized analysis', 422, 'artifact_not_authorized')
            artifact = store.get_artifact(edit.artifact_id)
            source = store.get_task(owner.task_id)
            if (artifact.session_id != task.session_id or artifact.task_id != owner.task_id or artifact.kind != edit.kind or
                    (source['status'] != 'succeeded' and owner.task_id != task.task_id)):
                raise StoreError('Replacement analysis is not successfully delivered', 422, 'analysis_not_authorized')
            store.resolve_artifact(artifact.artifact_id)
            updated = dict(component_id=old['component_id'] if old else artifact.artifact_id,
                artifact_id=artifact.artifact_id, analysis_id=owner.analysis_id, hash=artifact.hash,
                visible=edit.visible if edit.visible is not None else (old['visible'] if old else True))
            if not old and any(e['component_id'] == updated['component_id'] for e in entries):
                raise StoreError('Component already exists; use replace or move', 422, 'invalid_report_edit')
            if old:
                index = entries.index(old)
                entries[index] = updated
            else:
                entries.append(updated)
            if edit.position is not None:
                entries.remove(updated)
                if edit.position > len(entries):
                    raise StoreError('Component position exceeds report bounds', 422, 'invalid_report_edit')
                entries.insert(edit.position, updated)
        else:
            if edit.artifact_id is not None or edit.analysis_id is not None or edit.visible is not None:
                raise StoreError('Remove/move must not supply replacement data', 422, 'invalid_report_edit')
            if edit.operation == 'remove':
                if edit.position is not None:
                    raise StoreError('Remove must not supply a position', 422, 'invalid_report_edit')
                entries.remove(old)
            else:
                if edit.position is None or edit.position >= len(entries):
                    raise StoreError('Move requires an existing position', 422, 'invalid_report_edit')
                entries.remove(old)
                entries.insert(edit.position, old)
        changes.append(dict(kind=edit.kind, operation=edit.operation,
            target_id=old['component_id'] if old else edit.artifact_id,
            artifact_id=edit.artifact_id, analysis_id=edit.analysis_id))
    return state, changes


def explicit_analysis_ids(patch, narrative):
    """Return only IDs actually referenced by targeted presentation changes."""
    import re
    ids = {edit.analysis_id for edit in patch.components if edit.analysis_id} if patch else set()
    documents = [patch.model_dump(exclude_unset=True) if patch else {}, narrative or {}]
    for document in documents:
        def walk(value):
            if isinstance(value, dict):
                if value.get('analysis_id'):
                    ids.add(value['analysis_id'])
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                ids.update(re.findall(r'\{\{metric:([A-Za-z0-9_-]+):[^{}:]+\}\}', value))
        walk(document)
    return ids
