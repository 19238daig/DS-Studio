"""React delivery and explicit report-version contracts, with real validators."""
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from src.api import create_app
from src.config import Settings
from src.schemas import TaskSpec
from src.reporting import ReportService
from src.storage import file_hash
from test_general_revision_runtime import report_context


def test_spa_does_not_swallow_api_assets_or_private_paths(tmp_path):
    dist=tmp_path/'dist';(dist/'assets').mkdir(parents=True)
    (dist/'index.html').write_text('<html>React workbench</html>',encoding='utf-8')
    (dist/'assets'/'main.js').write_text('console.log("ok")',encoding='utf-8')
    client=TestClient(create_app(Settings(workspace=tmp_path/'workspace',_env_file=None),frontend_dist=dist))
    for path in ['/', '/sessions/example/chat','/sessions/example/reports/abc']:
        assert client.get(path).status_code==200
        assert 'React workbench' in client.get(path).text
    assert client.get('/assets/main.js').status_code==200
    for path in ['/api/v1/missing','/assets/missing.js','/.env','/KB/private.csv','/sessions/foo/script.js']:
        assert client.get(path).status_code==404,path
    assert client.get('/docs').status_code==200
    assert client.post('/api/v1/database/sql',json={'sql':'SELECT 1'}).status_code==404
    assert client.get('/api/v1/health').json()['status']=='ok'


def test_missing_build_keeps_api_available(tmp_path):
    client=TestClient(create_app(Settings(workspace=tmp_path,_env_file=None),frontend_dist=tmp_path/'absent'))
    assert client.get('/').status_code==503
    assert client.get('/api/v1/health').status_code==200


def test_safe_preview_requires_delivered_generated_report(report_context):
    store,task,fid,chart,mean,base=report_context
    client=TestClient(create_app(store.settings))
    report=base.report_ids[0];before=file_hash(store.resolve_artifact(report))
    response=client.get(f'/api/v1/artifacts/{report}/preview')
    assert response.status_code==200,response.text
    assert response.headers['content-disposition'].startswith('inline')
    assert 'sandbox allow-scripts allow-downloads' in response.headers['content-security-policy']
    assert response.headers['cache-control']=='private, no-store'
    assert file_hash(store.resolve_artifact(report))==before
    assert client.get(f'/api/v1/artifacts/{chart.table_ids[0]}/preview').status_code==422
    source=store.get_artifact(report).task_id
    # Terminal states are immutable through update_task; seed a genuinely failed
    # source record to exercise the delivery gate rather than a no-op transition.
    record=store.get_task(source)
    record['status']='failed'
    store.write_json(f'state/tasks/{source}.json',record)
    assert client.get(f'/api/v1/artifacts/{report}/preview').status_code==422


def test_explicit_old_report_pins_same_version_and_preserves_sources(report_context):
    store,task,fid,chart,mean,base=report_context
    other=TaskSpec(session_id=task.session_id,request='另一份报告',file_ids=[fid])
    store.create_task(other)
    new=ReportService(store).generate(other,[mean.analysis_id],'另一份报告',formats=['html'])
    store.update_task(other.task_id,status='succeeded',result=new.model_dump())
    client=TestClient(create_app(store.settings))
    payload={'content':'修改旧版标题','file_ids':[fid],'base_report_id':base.report_ids[0],'expected_outputs':['html']}
    response=client.post(f'/api/v1/sessions/{task.session_id}/messages',json=payload,headers={'Idempotency-Key':'edit-old'})
    assert response.status_code==202,response.text
    record=store.get_task(response.json()['task_id'])
    plan=record['spec']['constraints']['report_revision']
    assert plan['base_report_id']==base.report_ids[0] and plan['base_report_hash']==store.get_artifact(base.report_ids[0]).hash
    assert set(record['spec']['analysis_ids'])==set(base.analysis_ids)
    assert client.post(f'/api/v1/sessions/{task.session_id}/messages',json=payload,headers={'Idempotency-Key':'edit-old'}).json()['task_id']==record['task_id']
    different={**payload,'base_report_id':new.report_ids[0]}
    assert client.post(f'/api/v1/sessions/{task.session_id}/messages',json=different,headers={'Idempotency-Key':'edit-old'}).status_code==409
    assert client.post(f'/api/v1/sessions/{task.session_id}/messages',json={**payload,'file_ids':[]}).status_code==403
    foreign=store.new_session()['session_id']
    assert client.post(f'/api/v1/sessions/{foreign}/messages',json=payload).status_code==404


def test_openapi_exposes_web_contracts_without_secret_values(tmp_path):
    app=create_app(Settings(workspace=tmp_path,_env_file=None,api_key='private-fixture-key'))
    schema=app.openapi();models=schema['components']['schemas']
    assert 'base_report_id' in models['MessageRequest']['properties']
    for name in ['SessionResponse','TaskResponse','ArtifactResponse','TelemetryResponse','FileResponseModel']:
        assert name in models
    assert 'private-fixture-key' not in str(schema)
