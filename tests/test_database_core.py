"""Database policy and immutable-snapshot checks on an isolated PG18 cluster.

The cluster never uses the user's running database or connection credentials.
"""
from __future__ import annotations

import copy
import csv
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import RawServerCursor

from src.config import ROOT, Settings
from src.database import DatabaseService
from src.database.policy import validate_sql
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.mark.parametrize('sql,params,tables', [
    ('SELECT AVG(price) AS mean FROM olist.products', 0, ['olist.products']),
    ('WITH p AS (SELECT * FROM olist.products) SELECT count(*) FROM p', 0, ['olist.products']),
    ('WITH p AS (SELECT * FROM olist.products), q AS (SELECT * FROM p) SELECT * FROM q', 0, ['olist.products']),
    ('SELECT product_id, row_number() OVER (ORDER BY price) FROM olist.products', 0, ['olist.products']),
    ('SELECT count(*) FROM olist.products WHERE price > $1 AND product_id <> $2', 2, ['olist.products']),
    ('SELECT CAST(1 AS numeric(20,6)) AS precise', 0, []),
    ('SELECT pg_catalog.round(1.25::numeric, 1)', 0, []),
    ('SELECT * FROM (WITH local AS (SELECT * FROM olist.products) SELECT * FROM local) p', 0, ['olist.products']),
])
def test_policy_accepts_readonly_sql(sql, params, tables):
    assert validate_sql(sql, {'olist.products'}, params_count=params)['tables'] == tables


@pytest.mark.parametrize('sql,params', [
    ('SELECT 1; SELECT 2', 0),
    ('DELETE FROM olist.products', 0),
    ('WITH x AS (DELETE FROM olist.products RETURNING *) SELECT * FROM x', 0),
    ('WITH x AS (INSERT INTO olist.products VALUES (\'x\', 1) RETURNING *) SELECT * FROM x', 0),
    ('SELECT * INTO copied FROM olist.products', 0),
    ('SELECT * FROM olist.products FOR UPDATE', 0),
    ('COPY olist.products TO STDOUT', 0),
    ('SET ROLE postgres', 0),
    ('SELECT * FROM pg_catalog.pg_authid', 0),
    ('SELECT * FROM public.products', 0),
    ('SELECT * FROM products', 0),
    ('SELECT * FROM olist.orders', 0),
    ('SELECT pg_read_file(\'secret\')', 0),
    ('SELECT pg_sleep(1)', 0),
    ('SELECT set_config(\'role\',\'postgres\',true)', 0),
    ('SELECT public.round(1.2)', 0),
    ('SELECT 1::public.malicious_type', 0),
    ('SELECT \'a\' COLLATE public.custom', 0),
    ('SELECT 1 OPERATOR(public.+) 2', 0),
    ('WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x', 0),
    ('SELECT * FROM orders WHERE EXISTS (WITH orders AS (SELECT 1) SELECT * FROM orders)', 0),
    ('SELECT * FROM (WITH orders AS (SELECT 1) SELECT * FROM orders) x, orders', 0),
    ('SELECT $1', 0),
    ('SELECT $2', 2),
    ('SELECT $1', 2),
    ('SELECT 1', 1),
])
def test_policy_rejects_mutation_scope_escape_and_parameters(sql, params):
    with pytest.raises(StoreError):
        validate_sql(sql, {'olist.products'}, params_count=params)


@pytest.fixture(scope='session')
def isolated_postgres():
    """Start and remove a private loopback cluster using installed PG18 tools."""
    binary = Path('E:/PostgreSQL/18/bin')
    if not (binary / 'initdb.exe').is_file():
        pytest.skip('Native PG18 tools unavailable for isolated integration checks')
    base = (ROOT / '.runtime').resolve()
    directory = base / ('database_test_' + uuid4().hex)
    assert directory.resolve().is_relative_to(base)
    directory.mkdir(parents=True)
    data = directory / 'data'
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    running = False
    try:
        initialized = subprocess.run([str(binary / 'initdb.exe'), '-D', str(data), '-U', 'test_admin',
            '-A', 'trust', '--encoding=UTF8', '--locale=C'], capture_output=True, timeout=90)
        assert initialized.returncode == 0, 'Private PostgreSQL initdb failed'
        started = subprocess.run([str(binary / 'pg_ctl.exe'), '-D', str(data), '-l', str(directory / 'server.log'),
            '-o', f'-h 127.0.0.1 -p {port} -F', '-w', 'start'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        assert started.returncode == 0, 'Private PostgreSQL startup failed'
        running = True
        config = dict(host='127.0.0.1', port=port, dbname='postgres', user='test_admin', autocommit=True)
        with psycopg.connect(**config) as admin:
            admin.execute('CREATE ROLE ds_test_owner NOLOGIN')
            admin.execute('CREATE ROLE ds_test_reader LOGIN')
            admin.execute('CREATE ROLE ds_test_limited LOGIN')
            admin.execute('CREATE SCHEMA olist AUTHORIZATION ds_test_owner')
            admin.execute('SET ROLE ds_test_owner')
            admin.execute('CREATE TABLE olist.products(product_id text PRIMARY KEY, price numeric(20,6))')
            admin.execute('CREATE TABLE olist.orders(order_id text PRIMARY KEY, price numeric(20,6))')
            admin.execute('CREATE TABLE olist.sellers(seller_id text PRIMARY KEY,seller_city text)')
            admin.execute("INSERT INTO olist.products VALUES ('001',123456789012.123456),('002',0.000001)")
            admin.execute("INSERT INTO olist.orders VALUES ('o1',10),('o2',20)")
            admin.execute("INSERT INTO olist.sellers VALUES ('a','santo andre'),('b','sao paulo'),('c','santo andre')")
            admin.execute('CREATE VIEW olist.v_order_summary WITH (security_invoker=true) AS SELECT * FROM olist.orders')
            admin.execute('CREATE VIEW olist.v_order_items_agg AS SELECT * FROM olist.orders')
            admin.execute('GRANT USAGE ON SCHEMA olist TO ds_test_reader,ds_test_limited')
            admin.execute('GRANT SELECT ON ALL TABLES IN SCHEMA olist TO ds_test_reader')
            admin.execute('GRANT SELECT ON olist.sellers TO ds_test_limited')
            admin.execute('ALTER TABLE olist.sellers ENABLE ROW LEVEL SECURITY')
            admin.execute('CREATE POLICY full_sellers ON olist.sellers FOR SELECT TO ds_test_reader USING (true)')
            admin.execute("CREATE POLICY limited_sellers ON olist.sellers FOR SELECT TO ds_test_limited USING (seller_city='santo andre')")
            admin.execute('RESET ROLE')
        yield config
    finally:
        if running or (data / 'postmaster.pid').exists():
            stopped = subprocess.run([str(binary / 'pg_ctl.exe'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            assert stopped.returncode == 0, 'Private PostgreSQL stop failed; cluster retained'
        # Only this fixture's unique, absolute workspace path can be removed.
        assert directory.resolve().is_relative_to(base) and directory.name.startswith('database_test_')
        shutil.rmtree(directory)


@pytest.fixture
def db_setup(tmp_path, isolated_postgres):
    settings = Settings(workspace=tmp_path, _env_file=None, db_enabled=True,
        db_host='127.0.0.1', db_port=isolated_postgres['port'], db_name='postgres',
        db_user='ds_test_reader', db_password='private-test-placeholder',
        db_restricted_user='ds_test_limited', db_restricted_password='private-test-placeholder', db_fetch_rows=1)
    store = Store(settings)
    session_id = store.new_session()['session_id']
    return store, DatabaseService(store), session_id


def task_for(setup, source='olist', tables=None, session_id=None):
    store, _, sid = setup
    task = TaskSpec(session_id=session_id or sid, request='Database integration check')
    store.create_task(task)
    fields = dict(database_source_ids=[source], database_tables={source:tables} if tables is not None else {})
    spec = {**task.model_dump(), **fields}
    store.update_task(task.task_id, spec=spec)
    return SimpleNamespace(**spec)


def test_real_query_native_parameters_decimal_csv_and_authorization(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup)
    result = service.query(task, 'olist', 'SELECT product_id, price FROM olist.products WHERE product_id=$1', ['001'])
    assert result['scalar_metrics'] == {'product_id':'001', 'price':'123456789012.123456'}
    assert result['row_count'] == 1 and result['complete'] is True
    assert result['columns'][1]['data_type'] == 'numeric'
    rows = list(csv.DictReader(store.resolve_artifact(result['result_artifact_id']).open(encoding='utf-8-sig')))
    assert rows == [{'product_id':'001', 'price':'123456789012.123456'}]
    assert service.authorize_snapshot(task, result['db_ref'])['query_id'] == result['query_id']
    # Additional model-owned keys are never promoted to server-owned metadata.
    ref = {**result['db_ref'], 'forged':'value'}
    assert 'forged' not in service.authorize_snapshot(task, ref)['db_ref']


def test_catalog_excludes_owner_views_and_restricted_source_exposes_only_rls_rows(db_setup):
    _, service, _ = db_setup
    catalog = service.catalog('olist')
    tables = {x['name'] for x in catalog['tables']}
    assert 'v_order_summary' in tables and 'v_order_items_agg' not in tables
    limited = service.catalog('sellers_restricted')
    assert [t['name'] for t in limited['tables']] == ['sellers']
    assert limited['restricted'] is True
    task = task_for(db_setup, source='sellers_restricted')
    result = service.query(task, 'sellers_restricted', 'SELECT seller_id,seller_city FROM olist.sellers ORDER BY seller_id')
    assert result['row_count'] == 2
    assert {row['seller_city'] for row in result['preview']} == {'santo andre'}
    assert result['scope_fingerprint'] != catalog['scope_fingerprint']
    with pytest.raises(StoreError):
        service.query(task, 'sellers_restricted', 'SELECT * FROM olist.orders')


def test_registered_selection_and_table_whitelist_are_authoritative(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup, tables=['products'])
    with pytest.raises(StoreError) as failure:
        service.query(task, 'olist', 'SELECT * FROM olist.orders')
    assert failure.value.code == 'sql_object_not_allowed'
    forged = copy.deepcopy(task)
    forged.database_tables = {}
    with pytest.raises(StoreError) as failure:
        service.query(forged, 'olist', 'SELECT * FROM olist.orders')
    assert failure.value.code == 'database_source_not_selected'
    store.update_task(task.task_id, cancel_requested=True)
    with pytest.raises(InterruptedError):
        service.query(task, 'olist', 'SELECT * FROM olist.products')


@pytest.mark.parametrize('key', ['source_id','scope_fingerprint','result_artifact_id','sql_artifact_id','metadata_artifact_id'])
def test_snapshot_ref_tampering_fails(db_setup, key):
    _, service, _ = db_setup
    task = task_for(db_setup)
    result = service.query(task, 'olist', 'SELECT COUNT(*) AS total FROM olist.products')
    ref = {**result['db_ref'], key:'forged'}
    with pytest.raises(StoreError) as failure:
        service.authorize_snapshot(task, ref)
    assert failure.value.code == 'invalid_database_snapshot'


def test_snapshot_hash_foreign_session_and_later_selection_fail(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup)
    result = service.query(task, 'olist', 'SELECT COUNT(*) AS total FROM olist.products')
    other = store.new_session()['session_id']
    other_task = task_for(db_setup, session_id=other)
    with pytest.raises(StoreError) as failure:
        service.authorize_snapshot(other_task, result['db_ref'])
    assert failure.value.code == 'database_snapshot_not_found'
    narrower = task_for(db_setup, tables=['orders'])
    with pytest.raises(StoreError) as failure:
        service.authorize_snapshot(narrower, result['db_ref'])
    assert failure.value.code == 'database_table_not_allowed'
    result_file = store.resolve_artifact(result['result_artifact_id'])
    result_file.write_text('total\n999\n', encoding='utf-8')
    with pytest.raises(StoreError):
        service.authorize_snapshot(task, result['db_ref'])


def test_scope_fingerprint_changes_invalidate_old_snapshot(db_setup, isolated_postgres):
    _, service, _ = db_setup
    task = task_for(db_setup)
    result = service.query(task, 'olist', 'SELECT COUNT(*) AS total FROM olist.products')
    with psycopg.connect(**isolated_postgres) as admin:
        admin.execute('REVOKE SELECT ON olist.products FROM ds_test_reader')
        try:
            with pytest.raises(StoreError) as failure:
                service.authorize_snapshot(task, result['db_ref'])
            assert failure.value.code == 'database_scope_changed'
        finally:
            admin.execute('GRANT SELECT ON olist.products TO ds_test_reader')


def test_batch_snapshot_shared_and_failure_publishes_no_partial_results(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup)
    results = service.query_batch(task, 'olist', [
        {'sql':'SELECT COUNT(*) AS total FROM olist.products'},
        {'sql':'SELECT SUM(price) AS total FROM olist.products'},
    ])
    assert len({r['snapshot_id'] for r in results}) == 1
    existing_artifacts = set(store._index('artifacts'))
    existing_queries = set(store._index('database_queries'))
    with pytest.raises(StoreError):
        service.query_batch(task, 'olist', [
            {'sql':'SELECT COUNT(*) AS total FROM olist.products'},
            {'sql':'SELECT 1/0 AS invalid'},
        ])
    assert set(store._index('artifacts')) == existing_artifacts
    assert set(store._index('database_queries')) == existing_queries


@pytest.mark.parametrize('fail_at', [3, 6])
def test_registration_failure_rolls_back_only_batch_indexes(db_setup, monkeypatch, fail_at):
    store, service, _ = db_setup
    task = task_for(db_setup)
    first = service.query(task, 'olist', 'SELECT 1 AS first')
    original = store.register_artifact
    calls = 0
    def fail_metadata(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError('Synthetic metadata registration failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'register_artifact', fail_metadata)
    with pytest.raises(RuntimeError):
        service.query_batch(task, 'olist', [{'sql':'SELECT 2 AS second'}, {'sql':'SELECT 3 AS third'}])
    assert len(store._index('artifacts')) == 3
    assert set(store._index('database_queries')) == {first['query_id']}
    service.authorize_snapshot(task, first['db_ref'])


def test_batch_repeatable_read_survives_external_commit(db_setup, isolated_postgres, monkeypatch):
    """A real commit between SELECTs must not change the shared batch view."""
    _, service, _ = db_setup
    task = task_for(db_setup)
    calls = 0
    class CommitBetweenQueries(RawServerCursor):
        def execute(self, query, params=None, **kwargs):
            nonlocal calls
            result = super().execute(query, params, **kwargs)
            calls += 1
            if calls == 1:
                with psycopg.connect(**isolated_postgres) as external:
                    external.execute("INSERT INTO olist.orders VALUES ('batch-visible-after-commit',30)")
            return result
    monkeypatch.setattr('src.database.query.RawServerCursor', CommitBetweenQueries)
    try:
        results = service.query_batch(task, 'olist', [
            {'sql':'SELECT COUNT(*) AS total FROM olist.orders'},
            {'sql':'SELECT COUNT(*) AS total FROM olist.orders'},
        ])
        assert [r['scalar_metrics']['total'] for r in results] == [2, 2]
        with psycopg.connect(**isolated_postgres) as external:
            assert external.execute('SELECT COUNT(*) FROM olist.orders').fetchone()[0] == 3
    finally:
        with psycopg.connect(**isolated_postgres) as external:
            external.execute("DELETE FROM olist.orders WHERE order_id='batch-visible-after-commit'")


@pytest.mark.parametrize('limit_type', ['rows','bytes'])
def test_complete_result_limits_never_publish_truncation(db_setup, limit_type):
    store, service, _ = db_setup
    task = task_for(db_setup)
    if limit_type == 'rows':
        store.settings.db_max_result_rows = 1
    else:
        store.settings.db_max_result_bytes = 1
    with pytest.raises(StoreError) as failure:
        service.query(task, 'olist', 'SELECT * FROM olist.products')
    assert failure.value.code == 'database_result_too_large'
    assert store._index('database_queries') == {}
    assert store._index('artifacts') == {}


def test_duplicate_result_names_are_rejected(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup)
    with pytest.raises(StoreError) as failure:
        service.query(task, 'olist', 'SELECT 1 AS value, 2 AS value')
    assert failure.value.code == 'duplicate_sql_columns'
    assert not store._index('database_queries')


def test_active_database_cancellation(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup)
    cancelled = threading.Event()
    # Cross joins with an arithmetic predicate create real cancellable work,
    # without permitting pg_sleep or altering the SQL function allowlist.
    sql = '''SELECT count(*) FROM olist.products a CROSS JOIN olist.products b
        CROSS JOIN olist.products c CROSS JOIN olist.products d CROSS JOIN olist.products e
        CROSS JOIN olist.products f CROSS JOIN olist.products g CROSS JOIN olist.products h
        CROSS JOIN olist.products i CROSS JOIN olist.products j CROSS JOIN olist.products k
        CROSS JOIN olist.products l CROSS JOIN olist.products m CROSS JOIN olist.products n
        CROSS JOIN olist.products o CROSS JOIN olist.products p CROSS JOIN olist.products q
        CROSS JOIN olist.products r CROSS JOIN olist.products s CROSS JOIN olist.products t
        CROSS JOIN olist.products u CROSS JOIN olist.products v CROSS JOIN olist.products w
        CROSS JOIN olist.products x CROSS JOIN olist.products y CROSS JOIN olist.products z'''
    timer = threading.Timer(.5, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            service.query(task, 'olist', sql, cancel_check=cancelled.is_set)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
    assert not store._index('database_queries')


def test_app_identity_rejects_admin_role(db_setup):
    store, service, _ = db_setup
    store.settings.db_user = 'test_admin'
    with pytest.raises(StoreError) as failure:
        service.catalog('olist')
    assert failure.value.code == 'unsafe_database_role'


@pytest.mark.parametrize('privilege', ['INSERT', 'REFERENCES', 'INSERT (price)', 'REFERENCES (product_id)'])
def test_app_identity_rejects_effective_write_privileges(db_setup, isolated_postgres, privilege):
    _, service, _ = db_setup
    with psycopg.connect(**isolated_postgres) as admin:
        admin.execute(f'GRANT {privilege} ON olist.products TO ds_test_reader')
        try:
            with pytest.raises(StoreError) as failure:
                service.catalog('olist')
            assert failure.value.code == 'unsafe_database_role'
        finally:
            admin.execute(f'REVOKE {privilege} ON olist.products FROM ds_test_reader')


def test_app_identity_rejects_schema_create(db_setup, isolated_postgres):
    _, service, _ = db_setup
    with psycopg.connect(**isolated_postgres) as admin:
        admin.execute('GRANT CREATE ON SCHEMA olist TO ds_test_reader')
        try:
            with pytest.raises(StoreError) as failure:
                service.catalog('olist')
            assert failure.value.code == 'unsafe_database_role'
        finally:
            admin.execute('REVOKE CREATE ON SCHEMA olist FROM ds_test_reader')


def test_app_identity_rejects_replication_role(db_setup, isolated_postgres):
    _, service, _ = db_setup
    with psycopg.connect(**isolated_postgres) as admin:
        admin.execute('ALTER ROLE ds_test_reader REPLICATION')
        try:
            with pytest.raises(StoreError) as failure:
                service.catalog('olist')
            assert failure.value.code == 'unsafe_database_role'
        finally:
            admin.execute('ALTER ROLE ds_test_reader NOREPLICATION')


def test_restricted_source_rejects_full_reader_misconfiguration(db_setup):
    store, service, _ = db_setup
    store.settings.db_restricted_user = store.settings.db_user
    with pytest.raises(StoreError) as failure:
        service.catalog('sellers_restricted')
    assert failure.value.code == 'unsafe_database_role'


def test_restricted_source_rejects_broadened_rls(db_setup, isolated_postgres):
    _, service, _ = db_setup
    with psycopg.connect(**isolated_postgres) as admin:
        admin.execute('ALTER POLICY limited_sellers ON olist.sellers USING (true)')
        try:
            with pytest.raises(StoreError) as failure:
                service.catalog('sellers_restricted')
            assert failure.value.code == 'unsafe_database_role'
        finally:
            admin.execute("ALTER POLICY limited_sellers ON olist.sellers USING (seller_city='santo andre')")
