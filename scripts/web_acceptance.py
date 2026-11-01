"""Isolated real-API browser acceptance server. Never calls a model provider.

Uses synthetic data, normal Store/DataService/ReportService validators and Worker
dispatch. The deterministic runner is injected only here, never in src.api.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config import Settings
from src.storage import Store
from src.schemas import TaskSpec, utcnow
from src.data import DataService
from src.reporting import ReportService
from src.jobs import Worker, dispatch_task
from src.api import create_app


def runner(task, store, event, cancelled):
    if store.get_task(task.task_id).get('kind') != 'message':
        return dispatch_task(task, store, event, cancelled)
    event('delegation', '主 Agent 将任务分配给数据分析 Agent', role='main', target='analyst')
    if '需要补充示例' in task.request and not task.parent_task_id:
        return {'status':'waiting_user','final_response':'请确认需要分析的月份。'}
    for _ in range(40 if '取消' in task.request else 3):
        if cancelled():
            raise InterruptedError('Cancelled')
        time.sleep(.15)
    if task.constraints.get('report_revision'):
        plan = task.constraints['report_revision']
        result = ReportService(store).generate(task, task.analysis_ids, title='修订后的收入分析',
            formats=['html'], base_report_id=plan['base_report_id'])
        return dict(result.model_dump(), final_response='报告已生成。')
    fid = task.file_ids[0] if task.file_ids else store.refresh_kb()[0]['file_id']
    task.file_ids = [fid]
    result = DataService(store).analyze(task, fid, 'aggregate', aggregations={'amount':'mean'})
    if task.expected_outputs:
        report = ReportService(store).generate(task, [result.analysis_id], '收入分析', formats=task.expected_outputs)
        return dict(report.model_dump(), final_response='报告已生成。')
    return {'analysis_ids':[result.analysis_id], 'final_response':f"平均收入为 {result.metrics['amount_mean']:g}。"}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18180)
    args=parser.parse_args()
    workspace=args.workspace.resolve()
    # Require an explicitly isolated new workspace; never reset production data.
    if (workspace/'state/sessions').exists() and any((workspace/'state/sessions').iterdir()):
        raise SystemExit('Use a fresh acceptance workspace, existing sessions are preserved.')
    settings=Settings(workspace=workspace, _env_file=None, api_key='offline-acceptance', db_enabled=False)
    store=Store(settings)
    (workspace/'KB/sales').mkdir(parents=True, exist_ok=True)
    (workspace/'KB/sales/revenue.csv').write_text('month,amount\nJan,10\nFeb,20\nMar,30\n',encoding='utf-8')
    fid=store.refresh_kb()[0]['file_id']
    long=store.new_session('长会话性能验收')
    long['messages']=[{'message_id':f'long{i}','role':'user' if i%2==0 else 'assistant',
        'content':f'历史记录 {i}：已验证的合成数据结论。','created_at':utcnow(),'task_id':None} for i in range(1000)]
    store.write_json(f"state/sessions/{long['session_id']}.json",long)
    sid=store.new_session('收入分析与报告修订')['session_id']
    original=TaskSpec(session_id=sid, request='按月份生成收入报告', file_ids=[fid],expected_outputs=['html'])
    store.create_task(original)
    store.append_message(sid,'user',original.request,task_id=original.task_id)
    chart=DataService(store).analyze(original,fid,'aggregate',group_by=['month'],aggregations={'amount':'sum'},chart='bar')
    mean=DataService(store).analyze(original,fid,'aggregate',aggregations={'amount':'mean'})
    first=ReportService(store).generate(original,[chart.analysis_id,mean.analysis_id],'月度收入分析',
        {'summary':'平均收入为 {{metric:'+mean.analysis_id+':amount_mean}}。'},['html'])
    store.update_task(original.task_id,status='succeeded',result=first.model_dump(),final_response='报告已生成。')
    store.append_message(sid,'assistant','报告已生成。',task_id=original.task_id)
    revised=TaskSpec(session_id=sid, request='修改标题，其余保持原样',file_ids=[fid],expected_outputs=['html'])
    store.create_task(revised)
    store.append_message(sid,'user',revised.request,task_id=revised.task_id)
    second=ReportService(store).generate(revised,first.analysis_ids,'月度收入分析 · 修订版',formats=['html'],base_report_id=first.report_ids[0])
    store.update_task(revised.task_id,status='succeeded',result=second.model_dump(),final_response='报告已生成。')
    store.append_message(sid,'assistant','报告已生成。',task_id=revised.task_id)
    (workspace/'fixture.json').write_text(json.dumps({'session_id':sid,'file_id':fid,'original':first.report_ids[0],
        'revised':second.report_ids[0]},indent=2),encoding='utf-8')
    worker=Worker(store)
    def loop():
        while True:
            worker.run_once(runner=runner,subprocess_mode=False)
            time.sleep(.1)
    threading.Thread(target=loop,daemon=True).start()
    import uvicorn
    uvicorn.run(create_app(settings),host='127.0.0.1',port=args.port,access_log=False)


if __name__=='__main__':
    main()
