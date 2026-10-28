"""Real Docker integration tests; no mocked sandbox success path."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from src.config import Settings
from src.execution import DockerExecutor
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def setup(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker CLI is required for sandbox integration tests')
    available = subprocess.run(['docker', 'image', 'inspect', 'ds-analysis:local'], capture_output=True)
    if available.returncode:
        pytest.skip('Build ds-analysis:local to run Docker integration tests')
    store = Store(Settings(workspace=tmp_path, api_key='host-only-test-secret', _env_file=None))
    sid = store.new_session()['session_id']
    path = store.root / 'KB' / 'wine.csv'
    # Small synthetic fixture keeps sandbox security tests independent of any
    # private/downloaded dataset. External wine regressions live in test_data.
    path.write_text('quality;alcohol\n4;9.5\n6;10.0\n8;11.5\n',encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='test execution', file_ids=[fid])
    store.create_task(task)
    return store, task, DockerExecutor(store)


def test_real_container_statistics_and_security(setup):
    store, task, executor = setup
    path = executor.input_manifest(task)[0]['path']
    code = f'''
import json, os, socket
import pandas as pd
assert os.getuid() == 65534
assert not any(key in os.environ for key in ('API_KEY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY', 'AWS_SECRET_ACCESS_KEY'))
assert 'host-only-test-secret' not in str(dict(os.environ))
try:
    open({path!r}, 'a').write('tamper')
    raise AssertionError('input writable')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
    raise AssertionError('network permitted')
except OSError:
    pass
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/workspace/.env')
df = pd.read_csv({path!r}, sep=';')
summary = df[['quality']].agg(['mean', 'count']).reset_index()
summary.to_csv('/output/summary.csv', index=False)
json.dump({{'metrics':{{'quality_mean':float(df.quality.mean()),'rows':len(df)}},
 'metric_definitions':{{'quality_mean':'Mean of quality over all input rows; missing omitted','rows':'All data rows'}},
 'tables':['summary.csv'], 'charts':[], 'findings':['Actual container result'], 'limitations':[]}}, open('/output/result.json','w'))
print('container checks passed')
'''
    result = executor.run_python(task, code)
    assert result.metrics == {'quality_mean':pytest.approx(6.0),'rows':3}
    assert len(result.table_ids) == 1 and len(result.script_ids) == 1
    assert store.get_analysis(result.analysis_id, task.session_id).input_versions
    assert any('container checks passed' in store.resolve_artifact(a.artifact_id).read_text() for a in store.list_artifacts(task.task_id) if a.kind == 'log')


def test_timeout_retains_error_and_cleans_container(setup):
    store, task, executor = setup
    with pytest.raises(StoreError, match='timeout'):
        executor.run_python(task, 'import time\nprint("started", flush=True)\ntime.sleep(60)', timeout_seconds=2)
    artifacts = store.list_artifacts(task.task_id)
    assert {a.kind for a in artifacts} == {'script', 'log'}
    containers = subprocess.run(['docker','ps','-aq','--filter',f'label=ds.task_id={task.task_id}'], capture_output=True, text=True)
    assert not containers.stdout.strip()


@pytest.mark.parametrize('payload,message', [
    ({'metrics':{'x':1},'metric_definitions':{'x':'all rows'},'tables':['../../outside.csv']}, 'relative paths'),
    ({'metrics':{'x':1},'metric_definitions':{},'tables':[]}, 'definition'),
])
def test_invalid_result_never_published(setup, payload, message):
    store, task, executor = setup
    code = 'import json\njson.dump(' + repr(payload) + ',open("/output/result.json","w"))'
    with pytest.raises(StoreError, match=message):
        executor.run_python(task, code)
    assert not store._index('analyses')
    assert not any(a.kind == 'table' for a in store.list_artifacts(task.task_id))


def test_failed_script_and_nonfinite_result(setup):
    store, task, executor = setup
    with pytest.raises(StoreError, match='division by zero'):
        executor.run_python(task, 'print(1/0)')
    with pytest.raises(StoreError, match='Nonfinite'):
        executor.run_python(task, 'open("/output/result.json","w").write(\'{"metrics":{"x":NaN},"metric_definitions":{"x":"x"}}\')')
    assert not store._index('analyses')


def test_symlink_and_corrupt_file_rejected(setup):
    store, task, executor = setup
    with pytest.raises(StoreError, match='symlinks'):
        executor.run_python(task, 'import os\nos.symlink("/etc/passwd","/output/stolen.txt")')
    with pytest.raises(StoreError, match='Invalid output'):
        executor.run_python(task, 'open("/output/plot.png","w").write("not a PNG")')
    assert not store._index('analyses')


def test_fast_exit_log_limit_is_checked_after_poll_loop(tmp_path, monkeypatch):
    """Force poll() to observe an already-exited process, bypassing loop checks."""
    from src.execution import MAX_LOG
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = store.root / 'KB' / 'data.csv'
    path.write_text('x\n1\n')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='fast exit boundary', file_ids=[fid])
    store.create_task(task)
    executor = DockerExecutor(store)

    class AlreadyExited:
        returncode = 0

        def __init__(self, command, stdout, stderr, **kwargs):
            stdout.write(b'x' * (MAX_LOG + 1))
            stdout.flush()

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    monkeypatch.setattr('src.execution.subprocess.Popen', AlreadyExited)
    monkeypatch.setattr(executor, '_stop', lambda name: None)
    with pytest.raises(StoreError, match='log size limit'):
        executor.run_python(task, 'print("fast exit")')
    assert not store._index('analyses')
    assert {a.kind for a in store.list_artifacts(task.task_id)} == {'script', 'log'}
