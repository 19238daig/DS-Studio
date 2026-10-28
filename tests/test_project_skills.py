"""Installed project skills are actually consumed by Deep Agents, read-only."""
import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup
from langchain_core.messages import AIMessage, ToolMessage

from src.agents import TaskRuntime, build_agents, run_task
from src.backends import KnowledgeService, role_backend
from src.config import Settings
from src.data import DataService
from src.schemas import TaskSpec
from src.skills import ROLE_SKILLS, SKILLS_ROOT, skill_catalog, skill_root
from src.storage import Store
from test_agents import call, models


def setup(tmp_path, request='生成销售分析报告'):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = tmp_path / 'KB' / 'sales.csv'
    path.write_text('amount,category\n10,A\n20,A\n30,B\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request=request, file_ids=[fid])
    store.create_task(task)
    events = lambda kind, message='', **details: store.add_event(task.task_id, kind, message, **details)
    return store, task, fid, events


def pinned(messages):
    return [m for m in messages if getattr(m, 'additional_kwargs', {}).get('lc_source') == 'pinned_skill']


def test_downloaded_and_active_skills_match_pinned_manifest():
    root = SKILLS_ROOT.parent
    manifest = json.loads((SKILLS_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['upstream']['repository'] == 'anthropics/knowledge-work-plugins'
    assert manifest['upstream']['license'] == 'Apache-2.0'
    assert len(manifest['upstream']['commit']) == 40
    for entry in [*manifest['upstream']['skills'], *manifest['active']]:
        assert hashlib.sha256((root / entry['path']).read_bytes()).hexdigest() == entry['sha256']
    license_path = root / manifest['upstream']['license_path']
    assert hashlib.sha256(license_path.read_bytes()).hexdigest() == manifest['upstream']['license_sha256']
    assert 'Apache License' in license_path.read_text(encoding='utf-8')
    assert 'cdn.jsdelivr.net' in (root / 'vendor/skills/anthropic-data/build-dashboard/SKILL.md').read_text(encoding='utf-8')
    assert 'render_report/revise_report' in (SKILLS_ROOT / 'report_designer/analytics-dashboard/SKILL.md').read_text(encoding='utf-8')


def test_role_skill_backends_are_isolated_readonly_and_separate_from_datasets(tmp_path):
    store, task, _, _ = setup(tmp_path)
    assert skill_root('analyst').is_relative_to(Path(__file__).resolve().parents[1])
    assert not skill_root('analyst').is_relative_to(store.root)
    for role, names in ROLE_SKILLS.items():
        backend, permissions, _ = role_backend(store, role, task.session_id)
        assert {x['name'] for x in skill_catalog(role)} == set(names)
        for name in names:
            virtual = f'/skills/{name}/SKILL.md'
            before = (skill_root(role) / name / 'SKILL.md').read_bytes()
            assert name in str(backend.read(virtual))
            assert backend.write(virtual, 'overwrite').error
            assert backend.edit(virtual, name, 'changed').error
            assert backend.delete(virtual).error
            assert backend.upload_files([(virtual, b'overwrite')])[0].error == 'permission_denied'
            assert before == (skill_root(role) / name / 'SKILL.md').read_bytes()
        assert any('/skills/**' in p.paths and p.mode == 'deny' for p in permissions)
    analyst, _, _ = role_backend(store, 'analyst', task.session_id)
    assert analyst.download_files(['/skills/analytics-dashboard/SKILL.md'])[0].error
    assert all('skills/' not in item['name'] for item in store.list_files(task.session_id))
    assert not any('SKILL.md' in item['name'] for item in KnowledgeService(store).snippets(task.session_id))


def test_native_skills_pinning_reaches_each_specialist_and_records_hash(tmp_path):
    store, task, _, events = setup(tmp_path)
    runtime = TaskRuntime(task, store, events, lambda: False)
    scripted, factory = models([AIMessage(content='协调')], [AIMessage(content='分析')], [AIMessage(content='报告')])
    agents = build_agents(runtime, factory)
    for role in ROLE_SKILLS:
        agents[role].invoke({'messages': [{'role': 'user', 'content': task.request}]})
        injected = pinned(scripted[role]._seen[0])
        assert {m.additional_kwargs['skill']['name'] for m in injected} == set(ROLE_SKILLS[role])
        assert all('<skill name=' in m.content and '</skill>' in m.content and len(m.content) > 100 for m in injected)
    loaded = [e for e in store.list_events(task.task_id) if e['type'] == 'skill_loaded']
    assert len(loaded) == 7
    assert {e['role'] for e in loaded} == set(ROLE_SKILLS)
    assert all(len(e['skill_sha256']) == 64 and e['activation'] == 'native_pinned_skill' for e in loaded)


def test_scalar_fast_path_does_not_activate_chart_skill_or_compute_extra_stats(tmp_path):
    store, task, fid, events = setup(tmp_path, 'amount的平均值是多少？只给均值')
    scripted, factory = models([call('task', {'subagent_type': 'analyst', 'description': '只计算amount均值'})],
        [call('answer_metric', {'file_id': fid, 'column': 'amount', 'aggregation': 'mean'})])
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed'
    assert '20' in result['final_response']
    a = store.get_analysis(result['analysis_ids'][0], task.session_id)
    assert [key for key in a.metrics if key not in {'source_rows', 'output_rows', 'result_rows'}] == ['amount_mean']
    assert {m.additional_kwargs['skill']['name'] for m in pinned(scripted['analyst']._seen[0])} == {'data-analysis', 'token-optimization'}
    assert '单指标快答' in pinned(scripted['analyst']._seen[0])[0].content


def test_real_deepagents_uses_dashboard_skill_to_render_registered_results(tmp_path):
    store, task, fid, events = setup(tmp_path)
    analysis = DataService(store).analyze(task, fid, 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    store.update_task(task.task_id, spec=task.model_dump())
    summary = '平均销售额为{{metric:' + analysis.analysis_id + ':amount_mean}}。'
    scripted, factory = models([call('task', {'subagent_type': 'report_designer', 'description': '生成销售分析报告'})],
        report=[call('render_report', {'analysis_ids': [analysis.analysis_id], 'title': '销售结论',
                                     'formats': ['html'], 'narrative': {'summary': summary}})])
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and result['report_ids']
    injected = pinned(scripted['report_designer']._seen[0])
    assert {m.additional_kwargs['skill']['name'] for m in injected} == {'analytics-dashboard', 'token-optimization'}
    assert 'include_tables=false' in injected[0].content
    html = store.resolve_artifact(result['report_ids'][0]).read_text(encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    assert soup.find('h2').get_text() == '结论'
    assert '20' in soup.select_one('.report-conclusion').get_text()
    assert not soup.find('h2', string='结果明细')
    assert not soup.find('h2', string='指标汇总')
    assert any(e['type'] == 'skill_loaded' and e['skill_name'] == 'analytics-dashboard' for e in store.list_events(task.task_id))
