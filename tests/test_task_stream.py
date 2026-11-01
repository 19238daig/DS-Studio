import json
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from src.api import create_app
from src.client import ApiClient, TaskStreamSubscription
from src.config import Settings
from src.jobs import execute_task
from src.schemas import TaskSpec
from src.storage import Store, StoreError
from src.task_stream import StreamJournal, finish, publish


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(workspace=tmp_path, api_key='sk-stream-offline-private', _env_file=None))
    sid = store.new_session()['session_id']
    task = TaskSpec(session_id=sid, request='a deterministic stream test')
    store.create_task(task)
    return store, task


def frames(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]


def test_tool_events_mirror_into_the_public_progress_stream(context):
    from src.task_stream import lifecycle_event
    store, task = context
    published = lifecycle_event(store, task.task_id, 'tool',
                                'analyst 正在执行 run_python', role='analyst', tool='run_python')
    assert published is not None and published['kind'] == 'progress'
    assert published['data']['text'] == 'analyst 正在执行 run_python'
    assert published['role'] == 'analyst'
    # Audit-only event types stay out of the public stream.
    assert lifecycle_event(store, task.task_id, 'span_completed', 'x') is None


def test_only_verified_content_and_fixed_protocol_fields_enter_stream(context, monkeypatch):
    store, task = context
    monkeypatch.setattr(store, 'add_event', lambda *a, **k: pytest.fail('stream must not use the audit journal'))
    publish(store, task.task_id, 'progress', role='analyst', text='正在计算。')
    with pytest.raises(StoreError, match='已验证'):
        publish(store, task.task_id, 'verified_answer_segment', text='999')
    with pytest.raises(StoreError):
        publish(store, task.task_id, 'progress', raw_tool_json={'sql': 'select secret'})
    with pytest.raises(StoreError):
        publish(store, task.task_id, 'reasoning', text='private chain')
    publish(store, task.task_id, 'verified_answer_segment', text='均值为20。', verified=True)
    values = StreamJournal(store, task.task_id).read()['items']
    assert [v['kind'] for v in values] == ['progress', 'verified_answer_segment']
    assert values[1]['data'] == {'text': '均值为20。', 'verified': True}


def test_terminal_stream_reconnect_is_exactly_once_and_not_an_agent_request(context, monkeypatch):
    store, task = context
    publish(store, task.task_id, 'progress', text='正在校验。')
    store.update_task(task.task_id, status='succeeded', final_response='均值为20。', result={'report_ids': []})
    final = finish(store, task.task_id)
    assert finish(store, task.task_id)['seq'] == final['seq']
    assert publish(store, task.task_id, 'progress', text='must not append')['seq'] == final['seq']
    app = create_app(store.settings)
    monkeypatch.setattr(app.state.store, 'create_task', lambda *a, **k: pytest.fail('SSE may not submit work'))
    with TestClient(app) as client:
        response = client.get('/api/v1/tasks/'+task.task_id+'/stream')
        assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
        events = frames(response.text)
        assert [e['seq'] for e in events] == [1, 2]
        assert events[-1]['data']['answer'] == '均值为20。'
        replay = client.get('/api/v1/tasks/'+task.task_id+'/stream', headers={'Last-Event-ID': str(final['seq'])})
        assert frames(replay.text) == []
        assert client.get('/api/v1/tasks/'+task.task_id+'/stream', headers={'Last-Event-ID': 'wrong'}).status_code == 422
        assert client.get('/api/v1/tasks/not-a-task/stream').status_code == 404


def test_old_terminal_task_gets_a_stable_snapshot_without_writing_stream(context):
    store, task = context
    store.update_task(task.task_id, status='succeeded', final_response='existing result', result={})
    journal = StreamJournal(store, task.task_id)
    with TestClient(create_app(store.settings)) as client:
        first = frames(client.get('/api/v1/tasks/'+task.task_id+'/stream').text)
        assert len(first) == 1 and first[0]['kind'] == 'final' and first[0]['seq'] == 1
        assert frames(client.get('/api/v1/tasks/'+task.task_id+'/stream?after=1').text) == []
    assert not journal.directory.exists()


def test_rotation_is_bounded_and_gap_recovers_verified_final(context, monkeypatch):
    import src.task_stream as module
    store, task = context
    monkeypatch.setattr(module, 'MAX_RECORDS', 4)
    monkeypatch.setattr(module, 'RETAIN_RECORDS', 2)
    publish(store, task.task_id, 'verified_answer_segment', text='original verified fact', verified=True)
    for i in range(12):
        publish(store, task.task_id, 'progress', text=f'phase {i}')
    journal = StreamJournal(store, task.task_id)
    page = journal.read()
    assert page['gap'] and page['first_seq'] > 1 and len(page['items']) <= 4
    assert len(list(journal.directory.glob('*.jsonl'))) == 1
    assert journal.checkpoint()['answer'] == 'original verified fact'
    store.update_task(task.task_id, status='succeeded', final_response='canonical final', result={})
    finish(store, task.task_id)
    with TestClient(create_app(store.settings)) as client:
        events = frames(client.get('/api/v1/tasks/'+task.task_id+'/stream').text)
    assert len(events) == 1 and events[0]['kind'] == 'final' and events[0]['data']['answer'] == 'canonical final'


def test_cursor_read_seeks_instead_of_scanning_prior_records(context, monkeypatch):
    from pathlib import Path
    store, task = context
    for i in range(100):
        publish(store, task.task_id, 'progress', text=f'phase {i}')
    original = Path.open
    reads = []

    class Guard:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def __getattr__(self, name): return getattr(self.handle, name)
        def read(self, size=-1):
            assert size >= 0, 'must never read the complete stream journal'
            return self.handle.read(size)
        def readline(self, size=-1):
            assert 0 < size <= 512 * 1024 + 1
            reads.append(size)
            return self.handle.readline(size)

    def guarded(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return Guard(handle) if path.suffix == '.jsonl' else handle

    monkeypatch.setattr(Path, 'open', guarded)
    page = StreamJournal(store, task.task_id).read(99)
    assert [e['seq'] for e in page['items']] == [100]
    assert len(reads) <= 4


def test_final_usage_is_provider_returned_and_secrets_redacted(context):
    store, task = context
    store.update_task(task.task_id, status='running')
    store.add_event(task.task_id, 'model_started', role='main', call_id='call-one')
    store.add_event(task.task_id, 'model_usage', role='main', call_id='call-one',
                    usage={'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13})
    secret = store.settings.api_key.get_secret_value()
    store.update_task(task.task_id, status='succeeded', final_response='value 20 '+secret, result={})
    item = finish(store, task.task_id)
    assert item['data']['telemetry']['total_tokens'] == 13
    assert secret not in json.dumps(item) and '[REDACTED]' in item['data']['answer']


def test_missing_stream_usage_remains_unknown_after_cancellation(context):
    store, task = context
    store.add_event(task.task_id, 'model_started', role='analyst', call_id='without-end')
    store.update_task(task.task_id, status='cancelled')
    item = finish(store, task.task_id)
    assert item['data']['telemetry']['total_tokens'] is None
    assert item['data']['telemetry']['usage_status'] == 'unavailable'
    assert item['data']['answer'] == ''


def test_disabled_sse_and_old_pickled_flags_fall_back(context):
    store, task = context
    del store.settings.__dict__['sse_stream_enabled']
    assert publish(store, task.task_id, 'progress', text='no operation') is None
    with TestClient(create_app(store.settings)) as client:
        assert client.get('/api/v1/health').json()['streaming']['enabled'] is False
        assert client.get('/api/v1/tasks/'+task.task_id+'/stream').status_code == 404


def test_job_publishes_final_after_persisting_response(context):
    store, task = context
    store.update_task(task.task_id, status='running')

    def runner(spec, child_store, event, cancelled):
        event('agent_assigned', role='main', target='analyst')
        publish(child_store, spec.task_id, 'verified_answer_segment', text='mean 20', verified=True)
        return {'final_response': 'mean 20'}

    execute_task(task.task_id, store.settings, runner)
    events = StreamJournal(store, task.task_id).read()['items']
    assert events[-1]['kind'] == 'final' and events[-1]['data']['answer'] == 'mean 20'
    assert store.get_session(task.session_id)['messages'][-1]['content'] == 'mean 20'


def test_client_subscription_reconnect_only_get_and_deduplicates():
    calls = []
    event = {'kind': 'verified_answer_segment', 'seq': 1, 'task_id': 'one', 'data': {'verified': True, 'text': 'mean 20'}}
    final = {'kind': 'final', 'seq': 2, 'task_id': 'one', 'data': {'status': 'succeeded', 'answer': 'mean 20'}}

    def respond(request):
        calls.append(request)
        assert request.method == 'GET'
        values = [event] if len(calls) == 1 else [event, final]
        body = ''.join('id: '+str(e['seq'])+'\ndata: '+json.dumps(e)+'\n\n' for e in values)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, text=body)

    client = ApiClient(transport=httpx.MockTransport(respond))
    sub = client.subscribe_task('one')
    try:
        deadline = time.monotonic() + 4
        while not sub.snapshot()['done'] and time.monotonic() < deadline: time.sleep(.02)
        result = sub.snapshot()
        assert result['done'] and result['answer'] == 'mean 20' and result['cursor'] == 2
        assert len(calls) == 2 and calls[1].headers['Last-Event-ID'] == '1'
    finally:
        sub.close(); client.close()


def test_client_unsupported_stream_falls_back_without_task_submission():
    methods = []
    def respond(request):
        methods.append(request.method)
        return httpx.Response(404, json={'code': 'stream_disabled'})
    client = ApiClient(transport=httpx.MockTransport(respond));sub = client.subscribe_task('one')
    try:
        deadline = time.monotonic() + 2
        while sub.snapshot()['available'] and time.monotonic() < deadline: time.sleep(.01)
        assert sub.snapshot()['available'] is False and methods == ['GET']
    finally: sub.close(); client.close()


def test_warm_executor_flag_is_a_real_setting():
    assert Settings(warm_executor=True, _env_file=None).warm_executor is True
    assert Settings(warm_executor=False, _env_file=None).warm_executor is False


def test_long_verified_answer_is_segmented_without_losing_content(context):
    store, task = context
    answer = '已验证结论。' * 2000
    publish(store, task.task_id, 'verified_answer_segment', text=answer, verified=True)
    journal = StreamJournal(store, task.task_id)
    records = journal.read()['items']
    assert len(records) > 1
    assert ''.join(row['data']['text'] for row in records) == answer
    assert journal.checkpoint()['answer'] == answer


def test_stale_journal_cannot_recreate_a_deleted_session(context):
    store, task = context
    journal = StreamJournal(store, task.task_id)
    store.update_task(task.task_id, status='succeeded')
    store.delete_session(task.session_id)
    for operation in (lambda: journal.append({'kind': 'progress', 'data': {'text': 'private'}}),
                      journal.read, journal.checkpoint):
        with pytest.raises(StoreError):
            operation()
    assert not (store.root / 'state/streams' / task.session_id).exists()


def test_session_delete_waits_for_append_then_removes_all_stream_content(context, monkeypatch):
    store, task = context
    journal = StreamJournal(store, task.task_id)
    store.update_task(task.task_id, status='succeeded')
    entered, release, removed = threading.Event(), threading.Event(), threading.Event()
    original = journal._generation
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(journal, '_generation', paused)
    errors = []
    def append():
        try: journal.append({'kind': 'progress', 'data': {'text': 'private'}})
        except Exception as exc: errors.append(exc)
    def delete():
        # Independent Store instance models the API process/file lock.
        try: Store(store.settings).delete_session(task.session_id)
        except Exception as exc: errors.append(exc)
        finally: removed.set()
    writer = threading.Thread(target=append); writer.start()
    assert entered.wait(3)
    deleter = threading.Thread(target=delete); deleter.start()
    assert not removed.wait(.1)
    release.set(); writer.join(4); deleter.join(4)
    assert not writer.is_alive() and not deleter.is_alive() and not errors
    assert removed.is_set() and not journal.directory.parent.exists()


def test_real_http_first_event_heartbeat_disconnect_and_resume_do_not_restart_job(context, monkeypatch):
    import socket
    import uvicorn
    store, task = context
    store.update_task(task.task_id, status='running', execution_id='one-existing-execution')
    app = create_app(store.settings)
    monkeypatch.setattr(app.state.store, 'create_task', lambda *a, **k: pytest.fail('subscription cannot enqueue'))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(10)
    address = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        path = address + '/api/v1/tasks/' + task.task_id + '/stream'
        started = time.monotonic()
        with httpx.stream('GET', path, timeout=4) as response:
            lines = response.iter_lines()
            while True:
                line = next(lines)
                if line.startswith('data: '):
                    initial = json.loads(line[6:]); break
            assert initial['kind'] == 'status' and time.monotonic() - started < 2
            progress = publish(store, task.task_id, 'progress', text='validated phase')
            while True:
                line = next(lines)
                if line.startswith('data: ') and json.loads(line[6:]).get('seq') == progress['seq']:
                    break
            assert any(line.startswith(': heartbeat') for line in lines)
        assert store.get_task(task.task_id)['status'] == 'running'
        assert not store.get_task(task.task_id)['cancel_requested']
        store.update_task(task.task_id, status='succeeded', final_response='verified final')
        final = finish(store, task.task_id)
        replay = httpx.get(path, headers={'Last-Event-ID': str(progress['seq'])}, timeout=4)
        values = frames(replay.text)
        assert [row['seq'] for row in values] == [final['seq']]
        assert values[-1]['data']['answer'] == 'verified final'
        assert store.get_task(task.task_id)['execution_id'] == 'one-existing-execution'
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()


def test_stream_storage_failure_does_not_fail_a_validated_task(context, monkeypatch):
    store, task = context
    def disk_failure(*args, **kwargs):
        raise OSError('isolated simulated disk failure')
    monkeypatch.setattr(StreamJournal, 'append', disk_failure)
    def runner(spec, worker_store, event, cancel_check):
        event('agent_assigned', target='analyst')
        return {'final_response': 'verified result'}
    execute_task(task.task_id, store.settings, runner)
    assert store.get_task(task.task_id)['status'] == 'succeeded'
    assert store.get_session(task.session_id)['messages'][-1]['content'] == 'verified result'
