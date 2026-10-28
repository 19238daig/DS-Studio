from pathlib import Path
import errno
import json

import pytest

from src.config import Settings
from src.schemas import AnalysisResult, TaskSpec
from src.storage import Store, StoreError


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
