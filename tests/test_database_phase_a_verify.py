"""Independent regressions for explicit-empty database table authorization."""
import pytest

from src.storage import StoreError
from test_database_core import db_setup, isolated_postgres, task_for


def test_missing_table_scope_retains_visible_source_tables(db_setup):
    _, service, _ = db_setup
    task = task_for(db_setup)
    result = service.query(task, 'olist', 'SELECT count(*) AS total FROM olist.orders')
    assert result['scalar_metrics'] == {'total': 2}


def test_explicit_empty_table_scope_rejects_business_queries_not_catalog(db_setup):
    store, service, _ = db_setup
    task = task_for(db_setup, tables=[])
    assert 'orders' in {table['name'] for table in service.catalog('olist')['tables']}
    with pytest.raises(StoreError) as failure:
        service.query(task, 'olist', 'SELECT count(*) AS total FROM olist.orders')
    assert failure.value.code == 'sql_object_not_allowed'
    assert not store._index('database_queries')
    assert not store._index('artifacts')


def test_explicit_empty_table_scope_rejects_prior_business_snapshots(db_setup):
    _, service, _ = db_setup
    full_task = task_for(db_setup)
    result = service.query(full_task, 'olist', 'SELECT count(*) AS total FROM olist.orders')
    empty_task = task_for(db_setup, tables=[])
    with pytest.raises(StoreError) as failure:
        service.authorize_snapshot(empty_task, result['db_ref'])
    assert failure.value.code == 'database_table_not_allowed'
