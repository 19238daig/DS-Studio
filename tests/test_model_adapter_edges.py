"""Additional independent offline regression checks for Phase 3."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, run_task
from src.config import Settings
from src.data import DataService
from src.memory import MemoryService
from src.schemas import MessageRequest, TaskSpec
from src.services import Services
from src.storage import Store, StoreError
from test_agents import ScriptedModel, call, models


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(_env_file=None, workspace=tmp_path, api_key='offline-review-secret'))
    sid = store.new_session()['session_id']
    path = tmp_path / 'KB' / 'sample.csv'
    path.write_text('value\n2\n3\n', encoding='utf-8')
    fid = store.register_file(path, path.name, None, source='kb')['file_id']
    task = TaskSpec(session_id=sid, request='Compute mean value', file_ids=[fid])
    store.create_task(task)
    return store, task, fid


@pytest.mark.parametrize('prompt', [
    '不要帮我生成报告，只计算value均值。',
    '不生成HTML报告，只计算value均值。',
    '不要生成任何报告，只计算value均值。',
    '无需出具报告，只计算value均值。',
    "Don't generate a report, calculate average value only.",
])
def test_explicit_no_report_still_completes_real_analysis(context, prompt):
    store, task, fid = context
    task.request = prompt
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Calculate the mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '已完成计算。'}), AIMessage(content='done'),
    ], [call('analyze_dataset', {'file_id': fid, 'operation': 'aggregate', 'aggregations': {'value': 'mean'}}), AIMessage(content='done')])
    result = run_task(task, store, lambda *a, **k: None, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['analysis_ids'] and result['report_ids'] == []
    assert store.get_analysis(result['analysis_ids'][0], task.session_id).metrics['value_mean'] == 2.5


@pytest.mark.parametrize('prompt', [
    '你能生成报告吗？', '如何生成报告？', '介绍一下制作报告的功能。',
    'Can you generate a report?', 'How do you generate reports?',
])
def test_report_capability_questions_finish_without_fake_deliverable(context, prompt):
    store, task, _ = context
    task.file_ids = []
    task.request = prompt
    _, factory = models([call('finish_task', {'intent': 'general', 'answer': '支持生成HTML和Excel报告。'}), AIMessage(content='done')])
    result = run_task(task, store, lambda *a, **k: None, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['analysis_ids'] == result['report_ids'] == []


def waiting_parent(context):
    store, task, fid = context
    first = DataService(store).analyze(task, fid, 'aggregate', aggregations={'value': 'mean'})
    second = DataService(store).analyze(task, fid, 'aggregate', aggregations={'value': 'max'})
    task.analysis_ids = [first.analysis_id]
    task.expected_outputs = ['html', 'xlsx']
    store.update_task(task.task_id, spec=task.model_dump(), status='waiting_user', result={'analysis_ids': [second.analysis_id]}, final_response='请选择受众。')
    return first, second


def test_clarification_omission_inherits_union_and_formats(context):
    store, task, _ = context
    first, second = waiting_parent(context)
    reply = Services(store).submit(task.session_id, MessageRequest(content='管理层。', reply_to_task_id=task.task_id))
    spec = TaskSpec.model_validate(reply['spec'])
    assert set(spec.analysis_ids) == {first.analysis_id, second.analysis_id}
    assert spec.expected_outputs == ['html', 'xlsx']
    runtime = TaskRuntime(spec, store, lambda *a, **k: None, lambda: False)
    assert set(runtime.allowed_analysis_ids) == {first.analysis_id, second.analysis_id}


def test_clarification_explicit_empty_clears_analyses_and_formats(context):
    store, task, _ = context
    waiting_parent(context)
    reply = Services(store).submit(task.session_id, MessageRequest(content='重新计算，不要报告。', reply_to_task_id=task.task_id,
                                    analysis_ids=[], expected_outputs=[]))
    spec = TaskSpec.model_validate(reply['spec'])
    assert spec.analysis_ids == spec.expected_outputs == []
    assert TaskRuntime(spec, store, lambda *a, **k: None, lambda: False).allowed_analysis_ids == []


def test_clarification_stale_parent_analysis_is_not_silently_reused(context):
    store, task, fid = context
    waiting_parent(context)
    path = store.resolve_file(fid, task.session_id)
    path.write_text('value\n99\n', encoding='utf-8')
    with pytest.raises(StoreError):
        Services(store).submit(task.session_id, MessageRequest(content='管理层。', reply_to_task_id=task.task_id))


def test_cancellation_after_model_returns_interrupts_without_success(context):
    store, task, _ = context
    cancelled = [False]

    class CancellingModel(ScriptedModel):
        def _generate(self, *a, **k):
            cancelled[0] = True
            return super()._generate(*a, **k)

    role_models = {
        role: CancellingModel(model_name='offline-' + role, responses=[AIMessage(content='done')])
        for role in ('main', 'analyst', 'report_designer')
    }
    with pytest.raises(InterruptedError):
        run_task(task, store, lambda *a, **k: None, lambda: cancelled[0], model_factory=lambda _, role: role_models[role])
    assert MemoryService(store).list('analyst') == []
