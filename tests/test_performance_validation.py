"""Performance changes must preserve checks across every operation boundary."""
import os

import pandas as pd
import psycopg
import pytest
from openpyxl import Workbook

from src.config import Settings
from src.data import DataService, infer_numbers, records
from src.database import DatabaseService
from src.database.catalog import describe
from src.database.connection import connect
from src.performance import span
from src.reporting import ReportService
from src.schemas import AnalysisResult, TaskSpec
from src.storage import Store, StoreError
from src.validation import catalog_once, validation_scope
from test_database_core import db_setup, isolated_postgres, task_for


@pytest.fixture
def local(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    task = TaskSpec(session_id=sid, request='Performance validation')
    store.create_task(task)
    return store, task, DataService(store)


def artifact(local, content, suffix='.csv'):
    store, task, _ = local
    path = store.root / 'script_output' / task.session_id / task.task_id / ('result'+suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding='utf-8')
    else:
        content.save(path)
    return path, store.register_artifact(path, task.task_id, task.session_id, 'table')


def test_catalog_scope_counts_copy_isolation_and_no_cross_boundary_cache(local):
    store, task, _ = local
    calls = []
    def load():
        calls.append(1)
        return {'tables': ['products', 'orders']}
    with span(store, task.task_id, 'model_context'):
        with validation_scope(store, fresh=True) as scope:
            for _ in range(16):
                catalog_once(store, ('olist',), load)['tables'].clear()
            assert catalog_once(store, ('olist',), load)['tables'] == ['products', 'orders']
            with validation_scope(store):
                catalog_once(store, ('olist',), load)
            assert scope.counters == {'catalog_requests':18, 'catalog_loads':1, 'catalog_hits':17}
            with validation_scope(store, fresh=True):
                catalog_once(store, ('olist',), load)
    with validation_scope(store, fresh=True):
        catalog_once(store, ('olist',), load)
    catalog_once(store, ('olist',), load)
    catalog_once(store, ('olist',), load)
    assert len(calls) == 5
    events = store.list_events(task.task_id)
    counters = [e for e in events if e.get('name') == 'validation_boundary' and e.get('catalog_requests') == 18]
    assert len(counters) == 1 and counters[0]['catalog_loads'] == 1


def test_spans_nest_redact_and_preserve_original_failure(local, monkeypatch):
    store, task, _ = local
    with span(store, task.task_id, 'outer', prompt='secret', password='secret') as outer:
        with span(store, task.task_id, 'inner', tool_name='read_table', model='bad secret'):
            pass
    events = [e for e in store.list_events(task.task_id) if e['type'] == 'span_completed']
    assert events[0]['parent_span_id'] == outer
    assert events[0]['tool_name'] == 'read_table'
    assert events[0]['duration_seconds'] >= 0
    assert not any('secret' in str(e) for e in events)
    def broken(*args, **kwargs):
        raise OSError('trace unavailable')
    monkeypatch.setattr(store, 'add_event', broken)
    with pytest.raises(StoreError, match='original'):
        with span(store, task.task_id, 'failure'):
            raise StoreError('original')


def test_catalog_source_identity_and_disabled_dedup(local):
    store, _, _ = local
    calls = []
    def load():
        calls.append(1)
        return {'tables': []}
    with validation_scope(store,fresh=True):
        catalog_once(store,('full',),load)
        catalog_once(store,('limited',),load)
        catalog_once(store,('full',),load)
    assert len(calls) == 2
    store.settings.auth_dedup_enabled = False
    with validation_scope(store,fresh=True):
        catalog_once(store,('full',),load)
        catalog_once(store,('full',),load)
    assert len(calls) == 4


def test_session_delete_cleans_only_its_stream_journal(local):
    store, task, _ = local
    other = store.new_session()['session_id']
    for sid in (task.session_id,other):
        path = store.root/'state'/'streams'/sid/'event.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('{}\n',encoding='utf-8')
    store.update_task(task.task_id,status='succeeded')
    store.delete_session(task.session_id)
    assert not (store.root/'state'/'streams'/task.session_id).exists()
    assert (store.root/'state'/'streams'/other/'event.jsonl').is_file()


def test_full_checksum_cannot_be_replaced_by_size_mtime_cache(local):
    store, task, _ = local
    path, table = artifact(local, 'x\n10\n')
    stamp = path.stat()
    with validation_scope(store, fresh=True):
        store.resolve_artifact(table.artifact_id)
        path.write_text('x\n20\n', encoding='utf-8')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with pytest.raises(StoreError):
            store.resolve_artifact(table.artifact_id)
    with validation_scope(store, fresh=True), pytest.raises(StoreError):
        store.resolve_artifact(table.artifact_id)


def test_csv_preview_is_bounded_and_preserves_full_column_inference(local, monkeypatch):
    store, task, data = local
    body = 'code,value,note\n' + ''.join(f'{i},{i}.5,"line\n{i}"\n' for i in range(5000)) + '0001,text,last\n'
    path, table = artifact(local, body)
    expected = records(infer_numbers(pd.read_csv(path, dtype=str, keep_default_na=False).replace('', None)).head(3))
    original, calls = pd.read_csv, []
    def bounded(*args, **kwargs):
        assert kwargs.get('chunksize') or 'nrows' in kwargs, 'preview loaded an entire CSV frame'
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, 'read_csv', bounded)
    first = data.read_result_table(table.artifact_id, task.session_id, 3)
    second = data.read_result_table(table.artifact_id, task.session_id, 2)
    assert first['total_rows'] == second['total_rows'] == 5001
    assert first['preview'] == expected and first['truncated'] is True
    assert second['preview'] == expected[:2]
    assert calls[0]['chunksize'] == 4096 and calls[-1]['nrows'] == 2


def test_csv_preview_signed_unsigned_inference_is_global(local):
    _, task, data = local
    path, table = artifact(local, 'number\n' + '18446744073709551615\n' * 4096 + '-1\n')
    expected = records(infer_numbers(pd.read_csv(path,dtype=str,keep_default_na=False).replace('',None)).head(1))
    assert data.read_result_table(table.artifact_id,task.session_id,1)['preview'] == expected


def test_xlsx_preview_matches_full_reader_dates_blanks_and_late_text(local):
    from datetime import datetime
    store, task, data = local
    book = Workbook()
    sheet = book.active
    sheet.append(['code', 'value', 'date'])
    sheet.append(['1', 1.5, datetime(2025,1,2)])
    sheet.append([None, None, None])
    sheet.append(['0002', 'late text', datetime(2025,1,3)])
    sheet.cell(100,1).number_format = '0'
    path, table = artifact(local, book, '.xlsx')
    expected = records(infer_numbers(pd.read_excel(path,dtype=object,keep_default_na=False).replace('',None)).head(2))
    first = data.read_result_table(table.artifact_id, task.session_id, 2)
    second = data.read_result_table(table.artifact_id, task.session_id, 2)
    assert first['total_rows'] == second['total_rows'] == 3
    assert first['preview'] == second['preview'] == expected


def test_parse_cache_reuses_parse_but_checks_integrity_and_returns_copy(local, monkeypatch):
    store, task, data = local
    path = store.root / 'input' / task.session_id / 'source.csv'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('value\n10\n',encoding='utf-8')
    fid = store.register_file(path, path.name, task.session_id)['file_id']
    original, calls = data._load_uncached, []
    def parse(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(data, '_load_uncached', parse)
    frame = data.read_table(fid,task.session_id)
    frame.iloc[0,0] = 99
    assert data.read_table(fid,task.session_id).iloc[0,0] == 10
    assert len(calls) == 1
    stamp = path.stat()
    path.write_text('value\n20\n',encoding='utf-8')
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    with pytest.raises(StoreError):
        data.read_table(fid,task.session_id)


def test_database_selection_and_acl_are_checked_across_fresh_boundaries(db_setup, isolated_postgres):
    store, service, _ = db_setup
    origin = task_for(db_setup, tables=['products','orders'])
    result = service.query(origin,'olist','SELECT count(*) AS count FROM olist.products')
    narrow = task_for(db_setup,tables=['orders'])
    with validation_scope(store,fresh=True):
        service.authorize_snapshot(origin,result['db_ref'])
        with pytest.raises(StoreError):
            service.authorize_snapshot(narrow,result['db_ref'])
    try:
        with psycopg.connect(**isolated_postgres) as admin:
            admin.execute('REVOKE SELECT ON olist.products FROM ds_test_reader')
        with validation_scope(store,fresh=True), pytest.raises(StoreError):
            service.authorize_snapshot(origin,result['db_ref'])
    finally:
        with psycopg.connect(**isolated_postgres) as admin:
            admin.execute('GRANT SELECT ON olist.products TO ds_test_reader')


def test_repeated_real_snapshot_authorizations_reduce_catalog_not_checks(db_setup, monkeypatch):
    import src.database.query as query
    store, service, _ = db_setup
    task = task_for(db_setup,tables=['products'])
    result = service.query(task,'olist','SELECT count(*) AS count FROM olist.products')
    original, calls = query.catalog, []
    def catalog(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(query,'catalog',catalog)
    for _ in range(16):
        service.authorize_snapshot(task,result['db_ref'])
    assert len(calls) == 16
    calls.clear()
    with validation_scope(store,fresh=True) as scope:
        for _ in range(16):
            service.authorize_snapshot(task,result['db_ref'])
    assert len(calls) == 1
    assert scope.counters['snapshot_checks'] == 16
    assert scope.counters['catalog_requests'] == 16
    assert scope.counters['catalog_loads'] == 1
    assert scope.counters['artifact_hashes'] >= 16 * 3


def test_report_final_publish_uses_fresh_acl_even_inside_outer_scope(db_setup, isolated_postgres, monkeypatch):
    store, service, _ = db_setup
    task = TaskSpec(**vars(task_for(db_setup,tables=['products'])))
    result = service.query(task,'olist','SELECT count(*) AS count FROM olist.products')
    analysis = AnalysisResult(task_id=task.task_id,session_id=task.session_id,
        db_refs=[result['db_ref']],table_ids=[result['result_artifact_id']],metrics={'count':2},
        metric_definitions={'count':{'source':'SQL snapshot'}},findings=['Count is 2.'])
    store.save_analysis(analysis)
    reports = ReportService(store)
    original = reports._validate
    def revoke_after_render(*args):
        original(*args)
        with psycopg.connect(**isolated_postgres) as admin:
            admin.execute('REVOKE SELECT ON olist.products FROM ds_test_reader')
    monkeypatch.setattr(reports,'_validate',revoke_after_render)
    try:
        with validation_scope(store,fresh=True), pytest.raises(StoreError):
            reports.generate(task,[analysis.analysis_id],formats=['html'])
        assert not [a for a in store.list_artifacts(task.task_id) if a.kind.startswith('report')]
        assert not list((store.root/'report').rglob('report.html'))
    finally:
        with psycopg.connect(**isolated_postgres) as admin:
            admin.execute('GRANT SELECT ON olist.products TO ds_test_reader')


def test_catalog_metadata_uses_two_queries_independent_of_table_count(db_setup):
    _, service, _ = db_setup
    statements = []
    with connect(service.settings,'olist') as connection:
        class Counting:
            info = connection.info
            def execute(self, sql, *args, **kwargs):
                statements.append(sql)
                return connection.execute(sql,*args,**kwargs)
        with connection.transaction():
            result = describe(Counting(),'olist')
    assert len(result['tables']) >= 3
    columns = [sql for sql in statements if 'pg_catalog.col_description' in sql]
    constraints = [sql for sql in statements if 'pg_catalog.pg_get_constraintdef' in sql]
    assert len(columns) == len(constraints) == 1
    assert next(t for t in result['tables'] if t['name']=='products')['primary_key'] == ['PRIMARY KEY (product_id)']
