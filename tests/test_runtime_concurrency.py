"""Independent review checks; isolated workspaces and no model calls."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.config import Settings
from src.jobs import Worker, execute_task
from src.schemas import MessageRequest
from src.services import Services
from src.storage import Store


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(_env_file=None, workspace=tmp_path, api_key=''))
    with TestClient(app) as client:
        yield client


def session(client):
    response = client.post('/api/v1/sessions', json={'title': 'Review'})
    assert response.status_code == 201
    return response.json()['session_id']


def test_parallel_idempotency_is_single_task(client):
    sid = session(client)
    url = f'/api/v1/sessions/{sid}/messages'
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: client.post(url, json={'content': 'review'}, headers={'Idempotency-Key': 'one'}), range(6)))
    assert all(r.status_code == 202 for r in results)
    assert len({r.json()['task_id'] for r in results}) == 1
    body = client.get(f'/api/v1/sessions/{sid}').json()
    assert len(body['tasks']) == len(body['messages']) == 1
    assert client.post(url, json={'content': 'different'}, headers={'Idempotency-Key': 'one'}).status_code == 409


def test_retry_cannot_return_original_cancelled_task(client):
    sid = session(client)
    original = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'review'}, headers={'Idempotency-Key': 'one'}).json()
    tid = original['task_id']
    client.post(f'/api/v1/tasks/{tid}/cancel')
    response = client.post(f'/api/v1/tasks/{tid}/retry', headers={'Idempotency-Key': 'one'})
    assert response.status_code in {202, 409}
    if response.status_code == 202:
        assert response.json()['task_id'] != tid
        assert response.json()['status'] == 'queued'


def test_upload_batch_rolls_back_all_files(client):
    sid = session(client)
    response = client.post(f'/api/v1/sessions/{sid}/files', files=[('files', ('a.csv', b'a\n1\n')), ('files', ('bad.exe', b'bad'))])
    assert response.status_code == 422
    assert not client.app.state.store.list_files(sid)
    assert not list((client.app.state.store.root / 'input' / sid).rglob('*.*'))


@pytest.mark.parametrize('filename', ['../bad.csv', 'C:bad.csv', 'sub\\bad.csv', '..'])
def test_upload_rejects_unsafe_names(client, filename):
    sid = session(client)
    response = client.post(f'/api/v1/sessions/{sid}/files', files={'files': (filename, b'x\n1\n')})
    assert response.status_code == 422


def test_upload_limit_cleans_staged_data(client):
    sid = session(client)
    client.app.state.store.settings.max_upload_bytes = 4
    response = client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('a.csv', b'x\n123\n')})
    assert response.status_code == 413
    assert not client.app.state.store.list_files(sid)
    assert not list((client.app.state.store.root / 'input' / sid).rglob('*.*'))


def test_cross_session_input_and_changed_input_rejected(client):
    sid = session(client)
    other = session(client)
    record = client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('a.csv', b'x\n1\n')}).json()['items'][0]
    request = {'content': 'review', 'file_ids': [record['file_id']]}
    assert client.post(f'/api/v1/sessions/{other}/messages', json=request).status_code == 404
    (client.app.state.store.root / record['relative_path']).write_text('x\n2\n')
    assert client.post(f'/api/v1/sessions/{sid}/messages', json=request).status_code == 409


def test_kb_updates_create_new_version_and_invalidate_old(client):
    store = client.app.state.store
    sid = session(client)
    path = store.root / 'KB' / 'a.csv'
    path.write_text('x\n1\n')
    first = store.refresh_kb()[0]
    path.write_text('x\n2\n')
    changed = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'review', 'file_ids': [first['file_id']]})
    assert changed.status_code == 409
    second = store.refresh_kb()[0]
    assert second['file_id'] != first['file_id']
    assert client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'review', 'file_ids': [first['file_id']]}).status_code == 404
    assert client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'review', 'file_ids': [second['file_id']]}).status_code == 202


def test_kb_refresh_recovers_deleted_maintenance_session(client):
    task = client.post('/api/v1/kb/refresh').json()
    store = client.app.state.store
    sid = store.get_task(task['task_id'])['session_id']
    client.post(f"/api/v1/tasks/{task['task_id']}/cancel")
    assert client.delete(f'/api/v1/sessions/{sid}').status_code == 204
    assert client.post('/api/v1/kb/refresh').status_code == 202


def test_artifact_download_only_after_success_and_hash_matches(client):
    sid = session(client)
    task = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'review'}).json()
    tid = task['task_id']
    store = client.app.state.store
    output = store.root / 'report' / sid / tid / 'report.html'
    output.parent.mkdir(parents=True)
    output.write_text('<html>review</html>')
    artifact = store.register_artifact(output, tid, sid, 'report')
    url = f'/api/v1/artifacts/{artifact.artifact_id}/download'
    assert client.get(url).status_code == 409
    store.update_task(tid, status='succeeded')
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('attachment')
    output.write_text('tampered')
    assert client.get(url).status_code == 409


def deterministic_success(task, store, event, cancelled):
    event('review', 'review runner')
    return {'final_response': 'review complete'}


def deterministic_failure(task, store, event, cancelled):
    return {'status': 'failed', 'error': 'review failure'}


def test_child_task_process_completes_without_model_calls(client):
    store = client.app.state.store
    sid = session(client)
    task = Services(store).submit(sid, MessageRequest(content='review'))
    assert Worker(store).run_once(deterministic_success)
    result = store.get_task(task['task_id'])
    assert result['status'] == 'succeeded'
    assert result['final_response'] == 'review complete'


def test_failed_runner_result_does_not_become_success(client):
    store = client.app.state.store
    sid = session(client)
    task = Services(store).submit(sid, MessageRequest(content='review'))
    output = store.root / 'report' / sid / task['task_id'] / 'partial.html'
    output.parent.mkdir(parents=True)
    output.write_text('incomplete')
    artifact = store.register_artifact(output, task['task_id'], sid, 'report')
    execute_task(task['task_id'], store.settings, deterministic_failure)
    assert store.get_task(task['task_id'])['status'] == 'failed'
    assert client.get(f'/api/v1/artifacts/{artifact.artifact_id}/download').status_code == 409


def test_second_worker_cannot_overwrite_first_worker_heartbeat(client):
    store = client.app.state.store
    first, second = Worker(store), Worker(store)
    first.heartbeat()
    with first.lock:
        with pytest.raises(RuntimeError, match='Another Worker'):
            second.run_forever()
    state = store.read_json('state/worker.json')
    assert state['owner'] == first.owner
    assert state['heartbeat_epoch'] > 0


def test_waiting_user_reply_is_claimable_and_history_is_preserved(client):
    store = client.app.state.store
    sid = session(client)
    service = Services(store)
    original = service.submit(sid, MessageRequest(content='review'))
    store.update_task(original['task_id'], status='waiting_user', final_response='Choose a column')
    reply = service.submit(sid, MessageRequest(content='Use x', reply_to_task_id=original['task_id']))
    worker = Worker(store)
    assert worker.claim()['task_id'] == reply['task_id']
    assert store.get_task(original['task_id'])['status'] == 'waiting_user'
    assert store.get_task(reply['task_id'])['spec']['parent_task_id'] == original['task_id']
    service.cancel(reply['task_id'])
    execute_task(reply['task_id'], store.settings, deterministic_success)
    assert store.get_task(reply['task_id'])['status'] == 'cancelled'
    assert store.get_task(original['task_id'])['status'] == 'waiting_user'
    assert client.delete(f'/api/v1/sessions/{sid}').status_code == 204
