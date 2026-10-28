import json
import io

import httpx
import pytest

from src.client import ApiClient


def test_upload_forwards_file_stream_in_bounded_chunks():
    content = b'x\n1\n' * 40000
    class ChunkedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536, 'Upload must stream without reading the whole file'
            return super().read(size)
    stream = ChunkedFile(content)
    def respond(request):
        assert request.url.path == '/api/v1/sessions/test-session/files'
        assert b'filename="data.csv"' in request.content and content in request.content
        assert request.extensions['timeout']['write'] == 300
        assert request.extensions['timeout']['read'] == 300
        return httpx.Response(201,json={'items':[{'name':'data.csv'}]})
    client = ApiClient(transport=httpx.MockTransport(respond))
    try:
        assert client.upload('test-session',[('data.csv',stream,'text/csv')])['items'][0]['name'] == 'data.csv'
        assert not stream.closed
    finally:
        client.close()
        stream.close()


@pytest.mark.parametrize('analysis_ids', [None, [], ['verified-analysis']])
def test_submit_preserves_omitted_and_explicit_analysis_references(analysis_ids):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(202, json={'task_id': 'queued-task'})

    client = ApiClient(transport=httpx.MockTransport(respond))
    try:
        result = client.submit('session-one', 'For management', ['file-one'], ['html'], 'stable-key',
                               reply_to_task_id='waiting-task', analysis_ids=analysis_ids)
    finally:
        client.close()
    assert result == {'task_id': 'queued-task'}
    request = captured[0]
    assert request.method == 'POST'
    assert request.url.path == '/api/v1/sessions/session-one/messages'
    assert request.headers['Idempotency-Key'] == 'stable-key'
    payload = json.loads(request.content)
    assert payload['content'] == 'For management'
    assert payload['file_ids'] == ['file-one']
    assert payload['expected_outputs'] == ['html']
    assert payload['reply_to_task_id'] == 'waiting-task'
    if analysis_ids is None:
        assert 'analysis_ids' not in payload
    else:
        assert payload['analysis_ids'] == analysis_ids


def test_delete_session_uses_delete_http_method_without_task_submission():
    captured = []
    def respond(request):
        captured.append(request)
        return httpx.Response(204)
    client = ApiClient(transport=httpx.MockTransport(respond))
    try:
        client.delete_session('selected-session')
    finally:
        client.close()
    assert len(captured) == 1
    assert captured[0].method == 'DELETE'
    assert captured[0].url.path == '/api/v1/sessions/selected-session'
