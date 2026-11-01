"""Short application operations; expensive computation belongs to Worker."""
import hashlib
import json
from pathlib import Path

from src.schemas import MessageRequest, TaskSpec, new_id, utcnow
from src.storage import DEFAULT_SESSION_TITLE, FILE_TYPES, TERMINAL, Store, StoreError, auto_title


class Services:
    def __init__(self, store: Store):
        self.store = store

    def database_selection(self, sources: list[str], tables: dict[str, list[str]]) -> tuple[list[str], dict, dict]:
        """Normalize an explicitly selected subset using actual-role metadata."""
        sources = list(dict.fromkeys(sources))
        if set(tables) - set(sources):
            raise StoreError('表选择必须属于本次选中的数据库来源。', 422, 'database_source_not_selected')
        if not sources:
            return sources, {}, {}
        from src.database import DatabaseService
        service = DatabaseService(self.store)
        configured = {source['source_id'] for source in service.list_sources()}
        if not set(sources).issubset(configured):
            raise StoreError('数据库来源未启用或不存在，请检查本机数据库配置。', 422, 'database_source_not_found')
        catalogs, normalized = {}, {}
        for source in sources:
            description = service.catalog(source)
            catalogs[source] = description
            if source in tables:
                chosen = list(dict.fromkeys(name if '.' in name else 'olist.' + name for name in tables[source]))
                visible = {table['qualified_name'] for table in description['tables']}
                if not set(chosen).issubset(visible):
                    raise StoreError('选择的数据库表不存在或当前账号无权读取。', 403, 'database_table_not_allowed')
                normalized[source] = chosen
        return sources, normalized, catalogs

    def submit(self, session_id: str, request: MessageRequest, key: str | None = None, *, retry_of: str | None = None,
               _database_explicit: tuple[bool, bool] | None = None,
               _revision_plan: dict | None = None) -> dict:
        key = key or request.idempotency_key
        if key and len(key) > 128:
            raise StoreError('Idempotency key exceeds 128 characters', 422)
        payload = request.model_dump(exclude={'idempotency_key'})
        if payload.get('base_report_id') is None:
            payload.pop('base_report_id', None)
        # Keep pre-database idempotency digests stable for ordinary file tasks.
        for field in ('database_source_ids', 'database_tables'):
            if not payload[field]:
                payload.pop(field)
        if request.reply_to_task_id:
            # Omitted fields inherit from the parent, while explicit empty
            # lists are deliberate replacements and must not share a key.
            payload['reply_fields'] = sorted(request.model_fields_set & {'analysis_ids', 'expected_outputs', 'database_source_ids', 'database_tables'})
        elif request.model_fields_set & {'database_source_ids', 'database_tables'}:
            payload['database_fields'] = sorted(request.model_fields_set & {'database_source_ids', 'database_tables'})
        if retry_of:
            payload['retry_of'] = retry_of
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.lock:
            self.store.get_session(session_id)
            # The task record is the canonical idempotency mapping, so crashes do
            # not leave a separate mapping referring to a missing task.
            if key:
                for task in self.store.list_tasks():
                    if task['session_id'] == session_id and task.get('idempotency_key') == key:
                        if task.get('request_digest') != digest or task.get('retry_of') != retry_of:
                            raise StoreError('Idempotency key was already used with different content', 409, 'idempotency_conflict')
                        return task
            files = list(dict.fromkeys(request.file_ids))
            analyses = list(dict.fromkeys(request.analysis_ids))
            expected_outputs = list(request.expected_outputs)
            sources = list(request.database_source_ids)
            tables = {source: list(names) for source, names in request.database_tables.items()}
            source_explicit, table_explicit = _database_explicit or (
                'database_source_ids' in request.model_fields_set, 'database_tables' in request.model_fields_set)
            parent_id = request.reply_to_task_id
            revision_plan = _revision_plan
            if parent_id:
                parent = self.store.get_task(parent_id)
                if parent['session_id'] != session_id:
                    raise StoreError('Referenced task is not in this session', 404, 'not_found')
                if parent['status'] != 'waiting_user':
                    raise StoreError('Only waiting_user tasks accept clarification replies', 409, 'invalid_state')
                if not files:
                    files = list(parent['spec']['file_ids'])
                if 'analysis_ids' not in request.model_fields_set:
                    analyses = list(dict.fromkeys([
                        *parent['spec'].get('analysis_ids', []),
                        *(parent.get('result') or {}).get('analysis_ids', []),
                    ]))
                if 'expected_outputs' not in request.model_fields_set:
                    expected_outputs = list(parent['spec'].get('expected_outputs', []))
                if 'database_source_ids' not in request.model_fields_set:
                    sources = list(parent['spec'].get('database_source_ids', []))
                    source_explicit = parent['spec'].get('constraints', {}).get('database_sources_explicit', bool(sources))
                if 'database_tables' not in request.model_fields_set:
                    tables = {source: list(names) for source, names in parent['spec'].get('database_tables', {}).items() if source in sources}
                    table_explicit = parent['spec'].get('constraints', {}).get('database_tables_explicit', bool(tables))
                revision_plan = parent['spec'].get('constraints', {}).get('report_revision')
            if request.base_report_id:
                from src.reporting import ReportService
                artifact = self.store.get_artifact(request.base_report_id)
                if artifact.session_id != session_id or artifact.kind != 'report':
                    raise StoreError('基础报告不属于当前会话。', 404, 'invalid_report_base')
                original = TaskSpec.model_validate(self.store.get_task(artifact.task_id)['spec'])
                base = ReportService(self.store).read_report(original, artifact.artifact_id)
                if revision_plan and revision_plan.get('base_report_id') != base['report_id']:
                    raise StoreError('不能切换已经锁定的报告版本。', 409, 'revision_base_changed')
                for aid in base['analysis_ids']:
                    evidence = self.store.get_analysis(aid, session_id)
                    if ('file_ids' in request.model_fields_set and
                            not set(evidence.input_versions).issubset(files)):
                        raise StoreError('当前文件选择不涵盖基础报告的数据，请选择所需来源。', 403, 'file_not_selected')
                analyses = list(dict.fromkeys([*base['analysis_ids'], *analyses]))
                if not expected_outputs:
                    expected_outputs = list(base['formats'])
                if not revision_plan:
                    revision_plan = dict(base_report_id=base['report_id'], base_report_hash=base['hash'],
                        base_version=base['version'], base_analysis_ids=base['analysis_ids'],
                        change_request=request.content, targets=[])
            if len(files) > self.store.settings.max_files_per_task:
                raise StoreError('Too many files selected', 413, 'file_limit')
            size = 0
            for file_id in files:
                record = self.store.get_file(file_id, session_id)
                self.store.resolve_file(file_id, session_id)
                size += record['size']
            if size > self.store.settings.max_task_input_bytes:
                raise StoreError('Selected data exceeds task input limit', 413, 'size_limit')
            inherited_tables = {}
            database_analyses = []
            for analysis_id in analyses:
                analysis = self.store.get_analysis(analysis_id, session_id)
                if analysis.status != 'completed':
                    raise StoreError('Only completed analyses may be reused', 422, 'invalid_analysis')
                if analysis.db_refs:
                    database_analyses.append(analysis)
                    origin = self.store.get_task(analysis.task_id)['spec']
                    for ref in analysis.db_refs:
                        source = ref['source_id']
                        if source not in sources:
                            if source_explicit or parent_id:
                                raise StoreError('引用的分析超出本次数据库来源选择。', 403, 'database_source_not_selected')
                            sources.append(source)
                        original_tables = origin.get('database_tables', {}).get(source)
                        if source not in inherited_tables:
                            inherited_tables[source] = None if original_tables is None else set(original_tables)
                        elif original_tables is None:
                            inherited_tables[source] = None
                        elif inherited_tables[source] is not None:
                            inherited_tables[source].update(original_tables)
                for artifact_id in analysis.table_ids + analysis.chart_ids + analysis.script_ids:
                    artifact = self.store.get_artifact(artifact_id)
                    if artifact.session_id != session_id:
                        raise StoreError('Analysis artifact is not in this session', 404, 'not_found')
                    self.store.resolve_artifact(artifact_id)
                for file_id in analysis.input_versions:
                    if file_id not in files:
                        files.append(file_id)
                        size += self.store.get_file(file_id, session_id)['size']
            if size > self.store.settings.max_task_input_bytes or len(files) > self.store.settings.max_files_per_task:
                raise StoreError('Referenced analysis exceeds task input limits', 413, 'size_limit')
            if any(output not in {'html', 'xlsx'} for output in expected_outputs):
                raise StoreError('Supported report formats are html and xlsx', 422, 'invalid_format')
            if not table_explicit and not parent_id and not source_explicit:
                for source, names in inherited_tables.items():
                    if names is not None:
                        tables[source] = sorted(names)
            sources, tables, catalogs = self.database_selection(sources, tables)
            for analysis in database_analyses:
                for ref in analysis.db_refs:
                    description = catalogs[ref['source_id']]
                    metadata = self.store.read_json(self.store.get_artifact(ref['metadata_artifact_id']).relative_path)
                    allowed = set(tables.get(ref['source_id'], [table['qualified_name'] for table in description['tables']]))
                    if ref['scope_fingerprint'] != description['scope_fingerprint'] or not set(metadata['tables']).issubset(allowed):
                        raise StoreError('引用的数据库结果超出当前表选择或权限范围。', 403, 'database_table_not_allowed')
            constraints = {'database_sources_explicit': source_explicit, 'database_tables_explicit': table_explicit}
            if revision_plan:
                # Internal provenance only. Public MessageRequest has no plan
                # field; the worker reauthorizes the pinned version/hash/scope.
                constraints['report_revision'] = json.loads(json.dumps(revision_plan))
            spec = TaskSpec(session_id=session_id, request=request.content, file_ids=files, analysis_ids=analyses,
                            parent_task_id=parent_id, expected_outputs=expected_outputs,
                            database_source_ids=sources, database_tables=tables,
                            constraints=constraints)
            # Name the conversation after its first question; a manually chosen
            # title (anything but the default) and follow-up turns are kept.
            title = auto_title(request.content)
            session = self.store.get_session(session_id)
            if title and not session.get('messages') and session.get('title') == DEFAULT_SESSION_TITLE:
                self.store.rename_session(session_id, title)
            message = self.store.append_message(session_id, 'user', request.content, task_id=spec.task_id, file_ids=files)
            return self.store.create_task(spec, message_id=message['message_id'], idempotency_key=key, request_digest=digest,
                                          kind='message', retry_of=retry_of)
    async def upload(self, session_id: str, uploads: list) -> list[dict]:
        self.store.get_session(session_id)
        if not uploads or len(uploads) > self.store.settings.max_files_per_task:
            raise StoreError('Upload between 1 and 10 files per request', 413, 'file_limit')
        staged = []
        total = 0
        try:
            for upload in uploads:
                name = upload.filename or ''
                if not name or name in {'.', '..'} or any(c in name for c in ('/', '\\', ':', '\x00')) or len(name) > 240:
                    raise StoreError('Invalid filename', 422, 'unsafe_filename')
                if Path(name).suffix.lower() not in FILE_TYPES:
                    raise StoreError('Only csv, xlsx, xls and static html files are supported', 422, 'invalid_file_type')
                file_id = new_id()
                directory = self.store.safe_path(Path('input') / session_id / file_id, 'input')
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / ('data' + Path(name).suffix.lower())
                temp = directory / 'upload.tmp'
                staged.append((temp, target, name, file_id))
                size = 0
                with temp.open('wb') as stream:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > self.store.settings.max_upload_bytes or total > self.store.settings.max_task_input_bytes:
                            raise StoreError('Upload exceeds configured size limit', 413, 'size_limit')
                        stream.write(chunk)
                if size == 0:
                    raise StoreError('Empty uploads are not supported', 422, 'empty_file')
                with temp.open('rb') as stream:
                    signature = stream.read(8)
                suffix = target.suffix
                if (suffix == '.xlsx' and not signature.startswith(b'PK')) or (suffix == '.xls' and signature != bytes.fromhex('D0CF11E0A1B11AE1')):
                    raise StoreError('Workbook contents do not match extension', 422, 'invalid_file_type')
            records = []
            with self.store.lock:
                self.store.get_session(session_id)
                for temp, target, name, file_id in staged:
                    temp.replace(target)
                    records.append(self.store.register_file(target, name, session_id, file_id=file_id))
                records = self.queue_profiles([r['file_id'] for r in records], session_id)
            return records
        except BaseException:
            with self.store.lock:
                files = self.store._index('files')
                for temp, target, name, file_id in staged:
                    files.pop(file_id, None)
                    temp.unlink(missing_ok=True)
                    target.unlink(missing_ok=True)
                    if temp.parent.exists():
                        temp.parent.rmdir()
                self.store._save_index('files', files)
            raise
        finally:
            for upload in uploads:
                await upload.close()

    def queue_profiles(self, file_ids: list[str], session_id: str) -> list[dict]:
        records = []
        with self.store.lock:
            for file_id in file_ids:
                item = self.store.get_file(file_id, session_id)
                task = self.store.create_task(TaskSpec(session_id=session_id, request=f"Profile {item['name']}",
                                                       file_ids=[file_id], intent='profile'), kind='profile')
                records.append(self.store.update_file_metadata(file_id, session_id, profile_task_id=task['task_id'],
                                                               profile_status='queued', profile_error=None))
        return records

    def cancel(self, task_id: str) -> dict:
        with self.store.lock:
            task = self.store.get_task(task_id)
            if task['status'] in TERMINAL:
                return task
            changes = dict(cancel_requested=True)
            if task['status'] == 'queued':
                changes.update(status='cancelled', stage='cancelled', finished_at=utcnow())
            task = self.store.update_task(task_id, **changes)
            if task['status'] == 'cancelled':
                self.store.finalize_profile_task(task_id, 'cancelled', 'Profile task was cancelled')
            self.store.add_event(task_id, 'cancel_requested', '已请求取消任务')
            return task

    def retry(self, task_id: str, key: str | None = None) -> dict:
        with self.store.lock:
            old = self.store.get_task(task_id)
            if old['status'] not in {'failed', 'interrupted', 'cancelled'}:
                raise StoreError('Only failed, interrupted or cancelled tasks may be retried', 409, 'invalid_state')
            if old.get('kind', 'message') != 'message':
                if key and len(key) > 128:
                    raise StoreError('Idempotency key exceeds 128 characters', 422)
                for task in self.store.list_tasks():
                    if key and task['session_id'] == old['session_id'] and task.get('idempotency_key') == key:
                        if task.get('retry_of') != task_id:
                            raise StoreError('Idempotency key was already used for a different operation', 409, 'idempotency_conflict')
                        return task
                spec = TaskSpec.model_validate(old['spec']).model_copy(update={'task_id': new_id(), 'parent_task_id': task_id})
                for file_id in spec.file_ids:
                    self.store.resolve_file(file_id, spec.session_id)
                task = self.store.create_task(spec, kind=old['kind'], retry_of=task_id, idempotency_key=key)
                if old['kind'] == 'profile':
                    for file_id in spec.file_ids:
                        self.store.update_file_metadata(file_id, spec.session_id, profile_task_id=task['task_id'], profile_status='queued', profile_error=None)
                return task
            request = MessageRequest(content=old['spec']['request'], file_ids=old['spec']['file_ids'],
                                     expected_outputs=old['spec']['expected_outputs'], analysis_ids=old['spec'].get('analysis_ids', []),
                                     database_source_ids=old['spec'].get('database_source_ids', []), database_tables=old['spec'].get('database_tables', {}))
            flags = old['spec'].get('constraints', {})
            revision_plan = flags.get('report_revision')
            if revision_plan:
                # Keep the selected immutable base and intended targets. A
                # failed task's transient patch/artifacts are not a fresh
                # retry's verified supplement and must not be reused by name.
                revision_plan = {key: value for key, value in revision_plan.items()
                                 if key not in {'patch', 'supplemental_analysis_ids'}}
            return self.submit(old['session_id'], request, key, retry_of=task_id,
                               _database_explicit=(flags.get('database_sources_explicit', bool(request.database_source_ids)),
                                                   flags.get('database_tables_explicit', bool(request.database_tables))),
                               _revision_plan=revision_plan)
