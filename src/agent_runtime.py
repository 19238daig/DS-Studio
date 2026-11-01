"""TaskRuntime: the per-task ledger that owns authorization, analysis and
report state, budget counters and the APP_STATE progress context."""
from __future__ import annotations

import importlib
import hashlib
import json
import re
import threading
import time
from contextlib import nullcontext
from functools import wraps
from uuid import uuid4
from pathlib import Path
from typing import Any, Literal

from deepagents import (CompiledSubAgent, GeneralPurposeSubagentProfile, HarnessProfile,
                        create_deep_agent, register_harness_profile)
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from pydantic import ValidationError

from src.backends import KnowledgeService, role_backend
from src.data import DataService
from src.database import DatabaseService
from src.execution import DockerExecutor, RESULT_CONTRACT, validate_analysis_script
from src.llm import build_model
from src.memory import MemoryService
from src.schemas import (TaskSpec, AnalysisResult, ReportResult, ReportNarrative, ReportLayout,
                         ReportMetricReference, ReportTextReplacement, ReportChartEdit, ReportSectionEdit,
                         ReportRevisionPatch)
from src.storage import Store, StoreError
from src.secrets_text import redact_text
from src.skills import ProjectSkillsMiddleware, skill_prompt, skill_sources
from src.answers import (answer_table_limit, automatic_refs, bound_template, narrow_aggregations,
                         positive_clauses, render_metrics, requested_operations, validate_refs, wants_method, wants_tables)
from src.agent_optimization import ContextPolicyMiddleware, deterministic_summary, enabled as context_enabled, revision_digest, standard_result_report

PROFILE_LOCK = threading.Lock()
Aggregation = Literal['sum', 'mean', 'count', 'min', 'max', 'median', 'nunique', 'std']


from src.agent_support import (
    BudgetExceeded, _data_answer, _delivery_gap, _initialization_boundary, _model_call_with_retry,
    _report_candidates, _report_requested, _revision_language, _span, _transient_model_failure,
    _validation_boundary, friendly_failure, redact,
)

class TaskRuntime:
    @_initialization_boundary
    def __init__(self, task, store, event, cancel_check):
        self.task, self.store = task, store
        self.event, self.cancel_check = event, cancel_check
        self.lock = threading.RLock()
        self.tool_context = threading.local()
        self.calls = 0
        self.python_attempts = 0
        self.analysis_failure: str | None = None
        self.report_rounds = 0
        self.report_revisions = 0
        self.report_limitations: list[str] = []
        self.report_revision_info: dict | None = None
        self.prior_reports: list[dict] = []
        self.analyses: list[str] = []
        self.reports: list[str] = []
        self.report_analysis_ids: list[str] = []
        self.clarification: str | None = None
        self.last_error: str | None = None
        self.finished_intent: str | None = None
        self.finished_answer: str = ''
        self.direct_metric_answer: dict | None = None
        self.answer_metric_refs: list[ReportMetricReference] = []
        self.answer_template: str | None = None
        self.answer_binding_needed = False
        self.preference_saved = False
        self.revision_state: dict | None = None
        self._revision_auto_source_restore = False
        self.agents = None
        self.role_requests: dict[str, str] = {}
        self.report_capsule: dict | None = None
        self.pending_handoff: dict | None = None
        self.workflow_active = False
        self.analysis_ready = False
        self.completed_tool_calls: dict[tuple, Any] = {}
        self.failed_tool_calls: dict[tuple, int] = {}
        self.failed_tool_codes: dict[tuple,int] = {}
        self.active_model_calls: dict[str, tuple[str,float]] = {}
        self.first_stream_calls: set[str] = set()
        self.scalar_batch_pending: set[str] = set()
        self.scalar_batch_incomplete = False
        self.scalar_batch_required = 0
        # Only deterministic executors may populate these names from verified
        # catalog metadata. Free-form model labels still pass validate_refs.
        self.trusted_metric_labels: dict[tuple[str,str],str] = {}
        # Per-task file metadata is pinned by input_versions hashes, so the
        # APP_STATE ledger can cache schema lookups across model calls.
        self._file_schema_cache: dict[str, dict] = {}
        # Coordinator finish_task declarations; the 2nd+ ends the main loop.
        self.finish_declarations = 0
        # Whether the most recent model call hit its output-token cap (used by
        # run_python to give truncation-specific feedback).
        self.last_output_capped = False
        self.data = DataService(store)
        self.database = DatabaseService(store)
        self.memory = MemoryService(store)
        self.kb = KnowledgeService(store)
        self.allowed_analysis_ids: list[str] = []
        for aid in task.analysis_ids:
            self.authorize_analysis(aid)
        self.prior_reports = _report_candidates(store, task)
        has_prior_report = bool(self.prior_reports)
        self.followup_report_edit = (has_prior_report and not _report_requested(task, None) and
                                     _report_requested(task, None, has_prior_report=True))
        # A revision may restore its latest report's source only when the user
        # has not explicitly chosen/cleared a database source this turn.
        if (not task.database_source_ids and not task.constraints.get('database_sources_explicit') and
                not task.constraints.get('database_tables_explicit') and
                _report_requested(task, None, has_prior_report=has_prior_report) and
                _revision_language(task.request)):
            for tid in reversed(store.get_session(task.session_id).get('tasks', [])):
                old = store.get_task(tid)
                result = old.get('result') or {}
                if old['status'] != 'succeeded' or not result.get('report_ids'):
                    continue
                try:
                    prior = [store.get_analysis(aid, task.session_id) for aid in result.get('analysis_ids', [])]
                    sources = list(dict.fromkeys(ref['source_id'] for analysis in prior for ref in analysis.db_refs))
                    if not sources:
                        break
                    tables = {source: names for source, names in old['spec'].get('database_tables', {}).items() if source in sources}
                    updated = task.model_copy(update={'database_source_ids': sources, 'database_tables': tables})
                    store.update_task(updated.task_id, spec=updated.model_dump())
                    self.task = task = updated
                    self._revision_auto_source_restore = True
                except StoreError:
                    pass
                break
        # Services normalizes clarification inheritance into TaskSpec. Reading
        # parent.result again would undo an explicit analysis_ids=[] override.
        # Recover only successful, same-session prior analysis references.
        for tid in store.get_session(task.session_id).get('tasks', [])[-20:]:
            if tid == task.task_id:
                continue
            old = store.get_task(tid)
            if old['status'] != 'succeeded':
                continue
            for aid in (old.get('result') or {}).get('analysis_ids', []):
                try:
                    prior = store.get_analysis(aid, task.session_id)
                    if task.file_ids and not set(prior.input_versions).issubset(task.file_ids):
                        continue
                    if task.database_source_ids and not task.file_ids and prior.input_versions:
                        continue
                    self.authorize_analysis(aid)
                except StoreError:
                    continue
        inherited_revision = task.constraints.get('report_revision')
        if inherited_revision:
            if not isinstance(inherited_revision, dict) or not inherited_revision.get('base_report_id'):
                raise StoreError('报告修订计划无效。', 422, 'invalid_revision_plan')
            self.prepare_revision(inherited_revision['base_report_id'],
                targets=inherited_revision.get('targets', []),
                change_request=inherited_revision.get('change_request'), inherited=inherited_revision)

    def check(self):
        if self.cancel_check():
            raise InterruptedError('Task cancellation requested')

    def terminal_result(self):
        """Only application-validated delivery or an explicit question ends work.

        This is separate from model finish_task declarations: missing metrics,
        prose revisions and unregistered files never constitute delivery.
        """
        with self.lock:
            if self.clarification:
                return {'status': 'waiting_user', 'question': self.clarification}
            if self.analysis_failure:
                return {'status': 'failed', 'message': self.analysis_failure, 'analysis_ids': list(self.analyses)}
            if self.reports:
                return {'status': 'completed', 'analysis_ids': list(dict.fromkeys(self.report_analysis_ids)),
                        'report_ids': list(self.reports), 'limitations': list(self.report_limitations)}
            if self.scalar_batch_pending or self.scalar_batch_incomplete:
                return None
            if self.direct_metric_answer is not None and not self.report_required():
                return {'status': 'completed', **self.direct_metric_answer}
            if self.finished_intent == 'analysis' and self.answer_metric_refs and not self.report_required():
                return {'status': 'completed', 'analysis_ids': list(dict.fromkeys(ref.analysis_id for ref in self.answer_metric_refs))}
        return None

    def report_required(self):
        return bool(self.revision_state or self.followup_report_edit or _report_requested(self.task, self.finished_intent))

    def consume(self, role: str, name: str):
        self.check()
        with self.lock:
            if self.calls >= self.store.settings.max_tool_calls:
                raise BudgetExceeded(f'任务已达到配置的工具调用上限（{self.store.settings.max_tool_calls} 次）。可提高 MAX_TOOL_CALLS 后重新运行；已完成的产物仍保留。')
            self.calls += 1
            count = self.calls
        self.event('tool', f'{role} 正在执行 {name}', role=role, tool=name, tool_calls=count)

    def authorize_analysis(self, aid):
        analysis = self.store.get_analysis(aid, self.task.session_id)
        if analysis.status != 'completed':
            raise StoreError('Only completed analyses may be reused', 422)
        for ref in analysis.db_refs:
            self.database.authorize_snapshot(self.task, ref)
        for artifact_id in analysis.table_ids + analysis.chart_ids + analysis.script_ids:
            self.store.resolve_artifact(artifact_id)
        if aid not in self.allowed_analysis_ids:
            self.allowed_analysis_ids.append(aid)
        return analysis

    def selected(self, file_id):
        if file_id not in self.task.file_ids:
            raise StoreError('文件未获本任务选择授权，请先select_files或追问。', 422, 'file_not_selected')
        return self.store.get_file(file_id, self.task.session_id)

    def safe(self, function, *args, **kwargs):
        self.check()
        try:
            result = function(*args, **kwargs)
            self.check()
            return result
        except (InterruptedError, BudgetExceeded):
            raise
        except Exception as exc:
            self.last_error = redact(exc, self.store)
            code = redact(getattr(exc, 'code', type(exc).__name__), self.store)[:100]
            self.event('tool_error', self.last_error[:1000],
                       role=getattr(self.tool_context, 'role', 'unknown'),
                       tool=getattr(self.tool_context, 'name', getattr(function, '__name__', 'tool')),
                       code=code)
            return {'status': 'error', 'code': code, 'message': self.last_error}

    def analysis_summary(self, analysis, *, details=False):
        summary = analysis.model_dump()
        if context_enabled(self) and not details:
            summary['metric_definitions'] = {key: {k: v for k, v in definition.items()
                if k in {'operation', 'definition', 'unit', 'table', 'filters', 'group_by', 'aggregations'}}
                if isinstance(definition, dict) else definition
                for key, definition in analysis.metric_definitions.items()}
            summary['table_previews'] = []
            summary['details_available'] = True
        else:
            summary['table_previews'] = [self.data.read_result_table(aid, self.task.session_id, limit=8) for aid in analysis.table_ids[:3]]
        return summary

    def record_analysis(self, analysis):
        if analysis.status != 'completed' or not analysis.metrics or not (analysis.input_versions or analysis.db_refs):
            raise StoreError('Analysis did not produce validated metrics and source versions', 422, 'invalid_analysis')
        self.authorize_analysis(analysis.analysis_id)
        with self.lock:
            if analysis.analysis_id not in self.analyses:
                self.analyses.append(analysis.analysis_id)
            if self.revision_state and analysis.analysis_id not in self.revision_state['base_analysis_ids']:
                added = self.revision_state.setdefault('supplemental_analysis_ids', [])
                if analysis.analysis_id not in added:
                    added.append(analysis.analysis_id)
                self._persist_revision()
        return self.analysis_summary(analysis)

    def database_catalog(self, source_id):
        if source_id not in self.task.database_source_ids:
            raise StoreError('数据库来源未获本任务授权。', 403, 'database_source_not_selected')
        result = dict(self.database.catalog(source_id))
        if source_id in self.task.database_tables:
            allowed = {name if '.' in name else 'olist.' + name for name in self.task.database_tables[source_id]}
            result['tables'] = [table for table in result['tables'] if table['qualified_name'] in allowed]
        return result

    def database_inputs(self):
        refs = {}
        # Prefer this turn's actual query outputs; do not mount every historical
        # snapshot merely because it remains accessible in the session.
        ids = self.analyses or ((self.revision_state or {}).get('base_analysis_ids')) or self.task.analysis_ids or self.allowed_analysis_ids[-12:]
        for aid in list(ids):
            analysis = self.authorize_analysis(aid)
            for ref in analysis.db_refs:
                refs[ref['query_id']] = ref
        return list(refs.values())

    def prepare_revision(self, report_id=None, *, targets=None, change_request=None, inherited=None, pin=True):
        """Pin an immutable base and task-scoped change request across expert calls.

        Database restoration is allowed only for an implicit selection. Explicit
        clearing or narrowing still goes through the normal snapshot authority.
        The full report service validates the exact delivered version and hash.
        """
        prior_state = self.revision_state
        # "结论" is the rendered summary panel; findings is a distinct panel.
        # Complete that explicitly requested dependency BEFORE supplements pin
        # the scope. Never enlarge a plan after a supplement request exists.
        text = positive_clauses(self.task.request)
        summary_update = re.search(
            r'(?:同步|同时).{0,8}(?:更新|修改|调整).{0,8}(?:结论|摘要)|'
            r'(?:更新|修改|调整).{0,8}(?:结论|摘要).{0,16}(?:对应|合计|指标|总额|数值|统计)', text)
        if targets is not None and summary_update and not (prior_state or {}).get('supplement_request'):
            targets = list(targets)
            if not re.search(r'发现|\bfindings\b',text,re.I):
                targets = [target for target in targets if target != 'findings']
            if 'summary' not in targets: targets.append('summary')
        rid = report_id or (prior_state or {}).get('base_report_id') or (
            self.prior_reports[0]['report_id'] if self.prior_reports else None)
        if rid is None:
            raise StoreError('当前会话没有可修订的已生成报告。', 422, 'missing_report')
        different_base = bool(prior_state and rid != prior_state['base_report_id'])
        if different_base and any(prior_state.get(key) for key in ('targets', 'patch', 'supplement_request', 'supplemental_analysis_ids')):
            raise StoreError('修订基准和目标已锁定，不能在处理过程中切换版本；请继续原计划。', 422, 'revision_base_changed')
        if (prior_state and prior_state.get('supplement_request') and targets is not None and
                set(targets) != set(prior_state.get('targets', []))):
            raise StoreError('正在补充目标分析，不能扩大或更换修订目标。', 422, 'revision_plan_changed')
        artifact = self.store.get_artifact(rid)
        source_task = self.store.get_task(artifact.task_id)
        if (artifact.session_id != self.task.session_id or artifact.kind != 'report' or
                source_task['status'] != 'succeeded' or rid not in (source_task.get('result') or {}).get('report_ids', [])):
            raise StoreError('只能以当前会话已交付的报告为修订基准。', 404, 'not_found')
        self.store.resolve_artifact(rid)
        source_analyses = [self.store.get_analysis(aid, self.task.session_id)
                           for aid in (source_task.get('result') or {}).get('analysis_ids', [])]
        if ((not self.task.database_source_ids or self._revision_auto_source_restore) and
                not self.task.constraints.get('database_sources_explicit') and
                not self.task.constraints.get('database_tables_explicit')):
            sources = list(dict.fromkeys(ref['source_id'] for a in source_analyses for ref in a.db_refs))
            tables = {source: names for source, names in source_task['spec'].get('database_tables', {}).items() if source in sources}
            updated = self.task.model_copy(update={'database_source_ids': sources, 'database_tables': tables})
            self.store.update_task(updated.task_id, spec=updated.model_dump())
            self.task = updated
            self._revision_auto_source_restore = True
        base = importlib.import_module('src.reporting').ReportService(self.store).read_report(self.task, rid)
        self.report_capsule = base
        if not pin:
            return base
        previous = inherited or (prior_state if not different_base else None)
        if previous and previous.get('base_report_hash') != base['hash']:
            raise StoreError('报告修订基准已变化，不能继续旧计划。', 409, 'stale_revision_plan')
        if targets is not None and (not isinstance(targets, list) or len(targets) > 100 or
                any(not isinstance(target, str) or not target.strip() or len(target) > 32767 for target in targets)):
            raise StoreError('修订目标必须为稳定组件ID或完整章节标题列表。', 422, 'invalid_revision_plan')
        legal_targets = {'title', 'summary', 'findings', 'limitations', 'quality', 'charts:add', 'tables:add', 'sections:add'}
        legal_targets.update('layout:' + key for key in base['presentation']['layout'])
        legal_targets.update('section:' + section['heading'] for section in base['presentation'].get('sections', []))
        for kind, plural in (('chart', 'charts'), ('table', 'tables')):
            legal_targets.update(kind + ':' + item['component_id'] for item in base.get('component_state', {}).get(plural, []))
        # Legacy bar-edit selectors are their original chart ids, which are
        # also stable component ids in the general component state.
        effective_targets = targets if targets is not None else ([] if different_base else (prior_state or inherited or {}).get('targets', []))
        if any(target not in legal_targets for target in effective_targets):
            raise StoreError('修订目标未唯一定位，请使用read_report中的chart:/table:稳定组件ID、section:完整章节标题或summary等字段。',
                             422, 'revision_target_not_found')
        state = dict((None if different_base else prior_state) or inherited or {})
        state.update(base_report_id=base['report_id'], base_report_hash=base['hash'],
                     base_version=base['version'], base_analysis_ids=base['analysis_ids'],
                     change_request=change_request or state.get('change_request') or self.task.request,
                     targets=list(dict.fromkeys(effective_targets)))
        self.revision_state = state
        self.followup_report_edit = True
        for aid in base['analysis_ids']:
            self.authorize_analysis(aid)
        self._persist_revision()
        self.event('report_revision_prepared', '已锁定报告版本，后续仅修订指定内容',
                   role=getattr(self.tool_context, 'role', 'main'), report_id=rid,
                   base_hash=base['hash'], targets=state['targets'])
        return {**base, 'revision_plan': dict(state)}

    def _persist_revision(self):
        constraints = dict(self.task.constraints)
        constraints['report_revision'] = json.loads(json.dumps(self.revision_state, ensure_ascii=False))
        self.task = self.task.model_copy(update={'constraints': constraints})
        self.store.update_task(self.task.task_id, spec=self.task.model_dump())

    def record_sql(self, result):
        snapshot = self.database.authorize_snapshot(self.task, result['db_ref'])
        metrics = snapshot['scalar_metrics'] or {'result_rows': snapshot['row_count']}
        definitions = {key: {'operation': 'sql', 'source_id': snapshot['source_id'],
            'query_id': snapshot['query_id'], 'snapshot_id': snapshot['snapshot_id'],
            'definition': '完整只读SQL结果的列值' if snapshot['scalar_metrics'] else '完整SQL结果行数（非业务指标）',
            'sql_artifact_id': snapshot['sql_artifact_id'], 'queried_at': snapshot['queried_at']}
            for key in metrics}
        findings = ([f'{key}为{value}。' for key, value in metrics.items()]
                    if snapshot['scalar_metrics'] else [])
        analysis = AnalysisResult(task_id=self.task.task_id, session_id=self.task.session_id,
            db_refs=[snapshot['db_ref']], metrics=metrics, metric_definitions=definitions,
            table_ids=[snapshot['result_artifact_id']],
            findings=findings, limitations=['数据库结果为查询时刻的完整快照，采用SQL中明示的筛选与统计口径。'])
        return self.record_analysis(self.store.save_analysis(analysis))

    def request_supplement(self, request: str) -> dict:
        with self.lock:
            self.report_rounds += 1
            if self.report_rounds > 2:
                self.clarification = '报告所需信息仍不完整，请明确补充分析范围。'
                return {'status': 'waiting_user', 'question': self.clarification}
            result = {'status': 'need_analysis', 'supplement_request': request, 'round': self.report_rounds}
            if self.revision_state:
                self.revision_state['supplement_request'] = request
                self._persist_revision()
                result['revision_plan'] = dict(self.revision_state)
                result['next_step'] = '只补充此计划目标所需的分析，再交报告专家用原base_report_id及revision_patch修订；不要新建默认报告。'
            self.pending_handoff = result
            return result

    def report_validation(self, status: str, reason_code: str, message: str):
        self.event('report_validation', redact(message, self.store)[:1000],
                   role='report_designer', tool='render_report', status=status, reason_code=reason_code)

    def request_revision(self, reason_code: str, message: str) -> dict:
        with self.lock:
            self.report_revisions += 1
            count = self.report_revisions
        self.report_validation('needs_revision', reason_code, message)
        if self.revision_state:
            return {'status': 'needs_revision', 'reason_code': reason_code, 'revision': count,
                    'message': redact(message, self.store), 'revision_plan': dict(self.revision_state),
                    'next_step': '仅修正计划中目标文案/结构或绑定指标，保留原base和其它内容；不能用默认模板丢掉指定修改。'}
        return {'status': 'needs_revision', 'reason_code': reason_code, 'revision': count,
                'message': redact(message, self.store),
                'next_step': '仅修正文案/结构，复用已有分析，不要request_analysis。可直接narrative=null使用可信默认模板。' if count < 2 else '文案修正次数已达上限；下一次使用narrative=null默认模板，不再重做统计。'}

    def default_report_template(self):
        warning = '自定义报告文案未通过校验，已使用默认报告模板；所需已验证指标、表格和图表仍保留。'
        if warning not in self.report_limitations:
            self.report_limitations.append(warning)
            self.report_validation('default_template', 'default_template_fallback', warning)

    def _file_schema(self, fid: str) -> dict:
        """Compact per-file schema for the APP_STATE ledger.

        Columns are capped at 30; analysts retrieve the full list through
        python_inputs, so every model call only needs the identifying head.
        """
        cached = self._file_schema_cache.get(fid)
        if cached is None:
            item = self.store.get_file(fid, self.task.session_id)
            tables = []
            for p in (item.get('profiles') or ([item['profile']] if item.get('profile') else [])):
                columns = list(p.get('columns', []))[:30]
                dtypes = p.get('dtypes', {})
                tables.append({'table': p.get('sheet_or_table'), 'columns': columns,
                    'dtypes': {c: dtypes[c] for c in columns if c in dtypes},
                    'row_count': p.get('row_count'),
                    'quality_warnings': p.get('quality_warnings', [])[:8],
                    'columns_truncated': len(p.get('columns', [])) > 30})
            cached = {'file_id': fid, 'name': item['name'],
                'python_path': f'/input/{fid}{Path(item["name"]).suffix.lower()}', 'tables': tables}
            self._file_schema_cache[fid] = cached
        return cached

    def progress_context(self) -> dict:
        """Application-owned ledger, without prompts, raw data or private memory."""
        with self.lock:
            ids = list(self.allowed_analysis_ids)[-12:]
            if context_enabled(self):
                relevant = [*self.task.analysis_ids, *self.analyses,
                            *((self.revision_state or {}).get('base_analysis_ids', []))]
                ids = list(dict.fromkeys(relevant)) or ids[-3:]
            remaining = self.store.settings.max_tool_calls - self.calls
        completed = []
        for aid in ids:
            analysis = self.authorize_analysis(aid)
            definition = next(iter(analysis.metric_definitions.values()), {})
            completed.append({'analysis_id': aid, 'file_ids': list(analysis.input_versions),
                'database_source_ids': list(dict.fromkeys(ref['source_id'] for ref in analysis.db_refs)),
                'metrics': analysis.metrics, 'operation': definition.get('operation'),
                'group_by': definition.get('group_by'), 'aggregations': definition.get('aggregations'),
                'table_ids': analysis.table_ids, 'chart_ids': analysis.chart_ids})
        return {'remaining_tool_calls': remaining, 'completed_analyses': completed,
                'python_attempts_remaining': max(0, 3-self.python_attempts),
                'analysis_failure': self.analysis_failure,
                'report_handoff_due': bool(self.analyses and remaining <= 6 and self.report_required()),
                'report_ids': list(self.reports), 'expected_outputs': self.task.expected_outputs,
                'available_reports': self.prior_reports[:8],
                'report_revision_plan': revision_digest(self.revision_state) if context_enabled(self) else self.revision_state,
                'latest_report_id': self.prior_reports[0]['report_id'] if self.prior_reports else None,
                'selected_database_source_ids': list(self.task.database_source_ids),
                'selected_database_tables': dict(self.task.database_tables),
                'selected_file_schemas': [self._file_schema(fid) for fid in self.task.file_ids]}


