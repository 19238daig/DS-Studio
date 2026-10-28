"""Task-local context controls for the audited Token Optimization workflow.

The full history stays in Deep Agents' task-scoped backend; these controls do
not remove source validation, change permissions, or cache another task's graph.
Native middleware replacement and ModelRequest.override are documented at
https://docs.langchain.com/oss/python/deepagents/context-engineering .
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from langchain.agents.middleware import AgentMiddleware


def enabled(runtime) -> bool:
    return getattr(runtime.store.settings, 'context_policy', 'off') == 'enforce'


def tool_name(value):
    if isinstance(value, Mapping):
        return value.get('name') or value.get('function', {}).get('name', '')
    return getattr(value, 'name', '')


def tool_chars(values):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return len(json.dumps([convert_to_openai_tool(value) for value in values], ensure_ascii=False, default=str))


def revision_digest(state):
    if not state:
        return None
    # patch content is already in the matching tool call; repeating it in a
    # new system message on every turn breaks prefix reuse and wastes tokens.
    return {key: state[key] for key in ('base_report_id', 'base_report_hash', 'base_version',
        'base_analysis_ids', 'targets', 'supplemental_analysis_ids', 'supplement_request') if key in state}


def standard_result_report(request: str) -> bool:
    """Only a formatting-only request, never a new subject/chart/style/edit.

    The complete request must match. Novel business requirements cannot be
    discarded by matching a loose substring such as 'generate report'.
    """
    import re
    text = re.sub(r'[\s，。！!,.、：:;；?？]', '', request).casefold()
    chinese = (r'(?:请)?(?:帮我)?(?:(?:根据|基于|使用|把|将)?'
               r'(?:刚才|刚刚|上次|上述|以上|已有|当前|本次)(?:的)?(?:分析结果|分析|结果))?'
               r'(?:生成|制作|导出|整理成|转成|输出)(?:一份|一个)?(?:中文)?'
               r'(?:(?:html|excel|xlsx)(?:和|及|与|/)?)*(?:分析)?报告')
    english = r'(?:please)?(?:generate|create|export)(?:a)?(?:(?:html|excel|xlsx)(?:and|/)?)*(?:analysis)?report(?:from(?:the)?(?:previous|above|existing)results)?'
    return bool(re.fullmatch(chinese, text) or re.fullmatch(english, text))


class ContextPolicyMiddleware(AgentMiddleware):
    """Expose the relevant role's thin tool menu, while retaining backend guards.

    Full legacy tools remain in the ToolNode for compatibility and recovery.
    Filtering is an efficiency measure, never an authorization boundary.
    """
    def __init__(self, runtime, role):
        self.runtime, self.role = runtime, role

    def wrap_model_call(self, request, handler):
        mode = getattr(self.runtime.store.settings, 'context_policy', 'off')
        if mode == 'off':
            return handler(request)
        names = {tool_name(value) for value in request.tools}
        common = {'ask_user', 'search_kb', 'memory_search', 'memory_propose', 'read_file'}
        if self.role == 'main':
            keep = common | {'query_metric', 'query_database_metric', 'perform_task', 'finish_task',
                            'select_files', 'select_database_sources', 'file_catalog', 'database_sources',
                            'available_analyses', 'prepare_report_revision'}
            if not getattr(self.runtime.store.settings, 'workflow_fast_paths', False):
                keep.add('task')
        elif self.role == 'analyst':
            keep = common | {'answer_metric', 'analyze_dataset', 'authorized_files', 'profile_dataset',
                             'database_catalog', 'run_sql', 'run_sql_batch', 'python_inputs',
                             'run_python', 'read_result_table', 'read_source_text', 'prepare_material', 'finish_analysis', 'answer_sql'}
        else:
            keep = common | {'read_report', 'read_analysis', 'read_analyses', 'read_result_table',
                             'prepare_report_revision', 'request_analysis', 'report_options'}
            keep.add('apply_report_edit' if (self.runtime.revision_state or self.runtime.followup_report_edit)
                     else 'create_report')
        chosen = [value for value in request.tools if tool_name(value) in keep]
        if not chosen:  # Never accidentally hide all tools from an unknown API shape.
            chosen = request.tools
        before, after = tool_chars(request.tools), tool_chars(chosen)
        self.runtime.event('context_payload', '已统计角色输入与工具定义', role=self.role,
            policy=mode, tool_count_before=len(request.tools), tool_count_after=len(chosen),
            tool_schema_chars_before=before, tool_schema_chars_after=after,
            system_chars=len(str(request.system_message.content)) if request.system_message else 0,
            message_chars=sum(len(str(getattr(m, 'content', ''))) for m in request.messages),
            units='unicode_characters_not_provider_tokens')
        if mode == 'enforce':
            request = request.override(tools=chosen)
        return handler(request)


def deterministic_summary(runtime, role, messages):
    """A lossless-reference checkpoint; no extra summarization model request.

    The original messages are offloaded by native SummarizationMiddleware.
    The current goal and exact revision/source identifiers are never shortened.
    Additional details remain retrievable through the existing authorized tools.
    """
    state = runtime.progress_context()
    checkpoint = {'request': runtime.task.request,
                  'delegated_request': runtime.role_requests.get(role),
                  'current_state': state,
                  'note': 'Earlier messages were offloaded by Deep Agents. Retrieve authorized source details only when needed.'}
    runtime.event('context_compacted', '已生成带来源引用的任务检查点，未调用额外摘要模型',
                  role=role, message_count=len(messages), added_model_calls=0)
    return json.dumps(checkpoint, ensure_ascii=False, default=str)
