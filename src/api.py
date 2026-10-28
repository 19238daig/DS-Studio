"""Local HTTP boundary. No agent loops or analysis run inside request handlers."""
from contextlib import asynccontextmanager
import asyncio
import importlib
import json
import time
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from src.config import Settings
from src.schemas import MessageRequest, TaskSpec, utcnow
from src.services import Services
from src.storage import Store, StoreError, identifier
from src.telemetry import summarize_task
from src.report_catalog import list_report_versions


class SessionRequest(BaseModel):
    title: str = Field(default='新会话', min_length=1, max_length=200)


class DatabasePreviewRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    source_id: str = Field(min_length=1, max_length=80)
    table: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=20, ge=1, le=50)


def create_app(settings: Settings | None = None) -> FastAPI:
    store = Store(settings)
    services = Services(store)

    @asynccontextmanager
    async def lifespan(app):
        store.refresh_kb()
        yield

    application = FastAPI(title='DS Project API', version='1.0.0', lifespan=lifespan)
    application.state.store = store

    @application.middleware('http')
    async def request_id(request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def error(request, status, code, message, details=None):
        return JSONResponse(status_code=status, content=dict(code=code, message=message,
                            request_id=getattr(request.state, 'request_id', ''), details=details))

    @application.exception_handler(StoreError)
    async def store_error(request, exc):
        return error(request, exc.status, exc.code, str(exc))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [{'loc': list(e['loc']), 'message': e['msg'], 'type': e['type']} for e in exc.errors()]
        return error(request, 422, 'validation_error', 'Invalid request parameters', details)

    prefix = '/api/v1'

    @application.get(prefix + '/health')
    def health():
        worker = store.read_json('state/worker.json', {})
        alive = bool(worker and time.time() - worker.get('heartbeat_epoch', 0) < 15)
        return dict(status='ok', worker=dict(ready=alive, **worker), models_configured=bool(store.settings.api_key.get_secret_value()),
                    streaming=dict(enabled=getattr(store.settings, 'sse_stream_enabled', False), protocol='sse-v1'),
                    sandbox_image=store.settings.docker_image,
                    database=dict(configured=bool(store.settings.db_enabled and store.settings.db_user and store.settings.db_password.get_secret_value())),
                    limits=dict(max_tool_calls=store.settings.max_tool_calls,
                                max_agent_steps=store.settings.max_agent_steps,
                                task_timeout_seconds=store.settings.task_timeout_seconds))

    @application.post(prefix + '/sessions', status_code=201)
    def new_session(request: SessionRequest = SessionRequest()):
        return store.new_session(request.title)

    @application.get(prefix + '/sessions')
    def sessions():
        return {'items': store.list_sessions()}

    @application.get(prefix + '/sessions/{session_id}')
    def session(session_id: str):
        with store.lock:
            record = store.get_session(session_id)
            logs = []
            tasks = []
            for tid in record.get('tasks', []):
                item = store.get_task(tid)
                tasks.append(item)
                if item.get('kind', 'message') == 'message':
                    logs.append(dict(task_id=tid, question=item['spec']['request'],
                                     status=item['status'], created_at=item['created_at'],
                                     telemetry=summarize_task(item, store.list_events(tid))))
            visible_tasks = [item['task_id'] for item in tasks if item.get('kind') != 'database_preview']
            return dict(record, tasks=visible_tasks, question_logs=logs, report_versions=list_report_versions(store, session_id, tasks))

    @application.get(prefix + '/database/sources')
    def database_sources():
        from src.database import DatabaseService
        return {'items': DatabaseService(store).list_sources()}

    @application.get(prefix + '/database/sources/{source_id}/catalog')
    def database_catalog(source_id: str):
        from src.database import DatabaseService
        return DatabaseService(store).catalog(source_id)

    @application.post(prefix + '/sessions/{session_id}/database/preview')
    def database_preview(session_id: str, request: DatabasePreviewRequest):
        # This endpoint accepts only a configured relation, never arbitrary SQL.
        # Preview is bounded and uses the exact policy/snapshot path of analysis.
        from src.database import DatabaseService
        store.get_session(session_id)
        sources, tables, _ = services.database_selection([request.source_id], {request.source_id: [request.table]})
        spec = TaskSpec(session_id=session_id, request=f'预览数据库表 {request.table}', intent='database_preview',
                        database_source_ids=sources, database_tables=tables,
                        constraints={'database_sources_explicit': True, 'database_tables_explicit': True})
        with store.lock:
            store.create_task(spec, kind='database_preview')
            store.update_task(spec.task_id, status='running', stage='database_preview', started_at=utcnow())
        try:
            result = DatabaseService(store).preview(spec, request.source_id, request.table, request.limit)
            store.add_event(spec.task_id, 'sql_completed', '数据库表预览完成', source_id=request.source_id,
                            row_count=result['row_count'], duration_seconds=result['duration_seconds'], query_id=result['query_id'])
            store.update_task(spec.task_id, status='succeeded', stage='completed', finished_at=utcnow(),
                              result={'query_id': result['query_id']})
            return {**result, 'task_id': spec.task_id}
        except BaseException as exc:
            store.update_task(spec.task_id, status='failed', stage='failed', finished_at=utcnow(),
                              error={'message': str(exc) if isinstance(exc, StoreError) else '数据库预览未完成。'})
            raise

    @application.delete(prefix + '/sessions/{session_id}', status_code=204)
    def delete_session(session_id: str):
        store.delete_session(session_id)
        return Response(status_code=204)

    @application.post(prefix + '/sessions/{session_id}/files', status_code=201)
    async def upload(session_id: str, files: list[UploadFile]):
        return {'items': await services.upload(session_id, files)}

    @application.get(prefix + '/sessions/{session_id}/files')
    def files(session_id: str):
        return {'items': store.list_files(session_id)}

    @application.post(prefix + '/sessions/{session_id}/messages', status_code=202)
    def message(session_id: str, request: MessageRequest, idempotency_key: str | None = Header(default=None)):
        task = services.submit(session_id, request, idempotency_key)
        return {k: task.get(k) for k in ('session_id', 'message_id', 'task_id', 'status')}

    @application.get(prefix + '/tasks/{task_id}')
    def task(task_id: str):
        with store.lock:
            record = store.get_task(task_id)
            return dict(record, telemetry=summarize_task(record, store.list_events(task_id)))

    @application.get(prefix + '/tasks/{task_id}/events')
    def events(task_id: str, after: int = Query(default=0, ge=0)):
        store.get_task(task_id)
        items = store.list_events(task_id, after)
        return {'items': items, 'cursor': items[-1]['seq'] if items else after}

    @application.get(prefix + '/tasks/{task_id}/stream')
    async def task_stream(task_id: str, request: Request, after: int = Query(default=0, ge=0, le=2**63 - 1),
                          last_event_id: str | None = Header(default=None)):
        """Subscribe to an existing task; this endpoint never invokes an Agent.

        FastAPI's documented StreamingResponse accepts this async iterator:
        https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
        Disk reads run outside the ASGI event loop; a disconnect only unsubscribes.
        """
        if not getattr(store.settings, 'sse_stream_enabled', False):
            raise StoreError('当前服务使用状态轮询。', 404, 'stream_disabled')
        store.get_task(task_id)
        if last_event_id is not None:
            if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id) > 19:
                raise StoreError('无效的任务流游标。', 422, 'invalid_stream_cursor')
            after = max(after, int(last_event_id))
            if after > 2**63 - 1:
                raise StoreError('无效的任务流游标。', 422, 'invalid_stream_cursor')
        from src.task_stream import StreamJournal, final_payload
        from src.storage import TERMINAL
        journal = StreamJournal(store, task_id)

        def frame(event):
            ident = f'id: {event["seq"]}\n' if event.get('seq') is not None else ''
            return ident + 'event: ' + event['kind'] + '\ndata: ' + json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n\n'

        async def events():
            cursor = after
            last_ping = time.monotonic()
            try:
                initial = await asyncio.to_thread(store.get_task, task_id)
            except (OSError, ValueError, StoreError):
                yield frame(dict(kind='stream_unavailable', seq=None, task_id=task_id,
                                 data={'message': '实时订阅暂不可用，继续使用任务状态。'}))
                return
            if initial['status'] not in TERMINAL:
                yield frame(dict(kind='status', seq=None, task_id=task_id,
                    data={'status': initial['status'], 'text': '已受理，正在等待执行。' if initial['status'] == 'queued' else '任务正在处理。'}))
            while not await request.is_disconnected():
                try:
                    page = await asyncio.to_thread(journal.read, cursor)
                    if page['gap']:
                        current = await asyncio.to_thread(store.get_task, task_id)
                        checkpoint = await asyncio.to_thread(journal.checkpoint)
                        if current['status'] in TERMINAL:
                            data = await asyncio.to_thread(final_payload, store, current)
                            yield frame(dict(kind='final', seq=page['last_seq'] + (page['last_kind'] != 'final'),
                                             task_id=task_id, data=data))
                            return
                        cursor = max(0, page['first_seq'] - 1, checkpoint['seq'])
                        yield frame(dict(kind='snapshot', seq=cursor, task_id=task_id,
                                         data={'status': current['status'], 'answer': checkpoint['answer'],
                                               'verified': bool(checkpoint['answer'])}))
                    for item in page['items']:
                        if item['seq'] <= cursor:
                            continue
                        cursor = item['seq']
                        yield frame(item)
                        if item['kind'] == 'final':
                            return
                    current = await asyncio.to_thread(store.get_task, task_id)
                    if current['status'] in TERMINAL:
                        # Old jobs and a stream-disk failure still have a stable
                        # terminal snapshot. Canonical task state is authoritative.
                        if cursor < page['last_seq']:
                            continue
                        if page['last_kind'] == 'final':
                            return
                        terminal_seq = max(page['last_seq'] + 1, 1)
                        if after < terminal_seq:
                            data = await asyncio.to_thread(final_payload, store, current)
                            yield frame(dict(kind='final', seq=terminal_seq, task_id=task_id, data=data))
                        return
                    if time.monotonic() - last_ping >= 2:
                        yield ': heartbeat\n\n'
                        last_ping = time.monotonic()
                    await asyncio.sleep(.15)
                except (OSError, ValueError, StoreError):
                    yield frame(dict(kind='stream_unavailable', seq=None, task_id=task_id,
                                     data={'message': '实时订阅暂不可用，继续使用任务状态。'}))
                    return
        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})

    @application.post(prefix + '/tasks/{task_id}/cancel', status_code=202)
    def cancel(task_id: str):
        return services.cancel(task_id)

    @application.post(prefix + '/tasks/{task_id}/retry', status_code=202)
    def retry(task_id: str, idempotency_key: str | None = Header(default=None)):
        return services.retry(task_id, idempotency_key)

    @application.get(prefix + '/tasks/{task_id}/artifacts')
    def artifacts(task_id: str):
        task = store.get_task(task_id)
        deliverable = task['status'] in {'succeeded', 'waiting_user'}
        return {'items': [dict(**a.model_dump(), downloadable=deliverable) for a in store.list_artifacts(task_id)]}

    @application.get(prefix + '/artifacts/{artifact_id}/download')
    def download(artifact_id: str):
        artifact = store.get_artifact(artifact_id)
        task = store.get_task(artifact.task_id)
        if task['status'] not in {'succeeded', 'waiting_user'}:
            raise StoreError('Artifacts from unfinished or unsuccessful tasks are not available for download', 409, 'incomplete_artifact')
        from src.chart_svg import chart_csp
        csp = ('sandbox allow-scripts; ' + chart_csp() if artifact.kind == 'report' and artifact.mime_type == 'text/html'
               else "sandbox; default-src 'none'")
        return FileResponse(store.resolve_artifact(artifact_id), filename=artifact.name,
                            media_type=artifact.mime_type, content_disposition_type='attachment',
                            headers={'Content-Security-Policy': csp})

    @application.get(prefix + '/kb/files')
    def kb_files():
        return {'items': [f for f in store._index('files').values() if f['source'] == 'kb' and f.get('active', True)]}

    @application.post(prefix + '/kb/refresh', status_code=202)
    def kb_refresh():
        with store.lock:
            session_id = store.read_json('state/kb_session.json')
            if session_id:
                try:
                    store.get_session(session_id)
                except StoreError as exc:
                    if exc.status != 404:
                        raise
                    session_id = None
            if not session_id:
                session_id = store.new_session('KB 索引维护')['session_id']
                store.write_json('state/kb_session.json', session_id)
            task = store.create_task(TaskSpec(session_id=session_id, request='Refresh KB index', intent='kb_refresh'), kind='kb_refresh')
            return {'task_id': task['task_id'], 'status': task['status']}

    def memory_service(owner):
        if owner not in {'analyst', 'report_designer'}:
            raise StoreError('Unknown memory owner', 422, 'invalid_owner')
        try:
            return importlib.import_module('src.memory').MemoryService(store)
        except ModuleNotFoundError:
            return None

    @application.get(prefix + '/memories/{agent}')
    def memories(agent: str):
        service = memory_service(agent)
        return {'items': [x.model_dump() if hasattr(x, 'model_dump') else x for x in service.list(agent)] if service else [],
                'ready': service is not None}

    @application.delete(prefix + '/memories/{agent}/{memory_id}', status_code=204)
    def delete_memory(agent: str, memory_id: str):
        identifier(memory_id)
        service = memory_service(agent)
        if service is None:
            raise StoreError('Memory service is not ready', 503, 'not_ready')
        service.delete(agent, memory_id)
        return Response(status_code=204)

    return application


app = create_app()
