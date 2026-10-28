from pathlib import Path
import pytest
from src.config import Settings
from src.memory import MemoryService
from src.backends import KnowledgeService, role_backend
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(workspace=tmp_path, api_key='sk-testcredential123456', _env_file=None))
    session = store.new_session()
    task = TaskSpec(session_id=session['session_id'], request='记住报告偏好')
    store.create_task(task)
    return store, task


def test_candidate_promotion_persistence_delete_and_role_isolation(context):
    store, task = context
    service = MemoryService(store)
    entry = service.propose('analyst', '使用已核对的分号读取规则', task.task_id)
    assert not service.search('analyst', '分号')
    service.finalize(task.task_id, success=True, verified=True)
    reloaded = MemoryService(Store(store.settings))
    assert reloaded.search('analyst', '分号')[0]['memory_id'] == entry['memory_id']
    assert not reloaded.search('report_designer', '分号')
    with pytest.raises(StoreError):
        reloaded.delete('report_designer', entry['memory_id'])
    root = reloaded.summary_root('analyst')
    assert '分号' in (root / 'AGENTS.md').read_text(encoding='utf-8')
    reloaded.delete('analyst', entry['memory_id'])
    assert not reloaded.search('analyst', '分号')
    assert '分号' not in (root / 'AGENTS.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('success,verified', [(False, True), (True, False), (False, False)])
def test_failed_unverified_or_cancelled_experience_never_promotes(context, success, verified):
    store, task = context
    service = MemoryService(store)
    service.propose('report_designer', '先写摘要再放图表', task.task_id)
    service.finalize(task.task_id, success=success, verified=verified)
    assert not service.search('report_designer', '摘要')


@pytest.mark.parametrize('content', ['API_KEY=do-not-save', 'sk-abcdefgh1234567890', 'a,b,c\n' * 25])
def test_credentials_and_bulk_tables_are_rejected(context, content):
    store, task = context
    with pytest.raises(StoreError):
        MemoryService(store).propose('analyst', content, task.task_id)


def test_empty_kb_and_readonly_role_backend(context):
    store, task = context
    kb = KnowledgeService(store)
    assert kb.search(task.session_id, '不存在') == []
    memory = MemoryService(store)
    memory.propose('analyst', '分析私有规则', task.task_id)
    memory.propose('report_designer', '报告私有规则', task.task_id)
    memory.finalize(task.task_id, success=True, verified=True)
    analyst, permissions, fs = role_backend(store, 'analyst', task.session_id)
    assert '分析私有规则' in str(analyst.read('/memory/AGENTS.md'))
    assert '报告私有规则' not in str(analyst.read('/memory/AGENTS.md'))
    assert analyst.write('/memory/AGENTS.md', 'overwrite').error
    assert analyst.write('/kb/catalog.md', 'overwrite').error
    assert fs._permissions
    assert 'entries.json' not in str(analyst.ls('/memory/'))


def test_explicit_preference_does_not_validate_unverified_experience(context):
    store, task = context
    service = MemoryService(store)
    service.propose('report_designer', '报告使用中文', task.task_id, kind='preference')
    service.propose('report_designer', '未经验证的计算方法', task.task_id, kind='experience')
    service.finalize(task.task_id, success=True, explicit_preferences=True)
    entries = service.list('report_designer')
    assert len(entries) == 1
    assert entries[0]['kind'] == 'preference' and entries[0]['status'] == 'active'
