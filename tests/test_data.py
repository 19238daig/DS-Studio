from pathlib import Path
import shutil

import pandas as pd
import pytest
from openpyxl import Workbook

from src.config import Settings
from src.data import DataService
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def setup(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    return store, sid, DataService(store)


def register(setup, name, content=None, encoding='utf-8'):
    store, sid, _ = setup
    path = store.root / 'input' / sid / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_text(content, encoding=encoding)
    return path, lambda: store.register_file(path, name, sid)['file_id']


def task_for(setup, *files):
    store, sid, _ = setup
    task = TaskSpec(session_id=sid, request='test', file_ids=list(files))
    store.create_task(task)
    return task


@pytest.mark.parametrize('name,rows,quality_mean', [('winequality-white.csv', 4898, 5.877909350755410), ('winequality-red.csv', 1599, 5.6360225140712945)])
def test_wine_full_exact(setup, name, rows, quality_mean):
    store, sid, data = setup
    source = Path(__file__).parents[1] / 'KB' / name
    if not source.is_file():
        pytest.skip('Optional external Wine Quality dataset is not distributed; place it in local KB/ to run this regression')
    path, save = register(setup, name)
    shutil.copyfile(source, path)
    fid = save()
    profile = data.profile(fid, sid)
    assert profile.row_count == rows and len(profile.columns) == 12
    result = data.analyze(task_for(setup, fid), fid, 'aggregate', aggregations={'quality': ['mean', 'count']})
    assert result.metrics['quality_mean'] == pytest.approx(quality_mean)
    assert result.metrics['quality_count'] == rows
    assert data.read_result_table(result.table_ids[0], sid)['preview'][0]['quality_count'] == rows


def test_encoding_id_bad_lines_and_quality(setup):
    _, sid, data = setup
    _, save = register(setup, 'encoded.csv', '编号;数值\n001;3\n002;\n001;3\n', 'gb18030')
    fid = save()
    frame = data.read_table(fid, sid)
    assert frame['编号'].tolist() == ['001', '002', '001']
    profile = data.profile(fid, sid)
    assert 'Duplicate full rows: 1' in profile.quality_warnings
    assert any('数值' in w and '1' in w for w in profile.quality_warnings)
    result = data.analyze(task_for(setup, fid), fid, 'rows')
    assert data.read_result_table(result.table_ids[0], sid)['preview'][0]['编号'] == '001'
    _, bad = register(setup, 'bad.csv', 'a,b\n1,2,3\n4\n')
    with pytest.raises(StoreError, match='width'):
        data.profile(bad(), sid)


def test_excel_multiple_sheets_and_uncached_formula(setup):
    _, sid, data = setup
    path, save = register(setup, 'book.xlsx')
    book = Workbook()
    sheet = book.active
    sheet.title = 'first'
    sheet.append(['id', 'value'])
    sheet.append(['001', '=1+1'])
    second = book.create_sheet('second')
    second.append(['x']); second.append([7])
    book.save(path)
    fid = save()
    profiles = data.profiles(fid, sid)
    assert [p.sheet_or_table for p in profiles] == ['first', 'second']
    assert any('lack cached' in w for w in profiles[0].quality_warnings)
    assert data.read_table(fid, sid, 'first').iloc[0]['id'] == '001'
    with pytest.raises(StoreError, match='explicitly'):
        data.read_table(fid, sid)


def test_html_static_multiple_tables_and_text(setup):
    _, sid, data = setup
    html = '<h1>说明</h1><script>bad_secret()</script><img src="http://127.0.0.1:1/no"><table><tr><th>ID</th><th>v</th></tr><tr><td>001</td><td>4</td></tr></table><table><tr><th>b</th></tr><tr><td>6</td></tr></table>'
    _, save = register(setup, 'tables.html', html)
    fid = save()
    assert len(data.profiles(fid, sid)) == 2
    assert data.read_table(fid, sid, 'table_1').iloc[0]['ID'] == '001'
    text = data.read_text(fid, sid)
    assert '说明' in text['text'] and 'bad_secret' not in text['text']
    _, plain = register(setup, 'plain.html', '<p>正文无表格</p>')
    assert data.profile(plain(), sid).row_count == 0


def test_aggregate_filter_merge_and_authorization(setup):
    _, sid, data = setup
    _, save = register(setup, 'a.csv', 'id,group,value\n1,A,2\n1,A,4\n2,B,10\n3,B,\n')
    left = save()
    _, save = register(setup, 'b.csv', 'id,name\n1,one\n1,uno\n2,two\n')
    right = save()
    task = task_for(setup, left, right)
    grouped = data.analyze(task, left, 'aggregate', group_by=['group'], aggregations={'value':['sum','mean','count']})
    preview = data.read_result_table(grouped.table_ids[0], sid)['preview']
    assert preview[0] == {'group':'A','value_sum':6,'value_mean':3.0,'value_count':2}
    filtered = data.analyze(task, left, 'aggregate', filters=[{'column':'value','op':'ge','value':4}], aggregations={'value':'mean'})
    assert filtered.metrics['value_mean'] == 7
    merged = data.analyze(task, left, 'merge', right_file_id=right, on=['id'], how='left')
    assert merged.metrics['result_rows'] == 6 and merged.metrics['left_key_match_rate'] == .75
    assert merged.metrics['right_duplicate_keys'] == 1
    with pytest.raises(StoreError, match='No rows'):
        data.analyze(task, left, 'rows', filters=[{'column':'value','op':'gt','value':99}])
    with pytest.raises(StoreError, match='Unknown column'):
        data.analyze(task, left, 'rows', columns=['absent'])
    with pytest.raises(StoreError, match='selected'):
        data.analyze(task_for(setup, left), right, 'rows')
    with pytest.raises(StoreError, match='not found'):
        data.read_result_table(grouped.table_ids[0], 'other_session')


def test_merge_explosion_rejected_before_join(setup):
    _, _, data = setup
    _, a = register(setup, 'a.csv', 'id,x\n' + '1,2\n' * 1100)
    _, b = register(setup, 'b.csv', 'id,y\n' + '1,3\n' * 1100)
    left, right = a(), b()
    with pytest.raises(StoreError, match='1210000'):
        data.analyze(task_for(setup, left, right), left, 'merge', right_file_id=right, on=['id'])


def test_correlation_chart_and_injection_rejected(setup):
    store, sid, data = setup
    _, save = register(setup, 'a.csv', 'x,y\n1,2\n2,4\n3,6\n')
    fid = save(); task = task_for(setup, fid)
    result = data.analyze(task, fid, 'correlation', columns=['x', 'y'])
    assert data.read_result_table(result.table_ids[0], sid)['preview'][0]['y'] == 1
    chart = data.analyze(task, fid, 'rows', chart='scatter')
    assert store.resolve_artifact(chart.chart_ids[0]).read_bytes().startswith(b'\x89PNG')
    with pytest.raises(StoreError, match='Allowed aggregation'):
        data.analyze(task, fid, 'aggregate', aggregations={'x':'__import__("os").system("whoami")'})


def test_empty_csv_and_duplicate_headers(setup):
    _, sid, data = setup
    _, empty = register(setup, 'empty.csv', 'x\n')
    fid = empty()
    with pytest.raises(StoreError, match='No rows'):
        data.analyze(task_for(setup, fid), fid, 'rows')
    _, duplicate = register(setup, 'duplicate.csv', 'x,x\n1,2\n')
    # Duplicate/merged headers are repaired in place (x, x_2) instead of
    # rejecting the whole file.
    profile = data.profile(duplicate(), sid)
    assert profile.columns == ['x', 'x_2']
