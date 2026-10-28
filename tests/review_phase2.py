"""Independent data and real Docker review, with no model requests."""
import json
from pathlib import Path
import shutil
import subprocess

import pandas as pd
import pytest
from openpyxl import Workbook

from src.config import Settings
from src.data import DataService
from src.execution import DockerExecutor, MAX_FILE, MAX_LOG
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(_env_file=None, workspace=tmp_path, api_key='review-only-host-secret'))
    sid = store.new_session()['session_id']
    return store, sid, DataService(store)


def dataset(context, text='id,value\n001,2\n002,3\n', name='a.csv', encoding='utf-8'):
    store, sid, _ = context
    path = store.root / 'KB' / name
    path.write_text(text, encoding=encoding)
    return next(x['file_id'] for x in store.refresh_kb() if x['name'] == name)


def task(context, files):
    store, sid, _ = context
    spec = TaskSpec(session_id=sid, request='Independent review', file_ids=files)
    store.create_task(spec)
    return spec


def test_result_preview_preserves_leading_zero_and_na_literals(context):
    store, sid, data = context
    fid = dataset(context, 'id,value,label\n001,2,NA\n002,3,null\n003,4,N/A\n', encoding='gb18030')
    result = data.analyze(task(context, [fid]), fid, 'rows')
    preview = data.read_result_table(result.table_ids[0], sid, limit=2)
    assert [x['id'] for x in preview['preview']] == ['001', '002']
    assert [x['label'] for x in preview['preview']] == ['NA', 'null']
    assert preview['total_rows'] == 3 and preview['truncated']
    assert 'preview' in preview['scope']
    other = store.new_session()['session_id']
    with pytest.raises(StoreError, match='not found'):
        data.read_result_table(result.table_ids[0], other)


def test_aggregate_uses_all_rows_and_changed_source_invalidates_analysis(context):
    store, sid, data = context
    fid = dataset(context, 'x,value\n' + ''.join(f'{i},{i}\n' for i in range(1000)))
    result = data.analyze(task(context, [fid]), fid, 'aggregate', aggregations={'value': ['sum', 'count']})
    assert result.metrics['value_sum'] == 499500
    assert result.metrics['value_count'] == 1000
    (store.root / 'KB' / 'a.csv').write_text('x,value\n1,1\n')
    with pytest.raises(StoreError, match='changed'):
        store.get_analysis(result.analysis_id, sid)


def test_bad_rows_empty_filter_and_unselected_source_rejected(context):
    _, sid, data = context
    bad = dataset(context, 'a,b\n1,2\n3\n', name='bad.csv')
    with pytest.raises(StoreError, match='width'):
        data.read_table(bad, sid)
    fid = dataset(context)
    spec = task(context, [fid])
    with pytest.raises(StoreError, match='No rows'):
        data.analyze(spec, fid, 'rows', filters=[{'column': 'value', 'op': 'gt', 'value': 100}])
    with pytest.raises(StoreError, match='selected'):
        data.analyze(spec, bad, 'rows')


def test_multisheet_html_and_formula_boundaries(context):
    store, sid, data = context
    path = store.root / 'KB' / 'multi.xlsx'
    book = Workbook()
    book.active.title = 'first'
    book.active.append(['id', 'value'])
    book.active.append(['001', '=1+2'])
    book.create_sheet('second').append(['x'])
    book['second'].append([5])
    book.save(path)
    fid = next(x['file_id'] for x in store.refresh_kb() if x['name'] == 'multi.xlsx')
    assert len(data.list_tables(fid, sid)) == 2
    assert any('lack cached' in w for w in data.profiles(fid, sid)[0].quality_warnings)
    with pytest.raises(StoreError, match='explicitly'):
        data.read_table(fid, sid)
    assert data.read_table(fid, sid, 'first').iloc[0]['id'] == '001'
    html = dataset(context, '<script>private()</script><p>Info</p><img src="http://127.0.0.1:1/x"><table><tr><th>id</th></tr><tr><td>001</td></tr></table><table><tr><th>v</th></tr><tr><td>2</td></tr></table>', name='multi.html')
    assert len(data.list_tables(html, sid)) == 2
    assert data.read_table(html, sid, 'table_1').iloc[0]['id'] == '001'
    assert 'private' not in data.read_text(html, sid)['text']


def test_merge_explosion_and_foreign_artifact_registration_rejected(context):
    store, sid, data = context
    left = dataset(context, 'id,v\n' + '1,2\n' * 1100)
    right = dataset(context, 'id,w\n' + '1,3\n' * 1100, name='right.csv')
    spec = task(context, [left, right])
    with pytest.raises(StoreError, match='1210000'):
        data.analyze(spec, left, 'merge', right_file_id=right, on=['id'])
    outsider = store.root / 'KB' / 'outsider.csv'
    outsider.write_text('x\n1\n')
    with pytest.raises(StoreError, match='current task'):
        store.register_artifact(outsider, spec.task_id, sid, 'table')


@pytest.fixture
def sandbox(context):
    available = subprocess.run(['docker', 'image', 'inspect', 'ds-analysis:local'], capture_output=True)
    assert available.returncode == 0, 'Phase2 requires the existing sandbox image'
    fid = dataset(context)
    store, _, _ = context
    return store, task(context, [fid]), DockerExecutor(store)


def result_code(extra=''):
    return extra + '\nimport json\njson.dump({"metrics":{"x":1},"metric_definitions":{"x":"One computed row"}},open("/output/result.json","w"))\n'


def test_real_docker_isolation_and_computed_output(sandbox):
    store, spec, executor = sandbox
    path = executor.input_manifest(spec)[0]['path']
    script = f'''
import os, socket, pandas as pd
assert os.getuid() == 65534
assert not any(x in os.environ for x in ('API_KEY','OPENAI_API_KEY','DASHSCOPE_API_KEY'))
assert 'review-only-host-secret' not in str(dict(os.environ))
assert not os.path.exists('/var/run/docker.sock')
try:
    open({path!r}, 'a').write('tamper')
    raise AssertionError('Writable input')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
    raise AssertionError('External network allowed')
except OSError:
    pass
df=pd.read_csv({path!r}, dtype={{'id':str}})
assert df.value.sum() == 5
df.to_csv('/output/result.csv',index=False)
'''
    script += '\nimport json\njson.dump({"metrics":{"total":int(df.value.sum())},"metric_definitions":{"total":"All selected input rows, sum(value), missing omitted"},"tables":["result.csv"]},open("/output/result.json","w"))'
    result = executor.run_python(spec, script)
    assert result.metrics == {'total': 5}
    assert store.get_analysis(result.analysis_id, spec.session_id).table_ids


@pytest.mark.parametrize('extra,match', [
    ('open("/output/large.txt","wb").write(b"x"*(26*1024*1024))', '25 MB'),
    ('import os\nos.symlink("/etc/passwd","/output/escape.txt")', 'symlinks'),
])
def test_real_docker_rejects_large_or_reparse_output(sandbox, extra, match):
    store, spec, executor = sandbox
    with pytest.raises(StoreError, match=match):
        executor.run_python(spec, result_code(extra))
    assert not store._index('analyses')
    assert not any(a.kind in {'table', 'execution_result'} for a in store.list_artifacts(spec.task_id))


def test_real_timeout_stops_container(sandbox):
    store, spec, executor = sandbox
    with pytest.raises(StoreError, match='timeout'):
        executor.run_python(spec, 'import time\ntime.sleep(20)', timeout_seconds=1)
    containers = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=ds.task_id={spec.task_id}'], capture_output=True, text=True)
    assert not containers.stdout.strip()
    assert not store._index('analyses')


def test_quick_exiting_script_cannot_bypass_log_limit(sandbox):
    store, spec, executor = sandbox
    code = result_code('import sys\nsys.stdout.write("x"*(3*1024*1024))\nsys.stdout.flush()')
    with pytest.raises(StoreError, match='log size limit'):
        executor.run_python(spec, code)
    assert not store._index('analyses')
