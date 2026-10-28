"""Opt-in small live Main→Analyst→data-tools test through the Worker entrypoint."""
import argparse
import csv
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Authorize small paid model calls.')
    args = parser.parse_args()
    if not args.live:
        print('No model calls made. Pass --live to run the paid integration smoke test.')
        return
    from src.config import Settings
    from src.storage import Store
    from src.schemas import TaskSpec
    from src.jobs import execute_task
    workspace = ROOT / '.runtime' / 'smoke_agents'
    (workspace / 'KB').mkdir(parents=True, exist_ok=True)
    expected = {}
    for name in ('winequality-red.csv', 'winequality-white.csv'):
        source = ROOT / 'KB' / name
        shutil.copy2(source, workspace / 'KB' / name)
        with source.open(encoding='utf-8-sig', newline='') as stream:
            values = [float(row['quality']) for row in csv.DictReader(stream, delimiter=';')]
        expected[name] = {'rows': len(values), 'quality_mean': sum(values) / len(values)}
    settings = Settings(workspace=workspace)
    store = Store(settings)
    files = store.refresh_kb()
    session = store.new_session('真实 Agent 葡萄酒小型验证')
    task = TaskSpec(session_id=session['session_id'], file_ids=[f['file_id'] for f in files],
        request='请委派数据分析专家，分别统计选中红、白葡萄酒两个文件的完整数据行数和quality均值。用固定aggregate工具即可，不生成报告，不做额外分析。')
    store.create_task(task)
    store.append_message(task.session_id, 'user', task.request, task_id=task.task_id)
    execute_task(task.task_id, settings)
    record = store.get_task(task.task_id)
    result = record.get('result') or {}
    output = {'task_id': task.task_id, 'workspace': str(workspace), 'status': record['status'],
              'tool_calls': result.get('tool_calls'), 'analyses': []}
    for aid in result.get('analysis_ids', []):
        analysis = store.get_analysis(aid, task.session_id)
        names = [store.get_file(fid, task.session_id)['name'] for fid in analysis.input_versions]
        output['analyses'].append({'names': names, 'metrics': analysis.metrics})
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if record['status'] != 'succeeded':
        print('Live integration did not succeed:', result.get('final_response') or record.get('error'))
        raise SystemExit(1)
    checked = set()
    for item in output['analyses']:
        if len(item['names']) == 1:
            name = item['names'][0]
            metrics = item['metrics']
            if name in expected and metrics.get('source_rows') == expected[name]['rows'] and abs(metrics.get('quality_mean', -1) - expected[name]['quality_mean']) < 1e-10:
                checked.add(name)
    if checked != set(expected):
        raise SystemExit('Output contract did not contain independently verified metrics for both files.')
    print('PASS: actual Worker + Deep Agents delegation + both full-data metrics verified.')


if __name__ == '__main__':
    main()
