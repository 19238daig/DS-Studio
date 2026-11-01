import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.config import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(workspace=tmp_path, _env_file=None, max_upload_bytes=30))
    with TestClient(app) as client:
        yield client


def new_session(client):
    return client.post('/api/v1/sessions', json={'title': 'test'}).json()['session_id']


def test_health_reports_effective_execution_limits(client):
    settings=client.app.state.store.settings
    settings.max_tool_calls,settings.max_agent_steps,settings.task_timeout_seconds=256,1024,3600
    assert client.get('/api/v1/health').json()['limits']=={
        'max_tool_calls':256,'max_agent_steps':1024,'task_timeout_seconds':3600,
        'max_upload_bytes':settings.max_upload_bytes,'max_task_input_bytes':settings.max_task_input_bytes,
        'max_files_per_task':settings.max_files_per_task}


def test_question_logs_rebuild_from_full_durable_events(client):
    sid = new_session(client)
    tid = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': '我的问题'}).json()['task_id']
    store = client.app.state.store
    store.add_event(tid, 'agent_assigned', role='main', target='analyst')
    store.add_event(tid, 'model_usage', role='analyst', usage={'input_tokens': 12, 'output_tokens': 7, 'total_tokens': 19})
    for _ in range(105):
        store.add_event(tid, 'progress', 'more')
    detail = client.get(f'/api/v1/tasks/{tid}').json()['telemetry']
    assert detail['total_tokens'] == 19 and detail['routes'][0]['target'] == 'analyst'
    with TestClient(create_app(store.settings)) as reopened:
        logs = reopened.get(f'/api/v1/sessions/{sid}').json()['question_logs']
        assert len(logs) == 1 and logs[0]['question'] == '我的问题'
        assert logs[0]['telemetry']['total_tokens'] == 19
    client.post(f'/api/v1/tasks/{tid}/cancel')
    cancelled = client.get(f'/api/v1/tasks/{tid}').json()
    assert cancelled['finished_at'] and cancelled['telemetry']['finished']


def test_upload_names_limits_and_cross_session(client):
    sid = new_session(client)
    url = f'/api/v1/sessions/{sid}/files'
    response = client.post(url, files=[('files', ('a.csv', b'x\n1\n')), ('files', ('a.csv', b'x\n2\n'))])
    assert response.status_code == 201, response.text
    files = response.json()['items']
    assert files[0]['file_id'] != files[1]['file_id']
    assert files[0]['relative_path'] != files[1]['relative_path']
    bad = client.post(url, files={'files': ('..\\escape.csv', b'x\n1')})
    assert bad.status_code == 422
    big = client.post(url, files={'files': ('big.csv', b'x' * 31)})
    assert big.status_code == 413
    assert not list(client.app.state.store.root.rglob('upload.tmp'))
    assert len(client.get(url).json()['items']) == 2
    other = new_session(client)
    result = client.post(f'/api/v1/sessions/{other}/messages', json={'content': 'test', 'file_ids': [files[0]['file_id']]})
    assert result.status_code == 404


def test_rename_session_endpoint_validates_and_persists(client):
    sid = new_session(client)
    renamed = client.patch(f'/api/v1/sessions/{sid}', json={'title': ' 工业分析 '})
    assert renamed.status_code == 200 and renamed.json()['title'] == '工业分析'
    items = client.get('/api/v1/sessions?summary=true').json()['items']
    assert items[0]['title'] == '工业分析'
    assert client.patch('/api/v1/sessions/missing', json={'title': 'x'}).status_code == 404
    assert client.patch(f'/api/v1/sessions/{sid}', json={'title': '  '}).status_code == 422
    assert client.patch(f'/api/v1/sessions/{sid}', json={'title': 'x' * 201}).status_code == 422


def test_clear_session_endpoint_keeps_files_and_drops_history(client):
    sid = new_session(client)
    store = client.app.state.store
    client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('a.csv', b'x\n1\n')})
    tid = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': '我的问题'}).json()['task_id']
    refused = client.delete(f'/api/v1/sessions/{sid}/messages')
    assert refused.status_code == 409 and refused.json()['code'] == 'task_active'
    # Uploads enqueue a profile task; cancel every non-terminal session task.
    pending = [t['task_id'] for t in store.list_tasks()
               if t['session_id'] == sid and t['status'] not in
               {'succeeded', 'failed', 'cancelled', 'interrupted', 'waiting_user'}]
    assert pending and tid in pending
    for task_id in pending:
        assert client.post(f'/api/v1/tasks/{task_id}/cancel').status_code in (200, 202)

    assert client.delete(f'/api/v1/sessions/{sid}/messages').status_code == 204
    detail = client.get(f'/api/v1/sessions/{sid}').json()
    assert detail['messages'] == [] and detail['tasks'] == [] and detail['question_logs'] == []
    assert len(client.get(f'/api/v1/sessions/{sid}/files').json()['items']) == 1
    assert not (store.root / 'state' / 'tasks' / f'{tid}.json').exists()
    assert client.get(f'/api/v1/tasks/{tid}').status_code == 404
    assert client.delete(f'/api/v1/sessions/{sid}/messages').status_code == 204
    assert client.delete('/api/v1/sessions/missing/messages').status_code == 404


def test_first_question_titles_the_session_automatically(client):
    sid = client.post('/api/v1/sessions', json={}).json()['session_id']
    client.post(f'/api/v1/sessions/{sid}/messages', json={'content': '分析不同年份的营收差异并生成报告'})
    assert client.get(f'/api/v1/sessions/{sid}').json()['title'] == '分析不同年份的营收差异并生成报告'
    # Follow-up turns never rename the session.
    client.post(f'/api/v1/sessions/{sid}/messages', json={'content': '换成完全不同的另一个问题'})
    assert client.get(f'/api/v1/sessions/{sid}').json()['title'] == '分析不同年份的营收差异并生成报告'
    # Long questions are truncated; whitespace-only keeps the default title.
    long_sid = client.post('/api/v1/sessions', json={}).json()['session_id']
    client.post(f'/api/v1/sessions/{long_sid}/messages', json={'content': '长' * 30})
    assert client.get(f'/api/v1/sessions/{long_sid}').json()['title'] == '长' * 24 + '…'
    # A manually renamed session keeps its name.
    named = client.post('/api/v1/sessions', json={}).json()['session_id']
    client.patch(f'/api/v1/sessions/{named}', json={'title': '我的会话'})
    client.post(f'/api/v1/sessions/{named}/messages', json={'content': '这个问题不应该改标题'})
    assert client.get(f'/api/v1/sessions/{named}').json()['title'] == '我的会话'


def test_upload_exact_limit_and_total_batch_limit(client):
    store = client.app.state.store
    # Use small byte limits to exercise the same streaming boundary without a
    # gigabyte allocation. Every failed batch must roll back its staged files.
    store.settings.max_upload_bytes = store.settings.max_task_input_bytes = 8
    sid = new_session(client)
    url = f'/api/v1/sessions/{sid}/files'
    assert client.post(url,files={'files':('exact.csv',b'x\n12345\n')}).status_code == 201
    assert client.post(url,files={'files':('over.csv',b'x\n123456\n')}).status_code == 413
    response = client.post(url,files=[('files',('a.csv',b'x\n12\n')),('files',('b.csv',b'x\n34\n'))])
    assert response.status_code == 413
    assert len(store.list_files(sid)) == 1
    assert not list(store.root.rglob('upload.tmp'))


def test_idempotency_cancel_retry_and_history(client):
    sid = new_session(client)
    url = f'/api/v1/sessions/{sid}/messages'
    headers = {'Idempotency-Key': 'once'}
    first = client.post(url, json={'content': 'hello'}, headers=headers)
    assert first.status_code == 202
    tid = first.json()['task_id']
    assert client.post(url, json={'content': 'hello'}, headers=headers).json()['task_id'] == tid
    assert client.post(url, json={'content': 'different'}, headers=headers).status_code == 409
    assert len(client.get(f'/api/v1/sessions/{sid}').json()['messages']) == 1
    assert client.post(f'/api/v1/tasks/{tid}/cancel').json()['status'] == 'cancelled'
    assert client.post(f'/api/v1/tasks/{tid}/retry', headers=headers).status_code == 409
    retry_headers = {'Idempotency-Key': 'retry-once'}
    retry = client.post(f'/api/v1/tasks/{tid}/retry', headers=retry_headers)
    assert retry.status_code == 202, retry.text
    assert retry.json()['task_id'] != tid
    assert retry.json()['retry_of'] == tid
    assert client.post(f'/api/v1/tasks/{tid}/retry', headers=retry_headers).json()['task_id'] == retry.json()['task_id']
    restored = create_app(client.app.state.store.settings)
    with TestClient(restored) as c:
        assert c.get(f'/api/v1/tasks/{tid}').json()['status'] == 'cancelled'
        assert len(c.get(f'/api/v1/sessions/{sid}').json()['tasks']) == 2


def test_error_envelope_unknown_ids_and_formats(client):
    result = client.get('/api/v1/tasks/missing')
    assert result.status_code == 404
    assert {'code', 'message', 'request_id'} <= result.json().keys()
    sid = new_session(client)
    result = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'test', 'expected_outputs': ['exe']})
    assert result.status_code == 422
    assert client.get('/api/v1/memories/main').status_code == 422
    assert client.get('/api/v1/artifacts/.env/download').status_code == 422


def test_download_only_registered_completed_untampered(client):
    sid = new_session(client)
    task = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'test'}).json()
    tid = task['task_id']
    store = client.app.state.store
    path = store.root / 'report' / sid / tid / 'report.html'
    path.parent.mkdir(parents=True)
    path.write_text('<html>report</html>')
    artifact = store.register_artifact(path, tid, sid, 'report')
    url = f'/api/v1/artifacts/{artifact.artifact_id}/download'
    assert client.get(url).status_code == 409
    store.update_task(tid, status='succeeded')
    response = client.get(url)
    assert response.status_code == 200
    assert 'attachment' in response.headers['content-disposition']
    assert 'sandbox' in response.headers['content-security-policy']
    path.write_text('modified')
    assert client.get(url).status_code == 409


def test_kb_refresh_is_a_queued_maintenance_task(client):
    result = client.post('/api/v1/kb/refresh')
    assert result.status_code == 202
    task = client.get('/api/v1/tasks/' + result.json()['task_id']).json()
    assert task['kind'] == 'kb_refresh'
    assert task['status'] == 'queued'


def test_upload_profiles_are_queued_then_persisted(client):
    from src.jobs import Worker
    sid = new_session(client)
    response = client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('data.csv', b'x;y\n1;2\n3;4\n')})
    item = response.json()['items'][0]
    assert item['profile_status'] == 'queued'
    task = client.get('/api/v1/tasks/' + item['profile_task_id']).json()
    assert task['kind'] == 'profile'
    assert not client.get(f'/api/v1/sessions/{sid}').json()['messages']
    Worker(client.app.state.store).run_once(subprocess_mode=False)
    current = client.get(f'/api/v1/sessions/{sid}/files').json()['items'][0]
    assert current['profile_status'] == 'ready'
    assert current['profile']['row_count'] == 2
    assert current['profile']['columns'] == ['x', 'y']
    assert len(current['profiles']) == 1
    assert client.get('/api/v1/tasks/' + item['profile_task_id']).json()['status'] == 'succeeded'


def test_malformed_profile_fails_and_retry_keeps_maintenance_kind(client):
    from src.jobs import Worker
    sid = new_session(client)
    response = client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('bad.csv', b'x,y\n1,2,3\n')})
    item = response.json()['items'][0]
    Worker(client.app.state.store).run_once(subprocess_mode=False)
    current = client.get(f'/api/v1/sessions/{sid}/files').json()['items'][0]
    assert current['profile_status'] == 'failed'
    assert current['profile_error']
    task = client.get('/api/v1/tasks/' + item['profile_task_id']).json()
    assert task['status'] == 'failed'
    retry = client.post('/api/v1/tasks/' + task['task_id'] + '/retry', headers={'Idempotency-Key': 'profile-retry'}).json()
    assert retry['kind'] == 'profile'
    assert retry['task_id'] != task['task_id']
    assert client.get(f'/api/v1/sessions/{sid}/files').json()['items'][0]['profile_status'] == 'queued'
    assert client.post('/api/v1/tasks/' + task['task_id'] + '/retry', headers={'Idempotency-Key': 'profile-retry'}).json()['task_id'] == retry['task_id']
    conflict = client.post(f'/api/v1/sessions/{sid}/messages', json={'content': 'test'}, headers={'Idempotency-Key': 'profile-retry'})
    assert conflict.status_code == 409


def test_cancelling_queued_profile_updates_file_state(client):
    sid = new_session(client)
    item = client.post(f'/api/v1/sessions/{sid}/files', files={'files': ('data.csv', b'x\n1\n')}).json()['items'][0]
    client.post('/api/v1/tasks/' + item['profile_task_id'] + '/cancel')
    assert client.get(f'/api/v1/sessions/{sid}/files').json()['items'][0]['profile_status'] == 'cancelled'


def test_kb_refresh_profiles_datasets_without_model(client):
    from src.jobs import Worker
    store = client.app.state.store
    (store.root / 'KB' / 'sample.csv').write_text('x\n1\n2\n')
    task = client.post('/api/v1/kb/refresh').json()
    Worker(store).run_once(subprocess_mode=False)
    assert client.get('/api/v1/tasks/' + task['task_id']).json()['status'] == 'succeeded'
    item = client.get('/api/v1/kb/files').json()['items'][0]
    assert item['profile_status'] == 'ready'
    assert item['profile']['row_count'] == 2


def clarification_parent(client):
    from src.data import DataService
    from src.schemas import TaskSpec
    store = client.app.state.store
    sid = new_session(client)
    (store.root / 'KB' / 'clarification.csv').write_text('value\n2\n4\n')
    fid = store.refresh_kb()[0]['file_id']
    spec = TaskSpec(session_id=sid, request='Analyze and report', file_ids=[fid], expected_outputs=['html', 'xlsx'])
    store.create_task(spec)
    analysis = DataService(store).analyze(spec, fid, 'aggregate', aggregations={'value': 'mean'})
    store.update_task(spec.task_id, status='waiting_user', result={'analysis_ids': [analysis.analysis_id]})
    return sid, spec, analysis


def test_clarification_inherits_verified_results_and_report_formats(client):
    sid, parent, analysis = clarification_parent(client)
    result = client.post(f'/api/v1/sessions/{sid}/messages', json={
        'content': 'For management', 'reply_to_task_id': parent.task_id})
    assert result.status_code == 202, result.text
    spec = client.app.state.store.get_task(result.json()['task_id'])['spec']
    assert spec['analysis_ids'] == [analysis.analysis_id]
    assert spec['file_ids'] == parent.file_ids
    assert spec['expected_outputs'] == ['html', 'xlsx']


def test_explicit_empty_clarification_fields_override_inheritance_and_idempotency(client):
    sid, parent, _ = clarification_parent(client)
    url = f'/api/v1/sessions/{sid}/messages'
    body = {'content': 'Only answer', 'reply_to_task_id': parent.task_id}
    headers = {'Idempotency-Key': 'clarification'}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 202
    override = {**body, 'expected_outputs': [], 'analysis_ids': []}
    assert client.post(url, json=override, headers=headers).status_code == 409
    result = client.post(url, json=override)
    assert result.status_code == 202, result.text
    spec = client.app.state.store.get_task(result.json()['task_id'])['spec']
    assert spec['expected_outputs'] == []
    assert spec['analysis_ids'] == []


@pytest.mark.parametrize('invalid', ['cross_session', 'incomplete', 'input_changed', 'artifact_changed'])
def test_clarification_rejects_invalid_inherited_analysis(client, invalid):
    sid, parent, analysis = clarification_parent(client)
    store = client.app.state.store
    if invalid == 'cross_session':
        other = new_session(client)
        from src.schemas import TaskSpec
        alternate = TaskSpec(session_id=other, request='Another session')
        store.create_task(alternate)
        store.update_task(alternate.task_id, status='waiting_user', result={'analysis_ids': [analysis.analysis_id]})
        sid, parent = other, alternate
    elif invalid == 'incomplete':
        analysis.status = 'failed'
        store.save_analysis(analysis)
    elif invalid == 'input_changed':
        store.resolve_file(parent.file_ids[0], sid).write_text('value\n99\n')
    else:
        store.resolve_artifact(analysis.table_ids[0]).write_text('tampered')
    result = client.post(f'/api/v1/sessions/{sid}/messages', json={
        'content': 'For management', 'reply_to_task_id': parent.task_id})
    assert result.status_code == {'cross_session': 404, 'incomplete': 422, 'input_changed': 409, 'artifact_changed': 409}[invalid], result.text
