"""Module-level helpers shared by the agent graph: request classification,
delivery answers, redaction, provider retry and budget errors.

Split out of src/agents.py; src/agents.py re-exports these names so existing
imports keep working."""
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


def _validation_boundary(store, task=None):
    if not getattr(store.settings, 'auth_dedup_enabled', False):
        return nullcontext()
    from src.validation import validation_scope
    return validation_scope(store, task, fresh=True)


def _span(store, task_id, name, role=None):
    if not getattr(store.settings, 'perf_trace_enabled', False):
        return nullcontext()
    from src.performance import span
    return span(store, task_id, name, role=role)


def _initialization_boundary(function):
    @wraps(function)
    def initialize(self, task, store, event, cancel_check):
        with _span(store, task.task_id, 'runtime_init'), _validation_boundary(store, task):
            return function(self, task, store, event, cancel_check)
    return initialize


def redact(value: Any, store: Store) -> str:
    # A long-lived worker may hold a pre-upgrade Settings instance while
    # importing updated tools; getattr inside redact_text preserves the failure.
    return redact_text(value, getattr(store, 'settings', None), limit=4000)


PROVIDER_FAILURE_MARKERS = (
    'litellm', 'internalerror', 'openaiexception', 'apitimeout', 'apiconnection', 'ratelimit',
    'rate limit', 'team_model_access_denied', 'connection error', 'timed out', 'permissiondenied',
    'service unavailable', 'bad gateway',
)

# Transient model-service failures worth one application-level retry: the
# SDK's own retries cover 429/5xx/connect, these markers catch proxied dumps.
TRANSIENT_CALL_MARKERS = (
    'timed out', 'timeout', 'connection error', 'connection reset', 'apiconnection',
    'rate limit', 'ratelimit', 'error code: 429', 'error code: 500', 'error code: 502',
    'error code: 503', 'error code: 504', 'internal server error', 'service unavailable',
    'overloaded', 'temporarily unavailable',
)


def _transient_model_failure(exc: BaseException) -> bool:
    low = str(exc).lower()
    return any(marker in low for marker in TRANSIENT_CALL_MARKERS)


MODEL_RETRY_BACKOFF_SECONDS = (2, 8, 20)


def _model_call_with_retry(runtime, role: str, call_id: str, handler, request):
    """Patient app-level retries for transient model-service failures.

    A single 429/timeout/connection blip after the SDK retries would otherwise
    kill the whole task mid-graph (there is deliberately no duplicate model
    retry elsewhere). The escalating backoff bridges ~30s of proxy instability;
    cancellation/timeout checks still run between attempts.
    """
    for attempt, backoff in enumerate(MODEL_RETRY_BACKOFF_SECONDS, start=1):
        try:
            return handler(request)
        except Exception as exc:
            if attempt == len(MODEL_RETRY_BACKOFF_SECONDS) or not _transient_model_failure(exc):
                raise
            runtime.event('model_retry', f'模型服务瞬时故障，{backoff} 秒后进行第 {attempt + 1} 次尝试',
                          role=role, call_id=call_id, backoff_seconds=backoff)
            time.sleep(backoff)
            runtime.check()


def friendly_failure(value: Any) -> str:
    """User-facing failure text: provider/internal dumps become actionable
    Chinese; application StoreError wording passes through unchanged. The raw
    text stays available in the audit journal events."""
    text = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[REDACTED]', str(value))[:4000]
    low = text.lower()
    if any(marker in low for marker in PROVIDER_FAILURE_MARKERS) or re.search(r'error code: [45]\d\d', low):
        return ('模型服务调用失败，可能是服务商临时故障或请求被拒绝；'
                '已完成的分析和报告均已保留，请稍后重试，或在工作台设置中更换模型。')
    return text


class BudgetExceeded(RuntimeError):
    pass


def _report_candidates(store: Store, task: TaskSpec) -> list[dict]:
    """A descriptive same-session catalog; opening a version still reauthorizes it."""
    versions = []
    for tid in store.get_session(task.session_id).get('tasks', []):
        if tid == task.task_id:
            continue
        old = store.get_task(tid)
        if old['status'] != 'succeeded':
            continue
        groups = {}
        for rid in (old.get('result') or {}).get('report_ids', []):
            try:
                artifact = store.get_artifact(rid)
                if artifact.kind != 'report' or artifact.session_id != task.session_id or artifact.task_id != tid:
                    continue
                # This is a descriptive catalog, not a read authorization. The
                # selected report is hash-checked in prepare_revision/read_report.
                # Avoid rehashing every old HTML/XLSX merely to list its name.
                path = (store.safe_path(artifact.relative_path) if
                        getattr(store.settings, 'context_policy', 'off') == 'enforce' else
                        store.resolve_artifact(rid))
                group = groups.setdefault(str(path.parent), dict(report_id=rid, task_id=tid,
                    formats=[], request=old['spec']['request'][:300], title='', version=path.parent.name,
                    revision_of=None))
                fmt = path.suffix.lstrip('.')
                group['formats'].append(fmt)
                if fmt == 'html':
                    group['report_id'] = rid
                manifests = [a for a in store.list_artifacts(tid) if a.kind == 'report_manifest'
                             and Path(a.relative_path).parent == Path(artifact.relative_path).parent]
                if len(manifests) == 1:
                    manifest = json.loads(store.resolve_artifact(manifests[0].artifact_id).read_text(encoding='utf-8'))
                    if manifest.get('task_id') == tid and manifest.get('session_id') == task.session_id:
                        group.update(title=(manifest.get('presentation') or {}).get('title', ''),
                                     version=manifest.get('report_version', path.parent.name),
                                     revision_of=manifest.get('revision_of'))
            except (StoreError, OSError, ValueError):
                continue
        versions[0:0] = list(reversed(list(groups.values())))
    return versions



def _markdown_table(columns, rows):
    def cell(value):
        text = str(value) if value is not None else '缺失'
        return text.replace('|', '\\|').replace('\n', ' ')[:180]
    return '\n'.join(['| ' + ' | '.join(cell(c) for c in columns) + ' |',
                      '| ' + ' | '.join('---' for _ in columns) + ' |'] +
                     ['| ' + ' | '.join(cell(row.get(c)) for c in columns) + ' |' for row in rows])


def _data_answer(runtime: TaskRuntime, ids: list[str]) -> str:
    if runtime.direct_metric_answer is not None and not runtime.reports:
        return runtime.direct_metric_answer['final_response']
    refs = validate_refs(runtime, runtime.answer_metric_refs) or automatic_refs(runtime, ids)
    parts = []
    if refs:
        # An explicit natural conclusion can bind every value to the ledger.
        # Plain unbound model text is never a reason to print all stored data.
        parts.append(runtime.answer_template or render_metrics(runtime, refs))
    table_requested, method_requested = wants_tables(runtime.task.request), wants_method(runtime.task.request)
    for aid in ids:
        analysis = runtime.authorize_analysis(aid)
        if table_requested:
            for tid in analysis.table_ids[:2]:
                preview = runtime.data.read_result_table(tid, runtime.task.session_id, limit=answer_table_limit(runtime.task.request))
                if preview['preview']:
                    parts.append(_markdown_table(preview['columns'], preview['preview']))
                    if preview['total_rows'] > len(preview['preview']):
                        parts.append('完整明细见任务产物。')
        if method_requested:
            for ref in refs:
                if ref.analysis_id != aid:
                    continue
                definition = analysis.metric_definitions.get(ref.metric_key, {})
                if isinstance(definition, dict):
                    text = definition.get('definition') or definition.get('scope')
                    if text:
                        parts.append('口径：' + str(text))
            if analysis.limitations:
                parts.append('局限：' + '；'.join(analysis.limitations))
    if not parts:
        # Metadata-only analyses (describe/rows previews) have no bindable
        # business metric: answer with the structure summary instead of a
        # dead-end failure.
        meta_parts = []
        for aid in ids:
            analysis = runtime.authorize_analysis(aid)
            rows = analysis.metrics.get('source_rows') or analysis.metrics.get('result_rows')
            meta_parts.append(f'已完成所选数据的基础概览（{rows if rows is not None else "结构"}行级信息）')
        return ('；'.join(meta_parts) +
                '。请进一步指定统计口径（如均值、分布、分组对比），我会据此计算并绑定结论。')
    return '\n\n'.join(dict.fromkeys(parts))


def _revision_language(request: str) -> bool:
    """Recognize an edit speech act, not a closed list of supported examples.

    Semantic routing still belongs to the main Agent, which can explicitly pin
    a report. This bounded fallback only recovers clear contextual instructions.
    Ordinary statistical questions remain ordinary questions.
    """
    text = positive_clauses(request)
    if re.search(r'从头|全新报告|整体重做|重新分析全部|请记住|记住.*偏好|保存.*偏好|以后.*默认|\b(?:from scratch|brand.new report|remember)\b', text, re.I):
        return False
    question = re.search(r'如何|怎么|怎样|介绍|解释|多少|几笔|几个|有什么|是什么|为什么|是否|能否|能不能|可否|\b(?:how|what|why|can\s+you)\b', text, re.I)
    if question:
        return False
    return bool(re.search(
        r'(?:修改|修订|翻译|删除|移除|调整|改|换|删|译|精简|简短|缩短|重排|挪到|移到|放到|'
        r'加一节|加一段|增加一节|新增|补充|增加|只保留|仅保留|只统计|仅统计|revise|translate|edit|replace|remove|shorten|reorder)', text, re.I))


def _report_requested(task: TaskSpec, intent: str | None, *, has_prior_report: bool = False) -> bool:
    """Prefer declared semantics; guard explicit deliverables without keyword routing.

    Mentions in capability questions or negated instructions are not requests to
    produce a report. Format selections apply unless explicitly negated now.
    """
    clauses = re.split(r'[，,。.!！?？;；\n]', task.request)
    positive = []
    for clause in clauses:
        if re.search(r'(?:不需要|不必|无需|无须|不要|不用|暂时不|暂不|先不|别|禁止|不)'
                     r'[^，,。.!！?？;；\n]{0,30}报告|'
                     r"\b(?:do\s+not|don['’]t|never|no\s+need\s+to|without|not)\b[^,.;!?\n]{0,70}\breports?\b", clause, re.I):
            continue
        positive.append(clause)
    request = '，'.join(positive)
    # Sidebar format choices may persist from an earlier report. An explicit
    # current-turn "no report" wins unless another clause requests a report.
    if len(positive) < len(clauses) and not re.search(
        r'(?:生成|制作|导出|输出|做|出|修改|修订|翻译|删除|移除|调整|优化)[^，,。！？;；\n]{0,24}报告|'
        r'报告[^，,。！？;；\n]{0,50}(?:修改|修订|翻译|删除|移除|调整|改为|改成)|'
        r'\b(?:generate|create|export|revise|translate)\b[^,.;!?\n]{0,50}\breports?\b', request, re.I):
        return False
    if set(task.expected_outputs) & {'html', 'xlsx'}:
        return True
    if intent == 'report':
        return bool(request.strip()) and (bool(re.search(r'报告|\breports?\b', request, re.I)) or len(positive) == len(clauses))
    # A correctly classified capability explanation/preference must not become
    # a report job just because its wording includes "generate report".
    action = r'(?:生成|制作|导出|输出|做|出|修改|修订|翻译|删除|移除|调整|优化)(?:一份|一个)?[^，,。！？;；\n]{0,24}报告'
    imperative = bool(re.search(r'(?:^|[，,。！;；\n]|请|帮我|替我|并|再|然后)\s*' + action, request))
    imperative = imperative or bool(re.search(r'^(?:please\s+)?(?:generate|create|produce|export)\b[^,.;!?\n]{0,50}\breports?\b', request.strip(), re.I))
    question = bool(re.search(r'如何|怎么|怎样|介绍|解释|需要什么|有什么|能否|能不能|是否|\b(?:how|what|can\s+you|do\s+you)\b', request, re.I))
    edit = bool(re.search(r'报告[^，,。！？;；\n]{0,50}(?:修改|修订|翻译|删除|移除|调整|改为|改成)', request))
    if edit and not question and intent != 'preference':
        return True
    # Follow-up edits often omit "report" entirely. Resolve that reference only
    # when this session has a delivered report; a fresh Top-N question stays QA.
    scoped_edit = re.search(
        r'(?:Top\s*\d+[^，,。!?！？;；\n]{0,30}(?:改|换|调整)|'
        r'(?:改|换|调整|删除|移除|翻译)[^，,。!?！？;；\n]{0,35}'
        r'(?:Top\s*\d+|标题|章节|模块|图表|配色|颜色|字体|字号|图例|摘要|结论)|'
        r'(?:标题|章节|模块|图表|配色|颜色|字体|字号|图例|摘要|结论)[^，,。!?！？;；\n]{0,25}'
        r'(?:改|换|调整|删除|移除|翻译)|'
        r'(?:这一段|这段|那一节|这个标题|那张图|刚才|上一版|原来|原有|其中|里面|第[^，,。]{0,12}[图节段]|其它不变|其他不变|其余不变)[^，,。!?！？;；\n]{0,65}|'
        r'(?:改成|换成|译成|改得|简短|精简|加一节|加一段|增加一节|新增)[^，,。!?！？;；\n]{0,65})', positive_clauses(request), re.I)
    if has_prior_report and scoped_edit and _revision_language(request) and not question and intent != 'preference':
        return True
    if imperative and not question and intent != 'preference':
        return True
    if intent is not None:
        return False
    return not question and bool(re.search(action + r'|报告[^，,。！？;；\n]{0,50}(?:修改|修订|翻译|删除|移除|调整|改为|改成)|\b(?:generate|create|revise|translate)\b[^,.;!?\n]{0,50}\breports?\b', request, re.I))


def _data_requested(task: TaskSpec, intent: str | None) -> bool:
    """Negated/capability mentions do not turn a general turn into a query.

    A wrong general declaration must still never authorize an uncomputed answer
    to a positive statistical question.
    """
    keyword = r'统计|均值|平均|中位数|最大值|最小值|求和|标准差|相关性|筛选|对比|比较|分析.*(?:数据|文件)|行数|\b(?:count|average|median|sum|correlation)\b'
    active = []
    for clause in re.split(r'[，,。.!！?？;；\n]|但是|而是|不过', task.request):
        if re.search(r'(?:不需要|不必|无需|无须|不要|不用|暂时不|暂不|先不|别|禁止|不)[^，,。.!！?？;；\n]{0,30}(?:' + keyword + r')|\b(?:do\s+not|don[’\x27]t|without|no\s+need\s+to)\b[^,.;!?\n]{0,50}(?:analy[sz]|calculat|count|average|median|sum|correlation)', clause, re.I):
            continue
        if re.search(r'介绍|功能|支持什么|能做哪些|能做什么|如何|怎么|怎样|\b(?:capabilit\w*|how\s+(?:do|can)|what\s+can)\b', clause, re.I):
            continue
        active.append(clause)
    if re.search(keyword, '，'.join(active), re.I):
        return True
    if not any(clause.strip() for clause in active):
        return False
    general = intent in {'general', 'preference'} or bool(re.search(
        r'^(你好|谢谢|再见|hello|hi\b)|功能|能做哪些|能做什么|请记住|记住.*偏好|以后.*(?:报告|默认)', task.request, re.I))
    return not general and (intent == 'analysis' or bool(task.file_ids) or bool(task.database_source_ids))


def _preference_requested(task: TaskSpec, intent: str | None) -> bool:
    return intent == 'preference' or bool(re.search(r'请记住|记住.*偏好|保存.*偏好|\bremember\b', task.request, re.I))


def _delivery_gap(runtime: TaskRuntime) -> str | None:
    if runtime.clarification or runtime.reports:
        return None
    if runtime.answer_binding_needed:
        return 'analysis'
    if runtime.last_error:
        return None
    if runtime.followup_report_edit or _report_requested(runtime.task, runtime.finished_intent):
        return 'report'
    if _data_requested(runtime.task, runtime.finished_intent) and not (runtime.answer_metric_refs or runtime.direct_metric_answer):
        if not runtime.analyses or not (automatic_refs(runtime, runtime.analyses) or wants_tables(runtime.task.request)):
            return 'analysis'
    if _preference_requested(runtime.task, runtime.finished_intent) and not runtime.preference_saved:
        return 'preference'
    return None


