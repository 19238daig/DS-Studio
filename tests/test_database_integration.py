"""Database selections remain authoritative across API, analysis reuse and retry."""
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.client import ApiClient
from src.schemas import AnalysisResult, MessageRequest, TaskSpec
from src.services import Services
from src.storage import StoreError
from test_database_core import db_setup, isolated_postgres


def saved_analysis(setup, source='olist', tables=None):
    store, database, sid = setup
    chosen = tables or ['olist.orders']
    task = TaskSpec.model_validate(Services(store).submit(sid, MessageRequest(
        content='统计订单', database_source_ids=[source], database_tables={source: chosen}))['spec'])
    table = 'olist.sellers' if source == 'sellers_restricted' else 'olist.orders'
    query = database.query(task, source, f'SELECT count(*) AS total FROM {table}')
    analysis = AnalysisResult(task_id=task.task_id, session_id=sid, db_refs=[query['db_ref']],
        metrics=query['scalar_metrics'], metric_definitions={'total': 'complete count'}, table_ids=[query['result_artifact_id']])
    store.save_analysis(analysis)
    store.update_task(task.task_id, status='succeeded', result={'analysis_ids': [analysis.analysis_id]})
    return task, analysis, query


def test_database_only_mixed_and_explicit_selection_flags(db_setup):
    store, database, sid = db_setup
    services = Services(store)
    task = services.submit(sid, MessageRequest(content='订单数量', database_source_ids=['olist'], database_tables={'olist': ['orders']}))
    assert task['spec']['file_ids'] == []
    assert task['spec']['database_tables'] == {'olist': ['olist.orders']}
    assert task['spec']['constraints']['database_sources_explicit'] is True
    assert task['spec']['constraints']['database_tables_explicit'] is True
    result = database.query(TaskSpec.model_validate(task['spec']), 'olist', 'SELECT count(*) AS total FROM olist.orders')
    assert result['scalar_metrics'] == {'total': 2}
    path = store.root / 'KB' / 'data.csv'
    path.write_text('x\n1\n', encoding='utf-8')
    file_id = store.refresh_kb()[0]['file_id']
    mixed = services.submit(sid, MessageRequest(content='混合分析', file_ids=[file_id], database_source_ids=['olist']))
    assert mixed['spec']['file_ids'] == [file_id] and mixed['spec']['database_source_ids'] == ['olist']


def test_analysis_snapshot_reuse_inherits_only_omitted_source_and_checks_tables(db_setup):
    store, _, sid = db_setup
    _, analysis, _ = saved_analysis(db_setup)
    services = Services(store)
    inherited = services.submit(sid, MessageRequest(content='生成报告', analysis_ids=[analysis.analysis_id]))
    assert inherited['spec']['database_source_ids'] == ['olist']
    assert inherited['spec']['database_tables'] == {'olist': ['olist.orders']}
    assert store.get_analysis(analysis.analysis_id, sid).input_versions == {}
    for sources, tables in [([], {}), (['sellers_restricted'], {}), (['olist'], {'olist': []})]:
        with pytest.raises(StoreError) as error:
            services.submit(sid, MessageRequest(content='生成报告', analysis_ids=[analysis.analysis_id],
                database_source_ids=sources, database_tables=tables))
        assert error.value.code in {'database_source_not_selected', 'database_table_not_allowed'}


def test_clarification_inherits_omitted_but_clears_explicit_selection(db_setup):
    store, _, sid = db_setup
    services = Services(store)
    parent = services.submit(sid, MessageRequest(content='比较', database_source_ids=['olist'], database_tables={'olist': ['orders']}))
    store.update_task(parent['task_id'], status='waiting_user')
    inherited = services.submit(sid, MessageRequest(content='全部订单', reply_to_task_id=parent['task_id']))
    assert inherited['spec']['database_tables'] == {'olist': ['olist.orders']}
    cleared = services.submit(sid, MessageRequest(content='只回答通用问题', reply_to_task_id=parent['task_id'], database_source_ids=[], database_tables={}))
    assert cleared['spec']['database_source_ids'] == [] and cleared['spec']['database_tables'] == {}
    empty = services.submit(sid, MessageRequest(content='不读表', reply_to_task_id=parent['task_id'], database_tables={'olist': []}))
    assert empty['spec']['database_tables'] == {'olist': []}


def test_retry_preserves_source_empty_tables_and_explicit_flags(db_setup):
    store, _, sid = db_setup
    services = Services(store)
    original = services.submit(sid, MessageRequest(content='不要读取表', database_source_ids=['olist'], database_tables={'olist': []}))
    store.update_task(original['task_id'], status='failed')
    retried = services.retry(original['task_id'], 'retry-key')
    assert retried['spec']['database_tables'] == {'olist': []}
    assert retried['spec']['constraints'] == original['spec']['constraints']
    assert services.retry(original['task_id'], 'retry-key')['task_id'] == retried['task_id']
    implicit = services.submit(sid, MessageRequest(content='普通提问'))
    store.update_task(implicit['task_id'], status='failed')
    retried_implicit = services.retry(implicit['task_id'])
    assert retried_implicit['spec']['constraints']['database_sources_explicit'] is False


def test_legacy_digest_stable_and_changed_database_choice_conflicts(db_setup):
    store, _, sid = db_setup
    services = Services(store)
    request = MessageRequest(content='普通问题')
    expected = request.model_dump(exclude={'idempotency_key', 'database_source_ids', 'database_tables'})
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    task = services.submit(sid, request, 'old-key')
    assert task['request_digest'] == digest
    assert services.submit(sid, request, 'old-key')['task_id'] == task['task_id']
    with pytest.raises(StoreError) as error:
        services.submit(sid, MessageRequest(content='普通问题', database_source_ids=[]), 'old-key')
    assert error.value.code == 'idempotency_conflict'


def test_database_analysis_rejects_hash_cross_session_and_extra_reference_keys(db_setup):
    store, _, sid = db_setup
    _, analysis, result = saved_analysis(db_setup)
    other_sid = store.new_session()['session_id']
    other = TaskSpec(session_id=other_sid, request='复用', database_source_ids=['olist'])
    store.create_task(other)
    forged = analysis.model_copy(update={'analysis_id': 'forged', 'session_id': other_sid, 'task_id': other.task_id})
    with pytest.raises(StoreError):
        store.save_analysis(forged)
    extra = analysis.model_copy(update={'db_refs': [{**analysis.db_refs[0], 'extra': 'bad'}]})
    with pytest.raises(StoreError, match='reference'):
        store.save_analysis(extra)
    store.resolve_artifact(result['result_artifact_id']).write_text('total\n999\n', encoding='utf-8')
    with pytest.raises(StoreError) as error:
        store.get_analysis(analysis.analysis_id, sid)
    assert error.value.code == 'artifact_changed'


def test_source_table_permissions_and_file_only_when_database_unavailable(db_setup):
    store, database, sid = db_setup
    services = Services(store)
    with pytest.raises(StoreError):
        services.submit(sid, MessageRequest(content='读订单', database_source_ids=['sellers_restricted'], database_tables={'sellers_restricted': ['orders']}))
    with pytest.raises(StoreError):
        services.submit(sid, MessageRequest(content='读表', database_tables={'olist': ['orders']}))
    empty = services.submit(sid, MessageRequest(content='不读表', database_source_ids=['olist'], database_tables={'olist': []}))
    with pytest.raises(StoreError):
        database.query(TaskSpec.model_validate(empty['spec']), 'olist', 'SELECT count(*) FROM olist.orders')
    store.settings.db_port = 1
    assert services.submit(sid, MessageRequest(content='离线普通问答'))['status'] == 'queued'
    with pytest.raises(StoreError):
        services.submit(sid, MessageRequest(content='数据库', database_source_ids=['olist']))


def test_database_metadata_preview_and_no_arbitrary_sql_api(db_setup):
    store, _, sid = db_setup
    app = create_app(store.settings)
    with TestClient(app) as client:
        health = client.get('/api/v1/health').json()
        assert health['database'] == {'configured': True}
        sources = client.get('/api/v1/database/sources').json()['items']
        assert {item['source_id'] for item in sources} == {'olist', 'sellers_restricted'}
        catalog = client.get('/api/v1/database/sources/olist/catalog').json()
        assert any(table['qualified_name'] == 'olist.orders' for table in catalog['tables'])
        url = f'/api/v1/sessions/{sid}/database/preview'
        assert client.post(url, json={'source_id': 'olist', 'table': 'orders', 'limit': 51}).status_code == 422
        assert client.post(url, json={'source_id': 'olist', 'table': 'orders', 'sql': 'DELETE FROM olist.orders'}).status_code == 422
        assert client.post(url, json={'source_id': 'sellers_restricted', 'table': 'orders'}).status_code == 403
        preview = client.post(url, json={'source_id': 'sellers_restricted', 'table': 'sellers', 'limit': 1})
        assert preview.status_code == 200, preview.text
        result = preview.json()
        assert result['row_count'] == 1 and result['preview'][0]['seller_city'] == 'santo andre'
        task = client.get('/api/v1/tasks/' + result['task_id']).json()
        assert task['status'] == 'succeeded' and task['spec']['database_tables'] == {'sellers_restricted': ['olist.sellers']}
        assert result['task_id'] not in client.get('/api/v1/sessions/' + sid).json()['tasks']
        visible = json.dumps({'health': health, 'sources': sources, 'catalog': catalog, 'preview': result})
        assert 'private-test-placeholder' not in visible and 'password' not in visible.lower()
        assert client.post('/api/v1/database/sql', json={'sql': 'SELECT 1'}).status_code == 404


def test_deleted_session_removes_query_index_and_artifacts(db_setup):
    store, _, sid = db_setup
    _, analysis, result = saved_analysis(db_setup)
    assert result['query_id'] in store._index('database_queries')
    store.delete_session(sid)
    assert result['query_id'] not in store._index('database_queries')
    assert not store._index('artifacts') and not store._index('analyses')


@pytest.mark.parametrize('sources,tables', [(None, None), ([], {}), (['olist'], {'olist': []})])
def test_client_preserves_database_omission_and_clear(sources, tables):
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(202, json={'task_id': 'new'})
    client = ApiClient(transport=httpx.MockTransport(response))
    try:
        client.submit('sid', 'question', [], [], 'key', database_source_ids=sources, database_tables=tables)
    finally:
        client.close()
    payload = json.loads(requests[0].content)
    if sources is None:
        assert 'database_source_ids' not in payload and 'database_tables' not in payload
    else:
        assert payload['database_source_ids'] == sources and payload['database_tables'] == tables
