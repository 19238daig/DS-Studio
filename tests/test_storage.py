from pathlib import Path
import errno
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.schemas import AnalysisResult, TaskSpec
from src.storage import Store, StoreError


def _stamp(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def store(tmp_path):
    return Store(Settings(workspace=tmp_path, _env_file=None))


def test_persistence_authorization_and_versions(store):
    sid = store.new_session()['session_id']
    other = store.new_session()['session_id']
    path = store.root / 'input' / sid / 'a.csv'
    path.parent.mkdir(parents=True)
    path.write_text('x\n1\n')
    file = store.register_file(path, 'a.csv', sid)
    assert Store(store.settings).resolve_file(file['file_id'], sid) == path
    with pytest.raises(StoreError):
        store.resolve_file(file['file_id'], other)
    with pytest.raises(StoreError):
        store.resolve_file('../.env', sid)
    task = store.create_task(TaskSpec(session_id=sid, request='test'))
    analysis = AnalysisResult(task_id=task['task_id'], session_id=sid, input_versions={file['file_id']: file['hash']})
    store.save_analysis(analysis)
    assert store.get_analysis(analysis.analysis_id, sid).analysis_id == analysis.analysis_id
    path.write_text('x\n2\n')
    with pytest.raises(StoreError):
        store.get_analysis(analysis.analysis_id, sid)


def test_kb_refresh_invalidates_old_id(store):
    sid = store.new_session()['session_id']
    path = store.root / 'KB' / 'a.csv'
    path.write_text('x\n1\n')
    first = store.refresh_kb()[0]
    assert store.resolve_file(first['file_id'], sid) == path
    assert store.refresh_kb()[0]['file_id'] == first['file_id']
    path.write_text('x\n2\n')
    second = store.refresh_kb()[0]
    assert second['file_id'] != first['file_id']
    with pytest.raises(StoreError):
        store.get_file(first['file_id'], sid)
    path.unlink()
    assert store.refresh_kb() == []


def test_artifact_path_and_checksum(store):
    sid = store.new_session()['session_id']
    task = store.create_task(TaskSpec(session_id=sid, request='test'))
    path = store.root / 'script_output' / sid / task['task_id'] / 'answer.csv'
    path.parent.mkdir(parents=True)
    path.write_text('result\n42\n')
    artifact = store.register_artifact(path, task['task_id'], sid, 'table')
    assert store.resolve_artifact(artifact.artifact_id) == path
    with pytest.raises(StoreError):
        store.register_artifact(store.root / '.env', task['task_id'], sid, 'table')
    path.write_text('tampered')
    with pytest.raises(StoreError):
        store.resolve_artifact(artifact.artifact_id)


def test_cancel_cannot_be_overwritten(store):
    sid = store.new_session()['session_id']
    tid = store.create_task(TaskSpec(session_id=sid, request='test'))['task_id']
    store.update_task(tid, status='running')
    store.update_task(tid, cancel_requested=True)
    assert store.update_task(tid, status='succeeded')['status'] == 'cancelled'
    assert store.update_task(tid, status='succeeded')['status'] == 'cancelled'


def test_events_and_safe_cleanup(store):
    sid = store.new_session()['session_id']
    tid = store.create_task(TaskSpec(session_id=sid, request='test'))['task_id']
    store.add_event(tid, 'test', 'hello')
    assert len(store.list_events(tid, after=1)) == 1
    with pytest.raises(StoreError):
        store.delete_session(sid)
    store.update_task(tid, status='cancelled')
    store.delete_session(sid)
    with pytest.raises(StoreError):
        store.get_session(sid)


def test_clear_session_keeps_files_and_sources_but_drops_history(store):
    sid = store.new_session('待清空')['session_id']
    path = store.root / 'input' / sid / 'a.csv'
    path.parent.mkdir(parents=True)
    path.write_text('x\n1\n', encoding='utf-8')
    file = store.register_file(path, 'a.csv', sid)
    task = store.create_task(TaskSpec(session_id=sid, request='test'))
    store.update_task(task['task_id'], status='succeeded')
    analysis = AnalysisResult(task_id=task['task_id'], session_id=sid,
                              input_versions={file['file_id']: file['hash']})
    store.save_analysis(analysis)
    agent_kb = store.root / 'state' / 'indexes' / 'agent_kb' / sid
    agent_kb.mkdir(parents=True)
    (agent_kb / 'catalog.md').write_text('stale', encoding='utf-8')
    store.append_message(sid, 'user', '第一问', task_id=task['task_id'])

    running = store.create_task(TaskSpec(session_id=sid, request='running'))
    store.update_task(running['task_id'], status='running')
    with pytest.raises(StoreError) as error:
        store.clear_session(sid)
    assert error.value.code == 'task_active'
    with pytest.raises(StoreError) as error:
        store.delete_session(sid)
    assert error.value.code == 'task_active'
    store.update_task(running['task_id'], status='cancelled')

    cleared = store.clear_session(sid)
    assert cleared['messages'] == [] and cleared['tasks'] == []
    session = store.get_session(sid)
    assert session['title'] == '待清空' and session['messages'] == [] and session['tasks'] == []
    assert [f['file_id'] for f in store.list_files(sid, include_kb=False)] == [file['file_id']]
    assert path.exists()
    assert not store._index('analyses')
    assert not agent_kb.exists()
    assert not (store.root / 'state' / 'tasks' / f"{task['task_id']}.json").exists()
    store.delete_session(sid)
    with pytest.raises(StoreError):
        store.get_session(sid)
    assert not path.exists()


def test_backfill_titles_only_default_titled_sessions_with_history(store):
    from src.storage import DEFAULT_SESSION_TITLE
    named = store.new_session('手动命名')['session_id']
    store.append_message(named, 'user', '这个问题不该改标题')
    empty = store.new_session()['session_id']
    pending = store.new_session()['session_id']
    store.append_message(pending, 'assistant', '只有回复没有提问')
    legacy = store.new_session()['session_id']
    store.append_message(legacy, 'user', '分析不同年份的营收差异并生成报告')
    assert store.get_session(legacy)['title'] == DEFAULT_SESSION_TITLE

    store.backfill_session_titles()

    assert store.get_session(legacy)['title'] == '分析不同年份的营收差异并生成报告'
    assert store.get_session(named)['title'] == '手动命名'
    assert store.get_session(empty)['title'] == DEFAULT_SESSION_TITLE
    assert store.get_session(pending)['title'] == DEFAULT_SESSION_TITLE
    # A second pass is a no-op.
    store.backfill_session_titles()
    assert store.get_session(legacy)['title'] == '分析不同年份的营收差异并生成报告'


def test_clear_session_refuses_waiting_user_tasks(store):
    sid = store.new_session()['session_id']
    task = store.create_task(TaskSpec(session_id=sid, request='需要补充信息'))
    store.update_task(task['task_id'], status='waiting_user')
    with pytest.raises(StoreError) as error:
        store.clear_session(sid)
    assert error.value.code == 'waiting_clarification'
    # A session without a pending clarification clears normally.
    other = store.new_session()['session_id']
    t2 = store.create_task(TaskSpec(session_id=other, request='x'))
    store.update_task(t2['task_id'], status='succeeded')
    store.clear_session(other)


def test_rename_session_validates_and_persists(store):
    sid = store.new_session('旧名')['session_id']
    assert store.rename_session(sid, '  新名  ')['title'] == '新名'
    assert store.get_session(sid)['title'] == '新名'
    for bad in ('', '   ', 'x' * 201):
        with pytest.raises(StoreError) as error:
            store.rename_session(sid, bad)
        assert error.value.code == 'invalid_session_title'
    with pytest.raises(StoreError):
        store.rename_session('missing', '任意')


def test_add_event_seq_cache_stays_consistent_with_external_appends(store):
    sid = store.new_session()['session_id']
    task = store.create_task(TaskSpec(session_id=sid, request='t'))
    first = store.add_event(task['task_id'], 'a', 'x')
    second = store.add_event(task['task_id'], 'b', 'y')
    assert (first['seq'], second['seq']) == (2, 3)  # create_task already logged 'queued' as seq 1
    # Another process appends behind our back; the size check forces a re-scan.
    path = store.safe_path(f"state/tasks/{task['task_id']}.events.jsonl")
    with path.open('ab') as stream:
        stream.write((json.dumps({'seq': 4, 'type': 'external'}) + '\n').encode('utf-8'))
    fifth = store.add_event(task['task_id'], 'c', 'z')
    assert fifth['seq'] == 5
    assert [e['seq'] for e in store.list_events(task['task_id'])] == [1, 2, 3, 4, 5]


def test_estimate_seconds_buckets_reports_and_ignores_stale_tasks(store):
    sid = store.new_session()['session_id']

    def seed(request, seconds, report):
        task = store.create_task(TaskSpec(session_id=sid, request=request), kind='message')
        changes = {'status': 'succeeded', 'started_at': _stamp(seconds), 'finished_at': _stamp(0)}
        if report:
            changes['result'] = {'report_ids': ['r1']}
        store.update_task(task['task_id'], **changes)

    seed('报告一', 600, report=True)
    seed('报告二', 480, report=True)
    seed('快问一', 20, report=False)
    seed('快问二', 40, report=False)
    stale = store.create_task(TaskSpec(session_id=sid, request='旧报告'), kind='message')
    store.update_task(stale['task_id'], status='succeeded', created_at=_stamp(30 * 86400),
                      started_at=_stamp(30 * 86400), finished_at=_stamp(29 * 86400),
                      result={'report_ids': ['r0']})

    assert store.estimate_seconds('message', report=True) == 540
    assert store.estimate_seconds('message', report=False) == 30
    assert store.estimate_seconds('message', report=False, within_days=-1) is None  # cutoff future: everything excluded


def test_app_state_file_schema_is_trimmed_and_cached(store, monkeypatch):
    from unittest.mock import Mock

    from src.agents import TaskRuntime

    sid = store.new_session()['session_id']
    path = store.root / 'input' / sid / 'wide.csv'
    path.parent.mkdir(parents=True)
    path.write_text('x\n1\n', encoding='utf-8')
    file = store.register_file(path, 'wide.csv', sid)
    item = store._index('files')[file['file_id']]
    item['profile'] = {'sheet_or_table': 'data', 'columns': [f'c{i}' for i in range(45)],
                       'dtypes': {f'c{i}': 'float64' for i in range(45)}, 'row_count': 5,
                       'quality_warnings': ['w']}
    store._save_index('files', {**store._index('files'), file['file_id']: item})
    task = TaskSpec(session_id=sid, request='分析', file_ids=[file['file_id']])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    calls = []
    original = store.get_file
    monkeypatch.setattr(store, 'get_file', lambda fid, sid_arg: (calls.append(fid), original(fid, sid_arg))[1])
    table = runtime.progress_context()['selected_file_schemas'][0]['tables'][0]
    assert len(table['columns']) == 30 and table['columns_truncated'] is True
    assert set(table['dtypes']) == set(table['columns'])
    runtime.progress_context()
    assert len(calls) == 1  # the second ledger build is served from the cache


def test_estimate_seconds_is_median_of_recent_succeeded_message_tasks(store):
    sid = store.new_session()['session_id']
    assert store.estimate_seconds('message') is None
    for index, seconds in enumerate((200, 100, 40)):
        task = store.create_task(TaskSpec(session_id=sid, request=f'q{index}'), kind='message')
        store.update_task(task['task_id'], status='succeeded', started_at=_stamp(seconds), finished_at=_stamp(0))
    # Failed tasks and other kinds never enter the estimate.
    failed = store.create_task(TaskSpec(session_id=sid, request='x'), kind='message')
    store.update_task(failed['task_id'], status='failed', started_at=_stamp(999), finished_at=_stamp(0))
    profile = store.create_task(TaskSpec(session_id=sid, request='profile'), kind='profile')
    store.update_task(profile['task_id'], status='succeeded', started_at=_stamp(5000), finished_at=_stamp(0))
    assert store.estimate_seconds('message') == 100
    assert store.estimate_seconds('profile') == 5000


def test_profile_metadata_cannot_change_source_authorization(store):
    sid = store.new_session()['session_id']
    path = store.root / 'input' / sid / 'a.csv'
    path.parent.mkdir(parents=True)
    path.write_text('x\n1\n')
    record = store.register_file(path, 'a.csv', sid)
    with pytest.raises(StoreError):
        store.update_file_metadata(record['file_id'], sid, relative_path='.env')
    with pytest.raises(StoreError):
        store.update_file_metadata(record['file_id'], sid, profile_status='infinite')
    updated = store.update_file_metadata(record['file_id'], sid, profile_status='ready', profiles=[])
    assert updated['hash'] == record['hash']


@pytest.mark.parametrize('blocked_attempts', [1, 2])
def test_atomic_write_retries_permission_errors_without_rewriting_old_json(store, monkeypatch, blocked_attempts):
    import src.storage as storage
    store.write_json('state/worker.json', {'version': 'old'})
    target = store.root / 'state' / 'worker.json'
    previous = target.read_bytes()
    original_replace = storage.os.replace
    attempts, sleeps = [], []
    def temporary_denial(source, destination):
        assert store.lock.is_locked
        assert Path(destination) == target
        assert target.read_bytes() == previous
        assert json.loads(Path(source).read_text(encoding='utf-8')) == {'version': 'new'}
        attempts.append(Path(source))
        if len(attempts) <= blocked_attempts:
            raise PermissionError(errno.EACCES, 'simulated temporary sharing denial')
        return original_replace(source, destination)
    monkeypatch.setattr(storage.os, 'replace', temporary_denial)
    monkeypatch.setattr(storage.time, 'sleep', sleeps.append)
    store.write_json('state/worker.json', {'version': 'new'})
    assert store.read_json('state/worker.json') == {'version': 'new'}
    assert len(attempts) == blocked_attempts + 1
    assert len(set(attempts)) == 1
    assert len(sleeps) == blocked_attempts and sum(sleeps) < 1
    assert not list(target.parent.glob('worker.json.*.tmp'))


def test_atomic_write_permanent_permission_failure_preserves_old_file_and_cleans_temp(store, monkeypatch):
    import src.storage as storage
    store.write_json('state/worker.json', {'version': 'old'})
    target = store.root / 'state' / 'worker.json'
    previous = target.read_bytes()
    attempts, sleeps = [], []
    def permanent_denial(source, destination):
        attempts.append(Path(source))
        assert target.read_bytes() == previous
        raise PermissionError(errno.EACCES, 'permanent denial')
    monkeypatch.setattr(storage.os, 'replace', permanent_denial)
    monkeypatch.setattr(storage.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError, match='permanent denial'):
        store.write_json('state/worker.json', {'version': 'new'})
    assert target.read_bytes() == previous
    assert len(attempts) == 5 and len(sleeps) == 4 and sum(sleeps) < 1
    assert len(set(attempts)) == 1
    assert not list(target.parent.glob('worker.json.*.tmp'))


def test_atomic_write_other_oserror_is_not_retried_or_masked(store, monkeypatch):
    import src.storage as storage
    store.write_json('state/worker.json', {'version': 'old'})
    target = store.root / 'state' / 'worker.json'
    previous = target.read_bytes()
    attempts, sleeps = [], []
    failure = OSError(errno.ENOSPC, 'simulated disk full')
    def disk_full(source, destination):
        attempts.append(Path(source))
        raise failure
    monkeypatch.setattr(storage.os, 'replace', disk_full)
    monkeypatch.setattr(storage.time, 'sleep', sleeps.append)
    with pytest.raises(OSError) as caught:
        store.write_json('state/worker.json', {'version': 'new'})
    assert caught.value is failure
    assert len(attempts) == 1 and sleeps == []
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('worker.json.*.tmp'))
