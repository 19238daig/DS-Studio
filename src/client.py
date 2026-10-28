"""The Streamlit UI's only business-data access: the local HTTP API."""
from __future__ import annotations

from typing import Any, BinaryIO
from collections import deque
import json
import threading
import httpx


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class ApiClient:
    def __init__(self, base_url: str = 'http://127.0.0.1:8000', *, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip('/')
        self._client = httpx.Client(base_url=self.base_url + '/api/v1/', timeout=httpx.Timeout(20, connect=3), transport=transport, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path.lstrip('/'), **kwargs)
        except httpx.RequestError as exc:
            raise ApiError('无法连接本地后端，请运行 scripts/start.ps1，并检查 API 服务是否已启动。') from exc
        if response.is_error:
            try:
                data = response.json()
                detail = data.get('error', data.get('detail', data))
                message = detail.get('message', '请求未完成，请检查输入或服务状态。') if isinstance(detail, dict) else '请求未完成，请检查输入或服务状态。'
                request_id = data.get('request_id') or (detail.get('request_id') if isinstance(detail, dict) else None)
            except ValueError:
                message, request_id = '后端返回了错误响应。', None
            raise ApiError(f'{message}（HTTP {response.status_code}）', response.status_code, request_id)
        content_type = response.headers.get('content-type', '')
        if 'json' in content_type:
            return response.json()
        return response.content

    def health(self) -> dict[str, Any]:
        return self._request('GET', 'health')

    def sessions(self) -> list[dict[str, Any]]:
        return self._request('GET', 'sessions').get('items', [])

    def create_session(self) -> dict[str, Any]:
        return self._request('POST', 'sessions', json={})

    def session(self, session_id: str) -> dict[str, Any]:
        return self._request('GET', f'sessions/{session_id}')

    def delete_session(self, session_id: str) -> Any:
        return self._request('DELETE', f'sessions/{session_id}')

    def files(self, session_id: str) -> list[dict[str, Any]]:
        return self._request('GET', f'sessions/{session_id}/files').get('items', [])

    def upload(self, session_id: str, uploads: list[tuple[str, bytes | BinaryIO, str]]) -> dict[str, Any]:
        return self._request('POST', f'sessions/{session_id}/files', files=[('files', item) for item in uploads],
                             timeout=httpx.Timeout(300, connect=3))

    def submit(self, session_id: str, content: str, file_ids: list[str], outputs: list[str], key: str, *, reply_to_task_id: str | None = None, analysis_ids: list[str] | None = None,
               database_source_ids: list[str] | None = None, database_tables: dict[str, list[str]] | None = None) -> dict[str, Any]:
        payload = {'content': content, 'file_ids': file_ids, 'expected_outputs': outputs, 'reply_to_task_id': reply_to_task_id}
        if analysis_ids is not None:
            payload['analysis_ids'] = analysis_ids
        if database_source_ids is not None:
            payload['database_source_ids'] = database_source_ids
        if database_tables is not None:
            payload['database_tables'] = database_tables
        return self._request('POST', f'sessions/{session_id}/messages', json=payload, headers={'Idempotency-Key': key})

    def database_sources(self) -> list[dict[str, Any]]:
        return self._request('GET', 'database/sources').get('items', [])

    def database_catalog(self, source_id: str) -> dict[str, Any]:
        return self._request('GET', f'database/sources/{source_id}/catalog')

    def database_preview(self, session_id: str, source_id: str, table: str, limit: int = 20) -> dict[str, Any]:
        return self._request('POST', f'sessions/{session_id}/database/preview',
                             json={'source_id': source_id, 'table': table, 'limit': limit})

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request('GET', f'tasks/{task_id}')

    def events(self, task_id: str, after: int = 0) -> dict[str, Any]:
        return self._request('GET', f'tasks/{task_id}/events', params={'after': after})

    def stream_events(self, task_id: str, after: int = 0, *, stop_event=None):
        """Read SSE events without submitting, retrying or cancelling a task."""
        headers = {'Accept': 'text/event-stream'}
        if after:
            headers['Last-Event-ID'] = str(after)
        try:
            with self._client.stream('GET', f'tasks/{task_id}/stream', params={'after': after},
                                     headers=headers, timeout=httpx.Timeout(15, connect=3)) as response:
                if response.is_error or 'text/event-stream' not in response.headers.get('content-type', ''):
                    raise ApiError('实时订阅暂不可用，将继续显示任务状态。', response.status_code)
                fields = []
                size = 0
                for line in response.iter_lines():
                    if stop_event is not None and stop_event.is_set():
                        return
                    if line.startswith(':'):
                        continue
                    if not line:
                        if fields:
                            event = json.loads('\n'.join(fields))
                            if isinstance(event, dict) and event.get('task_id') == task_id:
                                yield event
                        fields, size = [], 0
                    elif line.startswith('data:'):
                        value = line[5:].lstrip(' ')
                        size += len(value)
                        if size > 512 * 1024:
                            raise ApiError('实时订阅记录过大，将继续显示任务状态。')
                        fields.append(value)
        except (httpx.RequestError, ValueError) as exc:
            raise ApiError('实时订阅已断开，将继续显示任务状态。') from exc

    def subscribe_task(self, task_id: str, after: int = 0):
        return TaskStreamSubscription(self, task_id, after=after)

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._request('POST', f'tasks/{task_id}/cancel')

    def retry(self, task_id: str) -> dict[str, Any]:
        return self._request('POST', f'tasks/{task_id}/retry')

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        return self._request('GET', f'tasks/{task_id}/artifacts').get('items', [])

    def download(self, artifact_id: str) -> bytes:
        return self._request('GET', f'artifacts/{artifact_id}/download')

    def kb_files(self) -> list[dict[str, Any]]:
        return self._request('GET', 'kb/files').get('items', [])

    def refresh_kb(self) -> dict[str, Any]:
        return self._request('POST', 'kb/refresh')

    def memories(self, agent: str) -> list[dict[str, Any]]:
        return self._request('GET', f'memories/{agent}').get('items', [])

    def delete_memory(self, agent: str, memory_id: str) -> Any:
        return self._request('DELETE', f'memories/{agent}/{memory_id}')


class TaskStreamSubscription:
    """A bounded background inbox; it never calls Streamlit APIs.

    HTTP reconnect is only a GET subscription. The canonical job, model calls
    and cancellation stay owned by the API/Worker. UI snapshots are copied.
    """
    def __init__(self, client, task_id, *, after=0):
        self.client, self.task_id = client, task_id
        self.cursor = after
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._inbox = deque(maxlen=128)
        self._state = {'task_id': task_id, 'cursor': after, 'answer': '', 'progress': '',
                       'status': 'queued', 'done': False, 'available': True}
        self._thread = threading.Thread(target=self._run, name='task-stream-'+task_id[:12], daemon=True)
        self._thread.start()

    def _accept(self, event):
        kind, seq, data = event.get('kind'), event.get('seq'), event.get('data') or {}
        if event.get('task_id') != self.task_id or not isinstance(data, dict):
            return
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq <= self.cursor):
            return
        with self._lock:
            if seq is not None:
                self.cursor = seq
            if kind in {'status', 'progress'}:
                self._state['progress'] = str(data.get('text', ''))[:500]
                if data.get('status'):
                    self._state['status'] = data['status']
            elif kind == 'verified_answer_segment' and data.get('verified') is True:
                self._state['answer'] = (self._state['answer'] + str(data.get('text', '')))[:65536]
            elif kind == 'snapshot':
                answer = str(data.get('answer', ''))[:65536] if data.get('verified') is True else ''
                self._state.update(status=data.get('status', 'running'), answer=answer)
                self._state['done'] = data.get('status') in {'succeeded', 'failed', 'cancelled', 'interrupted', 'waiting_user'}
            elif kind == 'final':
                answer = str(data.get('answer', ''))[:65536] if data.get('status') in {'succeeded', 'waiting_user'} else ''
                self._state.update(status=data.get('status'), answer=answer,
                                   done=True, telemetry=data.get('telemetry'), report_ids=data.get('report_ids', []))
            elif kind == 'stream_unavailable':
                self._state['available'] = False
            self._state['cursor'] = self.cursor
            self._inbox.append(event)

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                for event in self.client.stream_events(self.task_id, self.cursor, stop_event=self._stop):
                    if self._stop.is_set():
                        return
                    self._accept(event)
                    if self.snapshot()['done'] or not self.snapshot()['available']:
                        return
                if self.snapshot()['done']:
                    return
                failures += 1
            except ApiError as exc:
                if exc.status_code in {400, 403, 404, 409, 422}:
                    with self._lock:
                        self._state['available'] = False
                    return
                failures += 1
            if failures >= 3:
                with self._lock:
                    self._state['available'] = False
                return
            if self._stop.wait(min(2, .25 * 2**failures)):
                return

    def snapshot(self):
        with self._lock:
            self._inbox.clear()
            return dict(self._state)

    def close(self):
        self._stop.set()
