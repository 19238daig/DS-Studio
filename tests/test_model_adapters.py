"""Independent offline review of model configuration and Agent capability boundaries."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from deepagents import create_deep_agent

from src.backends import KnowledgeService, role_backend
from src.config import Settings
from src.llm import build_model
from src.memory import MemoryService
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(_env_file=None, workspace=tmp_path, api_key='synthetic-host-secret'))
    sid = store.new_session()['session_id']
    task = TaskSpec(session_id=sid, request='Independent review')
    store.create_task(task)
    return store, sid, task


def runtime():
    return ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None, tool_call_id='review', store=None)


def file_tool(middleware, name, **kwargs):
    tool = next(t for t in middleware.tools if t.name == name)
    return tool.func(runtime=runtime(), **kwargs)


class OfflineToolModel(BaseChatModel):
    """Runs one selected tool call through the real compiled Deep Agent."""
    calls: list[dict]
    answer: str = 'done'

    @property
    def _llm_type(self):
        return 'review-offline'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(content=self.answer) if not self.calls or any(isinstance(x, ToolMessage) for x in messages) else AIMessage(content='', tool_calls=self.calls)
        return ChatResult(generations=[ChatGeneration(message=message)])


def graph_tool(store, sid, role, name, args):
    backend, permissions, middleware = role_backend(store, role, sid)
    graph = create_deep_agent(model=OfflineToolModel(calls=[{'id': 'review', 'name': name, 'args': args}]), backend=backend, permissions=permissions, middleware=[middleware], subagents=[])
    result = graph.invoke({'messages': [{'role': 'user', 'content': 'Execute the offline review tool'}]})
    return next(x.content for x in result['messages'] if isinstance(x, ToolMessage))


def active_memory(context, owner, content):
    store, _, task = context
    memory = MemoryService(store)
    entry = memory.propose(owner, content, task.task_id)
    memory.finalize(task.task_id, success=True, verified=True)
    return entry


def test_model_instances_and_role_configuration(context):
    store, _, _ = context
    models = {r: build_model(store.settings, r) for r in ('main', 'analyst', 'report_designer')}
    assert len({id(x) for x in models.values()}) == 3
    assert models['main'].model_name == store.settings.main_model
    assert models['analyst'].model_name == models['report_designer'].model_name == store.settings.analyst_model
    assert models['main'].extra_body['enable_thinking'] is False
    assert models['analyst'].extra_body == {'enable_thinking': True, 'thinking_budget': 4096}
    assert models['report_designer'].extra_body['enable_thinking'] is False
    assert all(x.use_responses_api is False for x in models.values())


def test_candidate_promotion_requires_success_and_verification(context):
    store, _, task = context
    memory = MemoryService(store)
    memory.propose('analyst', 'Use exact row counts for review.', task.task_id)
    assert memory.search('analyst', 'row') == []
    memory.finalize(task.task_id, success=True, verified=False)
    assert memory.search('analyst', 'row') == []
    assert memory.list('analyst') == []
    next_task = TaskSpec(session_id=task.session_id, request='Next')
    store.create_task(next_task)
    memory.propose('analyst', 'Use exact row counts for review.', next_task.task_id)
    memory.finalize(next_task.task_id, success=False, verified=True)
    assert memory.search('analyst', 'row') == []


def test_memory_persistence_isolation_and_deletion(context):
    store, _, _ = context
    entry = active_memory(context, 'analyst', 'Analyst private marker.')
    memory = MemoryService(Store(store.settings))
    assert memory.search('analyst', 'private')[0]['memory_id'] == entry['memory_id']
    assert memory.search('report_designer', 'private') == []
    with pytest.raises(StoreError, match='not found'):
        memory.delete('report_designer', entry['memory_id'])
    memory.delete('analyst', entry['memory_id'])
    assert memory.search('analyst', 'private') == []
    assert 'Analyst private marker.' not in (memory.summary_root('analyst') / 'AGENTS.md').read_text()


@pytest.mark.parametrize('content', ['API_KEY=synthetic', 'synthetic-host-secret', 'Bearer: sensitive', 'sk-this-is-a-synthetic-token'])
def test_credentials_cannot_be_memorized(context, content):
    store, _, task = context
    with pytest.raises(StoreError, match='Credentials'):
        MemoryService(store).propose('analyst', content, task.task_id)


@pytest.mark.parametrize('role', ['main', 'analyst', 'report_designer'])
def test_file_tools_cannot_mutate_kb_or_memory(context, role):
    store, sid, _ = context
    _, _, middleware = role_backend(store, role, sid)
    for target in ['/kb/catalog.md', '/memory/AGENTS.md']:
        result = file_tool(middleware, 'write_file', file_path=target, content='replace')
        assert 'permission denied' in result.content
    assert file_tool(middleware, 'edit_file', file_path='/kb/catalog.md', old_string='Shared KB', new_string='Changed KB').content.startswith('Error: permission denied')


def test_main_and_leaf_filesystem_reads_are_isolated(context):
    store, sid, _ = context
    active_memory(context, 'analyst', 'ANALYST_PRIVATE_REVIEW')
    active_memory(context, 'report_designer', 'REPORT_PRIVATE_REVIEW')
    (store.root / '.env').write_text('ENV_REVIEW_SENTINEL')
    for role, own, foreign in [('analyst', 'ANALYST_PRIVATE_REVIEW', 'REPORT_PRIVATE_REVIEW'), ('report_designer', 'REPORT_PRIVATE_REVIEW', 'ANALYST_PRIVATE_REVIEW')]:
        _, _, middleware = role_backend(store, role, sid)
        assert own in file_tool(middleware, 'read_file', file_path='/memory/AGENTS.md').content
        for path in ['/.env', f'/memory/{"report_designer" if role == "analyst" else "analyst"}/context/AGENTS.md', '/../.env']:
            content = graph_tool(store, sid, role, 'read_file', {'file_path': path})
            assert foreign not in content and 'ENV_REVIEW_SENTINEL' not in content
    _, _, main = role_backend(store, 'main', sid)
    assert 'permission denied' in file_tool(main, 'read_file', file_path='/memory/AGENTS.md').content


def test_summary_updates_and_deletion_can_run_concurrently(context):
    store, _, _ = context
    entry = active_memory(context, 'analyst', 'Concurrent memory marker.')
    memory = MemoryService(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(memory.summary_root, 'analyst') for _ in range(12)]
        jobs.append(pool.submit(memory.delete, 'analyst', entry['memory_id']))
        for job in jobs:
            job.result()
    assert 'Concurrent memory marker.' not in (memory.summary_root('analyst') / 'AGENTS.md').read_text()


def test_kb_excerpts_are_source_addressed_metadata_not_full_statistics(context):
    store, sid, _ = context
    (store.root / 'KB' / 'wine.csv').write_text('quality\n5\n6\n')
    (store.root / 'KB' / 'rules.md').write_text('Quality means the recorded assessment.\n')
    store.refresh_kb()
    snippets = KnowledgeService(store).snippets(sid)
    dataset = next(x for x in snippets if x['name'] == 'wine.csv')
    assert dataset['location'] == 'dataset metadata'
    assert 'not full data' in dataset['text']
    assert dataset['file_id'] and dataset['hash']
    assert KnowledgeService(store).search(sid, 'Quality')


def test_real_agent_assembly_has_only_two_experts_and_leaf_cannot_delegate(context):
    from src.agents import TaskRuntime, build_agents
    store, _, spec = context
    rt = TaskRuntime(spec, store, lambda *args, **kwargs: None, lambda: False)
    agents = build_agents(rt, lambda *_: OfflineToolModel(calls=[]))
    assert not agents  # graphs are constructed only when the role is needed
    for role in ('main', 'analyst', 'report_designer'):
        agent = agents[role]
        tools = agent.nodes['tools'].bound.tools_by_name
        assert ('task' in tools) == (role == 'main')
        assert 'execute' not in tools
        assert ('run_python' in tools) == (role == 'analyst')
        assert ('render_report' in tools) == (role == 'report_designer')
    assert set(agents) == {'main', 'analyst', 'report_designer'}


def test_main_delegation_executes_real_analysis_with_shared_runtime(context):
    from src.agents import run_task
    store, sid, spec = context
    (store.root / 'KB' / 'a.csv').write_text('value\n2\n3\n')
    fid = store.refresh_kb()[0]['file_id']
    spec.file_ids = [fid]
    spec.request = 'Compute average value'
    store.update_task(spec.task_id, spec=spec.model_dump())
    calls = {
        'main': [{'name': 'task', 'args': {'subagent_type': 'analyst', 'description': 'Compute average value'}, 'id': 'delegate'}],
        'analyst': [{'name': 'analyze_dataset', 'args': {'file_id': fid, 'operation': 'aggregate', 'aggregations': {'value': 'mean'}}, 'id': 'compute'}],
        'report_designer': [],
    }
    result = run_task(spec, store, lambda *args, **kwargs: None, lambda: False, model_factory=lambda _, role: OfflineToolModel(calls=calls[role]))
    assert result['status'] == 'completed', result
    assert result['analysis_ids']
    analysis = store.get_analysis(result['analysis_ids'][0], sid)
    assert analysis.metrics['value_mean'] == 2.5
    assert result['tool_calls'] == 2


def test_unverified_median_query_cannot_complete_as_general_answer(context):
    from src.agents import run_task
    store, _, spec = context
    spec.request = 'What is the median alcohol value?'
    result = run_task(spec, store, lambda *args, **kwargs: None, lambda: False,
        model_factory=lambda _, role: OfflineToolModel(calls=[{'name': 'finish_task', 'args': {'intent': 'general', 'answer': 'The median is 99'}, 'id': 'finish'}] if role == 'main' else []))
    assert result['status'] in {'failed', 'waiting_user'}
    assert not result['analysis_ids']


def test_tool_budget_and_immediate_cancellation(context):
    from src.agents import TaskRuntime, BudgetExceeded, run_task
    store, _, spec = context
    store.settings.max_tool_calls = 1
    rt = TaskRuntime(spec, store, lambda *args, **kwargs: None, lambda: False)
    rt.consume('main', 'task')
    with pytest.raises(BudgetExceeded):
        rt.consume('analyst', 'analyze_dataset')
    with pytest.raises(InterruptedError):
        run_task(spec, store, lambda *args, **kwargs: None, lambda: True,
                 model_factory=lambda *_: OfflineToolModel(calls=[]))


def test_unselected_input_and_other_session_analysis_are_rejected(context):
    from src.agents import TaskRuntime, analyst_tools
    from src.data import DataService
    store, sid, spec = context
    (store.root / 'KB' / 'a.csv').write_text('value\n2\n3\n')
    fid = store.refresh_kb()[0]['file_id']
    rt = TaskRuntime(spec, store, lambda *args, **kwargs: None, lambda: False)
    tool = next(x for x in analyst_tools(rt) if x.name == 'profile_dataset')
    assert tool.invoke({'file_id': fid})['code'] == 'file_not_selected'
    other_sid = store.new_session()['session_id']
    other_spec = TaskSpec(session_id=other_sid, request='Other', file_ids=[fid])
    store.create_task(other_spec)
    analysis = DataService(store).analyze(other_spec, fid, 'aggregate', aggregations={'value': 'mean'})
    with pytest.raises(StoreError, match='not found'):
        rt.authorize_analysis(analysis.analysis_id)


def test_graph_budget_is_shared_between_main_and_specialist(context):
    from src.agents import run_task
    store, _, spec = context
    store.settings.max_tool_calls = 1
    calls = {
        'main': [{'name': 'task', 'args': {'subagent_type': 'analyst', 'description': 'Inspect authorized files'}, 'id': 'delegate'}],
        'analyst': [{'name': 'authorized_files', 'args': {}, 'id': 'inspect'}],
        'report_designer': [],
    }
    result = run_task(spec, store, lambda *args, **kwargs: None, lambda: False,
                      model_factory=lambda _, role: OfflineToolModel(calls=calls[role]))
    assert result['status'] == 'failed'
    assert result['tool_calls'] == 1


def test_explicit_preference_does_not_promote_unverified_experience(context):
    from src.agents import run_task
    store, _, spec = context
    spec.request = 'Remember my preference: reports should use Chinese.'
    calls = {
        'main': [{'name': 'task', 'args': {'subagent_type': 'report_designer', 'description': 'Remember this preference'}, 'id': 'delegate'}],
        'analyst': [],
        'report_designer': [
            {'name': 'memory_propose', 'args': {'content': 'An untested calculation always works.', 'kind': 'experience'}, 'id': 'experience'},
            {'name': 'memory_propose', 'args': {'content': 'Reports in Chinese.', 'kind': 'preference'}, 'id': 'preference'},
        ],
    }
    result = run_task(spec, store, lambda *args, **kwargs: None, lambda: False,
                      model_factory=lambda _, role: OfflineToolModel(calls=calls[role]))
    assert result['status'] == 'completed'
    active = MemoryService(store).search('report_designer', 'untested')
    assert not any(x['content'] == 'An untested calculation always works.' for x in active)


def test_clarification_reply_preserves_already_verified_analysis(context):
    from src.agents import TaskRuntime
    from src.data import DataService
    from src.schemas import MessageRequest
    from src.services import Services
    store, sid, spec = context
    (store.root / 'KB' / 'a.csv').write_text('value\n2\n3\n')
    fid = store.refresh_kb()[0]['file_id']
    spec.file_ids = [fid]
    spec.expected_outputs = ['html', 'xlsx']
    store.update_task(spec.task_id, spec=spec.model_dump())
    analysis = DataService(store).analyze(spec, fid, 'aggregate', aggregations={'value': 'mean'})
    store.update_task(spec.task_id, status='waiting_user', result={'analysis_ids': [analysis.analysis_id]}, final_response='Which report audience?')
    reply = Services(store).submit(sid, MessageRequest(content='For management.', reply_to_task_id=spec.task_id))
    reply_spec = TaskSpec.model_validate(reply['spec'])
    assert reply_spec.expected_outputs == ['html', 'xlsx']
    rt = TaskRuntime(reply_spec, store, lambda *args, **kwargs: None, lambda: False)
    assert analysis.analysis_id in rt.allowed_analysis_ids
