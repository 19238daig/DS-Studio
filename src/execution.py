"""Execute untrusted analytical Python only inside a resource-limited Docker container."""
from __future__ import annotations

import csv
import ast
import json
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

from openpyxl import load_workbook

from src.schemas import AnalysisResult, TaskSpec, new_id
from src.storage import Store, StoreError

MAX_FILE = 25 * 1024 * 1024
MAX_OUTPUT = 100 * 1024 * 1024
MAX_LOG = 2 * 1024 * 1024


def validate_analysis_script(code: str) -> None:
    """Parse only; untrusted code is still executed exclusively in Docker."""
    if not code.strip() or len(code.encode('utf-8')) > 100000:
        raise StoreError('Python script must be nonempty and at most 100 KB', 422, 'invalid_python')
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as exc:
        raise StoreError(f'Python语法校验失败（行 {getattr(exc,"lineno",None)}）：{getattr(exc,"msg",str(exc))}。请提交完整的精简脚本，检查字符串是否闭合。', 422, 'invalid_python') from exc
    if not any(isinstance(node, ast.Constant) and isinstance(node.value, str) and 'result.json' in node.value
               for node in ast.walk(tree)):
        raise StoreError('脚本未声明 result.json 输出。必须实际写入 /output/result.json，包含非空metrics及逐项metric_definitions；只print或导出CSV不算完成。先保存JSON，再绘制必要图表。', 422, 'missing_result_contract')
RESULT_CONTRACT = '''Write /output/result.json as strict JSON (no NaN/Infinity):
{"metrics":{"metric_name":123},"metric_definitions":{"metric_name":"exact calculation, row scope, missing-value handling"},
 "tables":["summary.csv"],"charts":["plot.png"],"findings":[],"limitations":[]}.
metrics must be nonempty and every metric must have a definition. All values must derive from actual computations.
Persist full computed precision in metrics and numeric result tables: use float(r), never round(r, 4),
formatted strings or a rounded temporary such as r_rounded. Do not round before writing result.json.
User-requested display decimals apply only to chart labels or rendered prose, not stored source metrics.
Example: r = float(df['x'].corr(df['y'])); metrics = {'pearson_r': r}; label = f'{r:.4f}'.
Tables/charts are optional relative filenames under /output. Allowed files: .json, .csv, .xlsx, .png, .svg, .txt.
All report charts must be true vector SVG. Use Matplotlib fig.savefig('/output/plot.svg').
The runner automatically exports paired SVG and PNG from Matplotlib figures, even when saving .png.
Use vector artists (bar/line/scatter/boxplot/pie/pcolormesh), NOT imshow/raster images or rasterized=True.
SVG must not contain scripts, event handlers, external URLs or embedded bitmap images.
Use all applicable data; state any explicit filters. No network or secrets are available.
Chinese and English plot labels are supported by the bundled CJK fonts; use the user's language.
The runner verifies glyph coverage before exporting SVG paths and the paired PNG. Do not replace Chinese labels with English to hide a font problem.
pandas CSV separators must be set explicitly.
The bundled image uses pandas 3 and Matplotlib 3.11. For boxplot use tick_labels=[...], NOT labels=[...].
Do not use removed APIs such as DataFrame.append; use pandas.concat. No seaborn package is installed.
Before json.dump, convert NumPy/Pandas scalars to native int/float/bool with .item() and arrays to .tolist().
Use allow_nan=False and map missing/non-finite values to None. Do not use default=str to hide numeric type errors.
'''


def unsafe_link(path: Path) -> bool:
    # Linux-created symlinks on Docker Desktop mounts may be Windows reparse points.
    try:
        info = path.lstat()
        return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 1024)
    except OSError as exc:
        raise StoreError(f'Output path cannot be inspected (symlinks/reparse points forbidden): {path.name}', 422, 'unsafe_output') from exc


class DockerExecutor:
    def __init__(self, store: Store):
        self.store = store

    def input_manifest(self, task: TaskSpec, *, db_refs: list[dict] | None = None) -> list[dict]:
        result = []
        total = 0
        for file_id in dict.fromkeys(task.file_ids):
            path = self.store.resolve_file(file_id, task.session_id)
            record = self.store.get_file(file_id, task.session_id)
            total += path.stat().st_size
            result.append(dict(file_id=file_id, name=record['name'], path=f'/input/{file_id}{path.suffix.lower()}', hash=record['hash']))
        if db_refs:
            from src.database import DatabaseService
            database = DatabaseService(self.store)
            seen = set()
            for ref in db_refs:
                metadata = database.authorize_snapshot(task, ref)
                if metadata['query_id'] in seen:
                    continue
                seen.add(metadata['query_id'])
                artifact = self.store.get_artifact(metadata['result_artifact_id'])
                path = self.store.resolve_artifact(artifact.artifact_id)
                total += path.stat().st_size
                result.append(dict(source_type='database', source_id=metadata['source_id'],
                    query_id=metadata['query_id'], name='SQL query result',
                    result_artifact_id=artifact.artifact_id, hash=artifact.hash, db_ref=metadata['db_ref'],
                    columns=metadata['columns'], row_count=metadata['row_count'],
                    path=f'/previous_output/{metadata["query_id"]}.csv'))
        if not result:
            raise StoreError('Select at least one input dataset', 422, 'missing_input')
        if total > self.store.settings.max_task_input_bytes:
            raise StoreError('Selected input files exceed the task size limit', 422, 'input_too_large')
        return result

    def _stop(self, name):
        try:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _json(path):
        def reject(value):
            raise ValueError(f'Nonfinite JSON number: {value}')
        return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=reject)

    def _validate_outputs(self, output: Path) -> tuple[dict, list[Path]]:
        files = []
        for path in output.rglob('*'):
            if unsafe_link(path) or not path.resolve().is_relative_to(output.resolve()):
                raise StoreError('Output symlinks and escaping paths are forbidden', 422, 'unsafe_output')
            if path.is_file():
                files.append(path)
        if len(files) > 100 or sum(p.stat().st_size for p in files) > MAX_OUTPUT:
            raise StoreError('Sandbox output exceeds file count or total size limit', 422, 'output_too_large')
        for path in files:
            if path.stat().st_size > MAX_FILE or path.stat().st_size == 0:
                raise StoreError(f'Output file empty or larger than 25 MB: {path.name}', 422, 'invalid_output')
            extension = path.suffix.lower()
            try:
                if extension == '.json':
                    self._json(path)
                elif extension == '.png':
                    import matplotlib.image as mpimg
                    image = mpimg.imread(path)
                    if image.ndim not in (2, 3) or not image.size:
                        raise ValueError('Invalid PNG dimensions')
                elif extension == '.svg':
                    from src.chart_svg import parse_svg
                    parse_svg(path.read_bytes())
                elif extension == '.xlsx':
                    book = load_workbook(path, read_only=True, data_only=True)
                    try:
                        for sheet in book:
                            for _ in sheet:
                                pass
                    finally:
                        book.close()
                elif extension == '.csv':
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        rows = csv.reader(stream, strict=True)
                        first = next(rows)
                        for row in rows:
                            if row and len(row) != len(first):
                                raise ValueError('CSV output has inconsistent row width')
                elif extension == '.txt':
                    path.read_text(encoding='utf-8')
                else:
                    raise ValueError('Allowed output extensions: .json/.csv/.xlsx/.png/.svg/.txt')
            except Exception as exc:
                raise StoreError(f'Invalid output {path.name}: {exc}', 422, 'invalid_output') from exc
        result_path = output / 'result.json'
        if result_path not in files:
            raise StoreError('Execution did not create /output/result.json', 422, 'missing_result')
        result = self._json(result_path)
        if not isinstance(result, dict) or set(result) - {'metrics', 'metric_definitions', 'tables', 'charts', 'findings', 'limitations'}:
            raise StoreError('result.json has invalid top-level fields', 422, 'invalid_result')
        metrics, definitions = result.get('metrics'), result.get('metric_definitions')
        if not isinstance(metrics, dict) or not metrics or len(metrics) > 200 or not isinstance(definitions, dict):
            raise StoreError('result.json requires nonempty metrics and metric_definitions dictionaries (max 200 metrics)', 422)
        for key in metrics:
            if not isinstance(definitions.get(key), str) or not definitions[key].strip():
                raise StoreError(f'Metric {key!r} lacks its calculation definition', 422, 'undefined_metric')
        for field in ('findings', 'limitations'):
            value = result.get(field, [])
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or len(x) > 5000 for x in value):
                raise StoreError(f'{field} must be a list of bounded strings', 422)
        for field, extensions in (('tables', {'.csv', '.xlsx'}), ('charts', {'.png', '.svg'})):
            names = result.get(field, [])
            if not isinstance(names, list) or len(names) > 50:
                raise StoreError(f'{field} must be a list of at most 50 output filenames', 422)
            for name in names:
                if not isinstance(name, str) or '\\' in name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                    raise StoreError('Output references must be safe relative paths', 422, 'unsafe_output')
                candidate = output.joinpath(name)
                if candidate not in files or candidate.suffix.lower() not in extensions:
                    raise StoreError(f'Referenced output is missing or has wrong type: {name}', 422, 'invalid_output')
        return result, files

    def run_python(self, task: TaskSpec, code: str, timeout_seconds: int | None = None, *, db_refs: list[dict] | None = None) -> AnalysisResult:
        if not code.strip() or len(code.encode('utf-8')) > 100000:
            raise StoreError('Python script must be nonempty and at most 100 KB', 422)
        manifest = self.input_manifest(task, db_refs=db_refs)
        source_ids = list(dict.fromkeys(task.file_ids + [r['source_id'] for r in manifest if r.get('source_type') == 'database']))
        timeout = min(self.store.settings.sandbox_timeout_seconds if timeout_seconds is None else timeout_seconds, self.store.settings.sandbox_timeout_seconds)
        if timeout < 1:
            raise StoreError('Execution timeout must be positive', 422)
        if self.store.get_task(task.task_id).get('cancel_requested'):
            raise InterruptedError('Task cancellation requested')
        execution_id = new_id()
        base = self.store.safe_path(f'script_output/{task.session_id}/{task.task_id}')
        scripts, output, logs = base / 'scripts' / execution_id, base / 'runs' / execution_id, base / 'logs' / execution_id
        for directory in (scripts, output, logs):
            directory.mkdir(parents=True, exist_ok=False)
        script = scripts / 'main.py'
        script.write_text(code, encoding='utf-8')
        for helper in ('chart_export.py', 'sandbox_chart_runner.py'):
            shutil.copyfile(Path(__file__).with_name(helper), scripts / helper)
        (scripts / 'input_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        script_artifact = self.store.register_artifact(script, task.task_id, task.session_id, 'script', source_ids)
        name = f'ds-{task.task_id[:32]}-{execution_id[:12]}'
        command = ['docker', 'run', '--rm', '--name', name, '--label', f'ds.task_id={task.task_id}',
            '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', '65534:65534', '--pids-limit', '64', '--memory', self.store.settings.sandbox_memory,
            '--cpus', self.store.settings.sandbox_cpus, '--ulimit', 'nofile=128:128',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m', '--workdir', '/output']
        for record in manifest:
            path = (self.store.resolve_artifact(record['result_artifact_id']) if record.get('source_type') == 'database'
                    else self.store.resolve_file(record['file_id'], task.session_id))
            if ',' in str(path):
                raise StoreError('Docker mount source cannot contain commas', 422)
            command.extend(['--mount', f'type=bind,src={path},dst={record["path"]},readonly'])
        command.extend(['--mount', f'type=bind,src={scripts},dst=/script,readonly',
            '--mount', f'type=bind,src={output},dst=/output', self.store.settings.docker_image, '-I', '-B', '/script/sandbox_chart_runner.py'])
        stdout_path, stderr_path = logs / 'stdout.txt', logs / 'stderr.txt'
        process = None
        reason = None
        try:
            with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                started = time.monotonic()
                while process.poll() is None:
                    if self.store.get_task(task.task_id).get('cancel_requested'):
                        reason = 'cancelled'
                        break
                    if time.monotonic() - started > timeout:
                        reason = f'Execution exceeded {timeout} second timeout'
                        break
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_LOG:
                        reason = 'Execution log size limit exceeded'
                        break
                    if sum(p.stat().st_size for p in output.rglob('*') if not unsafe_link(p) and p.is_file()) > MAX_OUTPUT:
                        reason = 'Execution output size limit exceeded'
                        break
                    time.sleep(.1)
                if reason:
                    self._stop(name)
                    process.kill()
                process.wait(timeout=20)
                # A short-lived process may exit between polls after writing its
                # final burst. Recheck completed logs before any result validation.
                if not reason and stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_LOG:
                    reason = 'Execution log size limit exceeded'
        except OSError as exc:
            raise StoreError(f'Docker execution unavailable: {exc}', 503, 'docker_unavailable') from exc
        finally:
            self._stop(name)
            for path in (stdout_path, stderr_path):
                if path.exists():
                    self.store.register_artifact(path, task.task_id, task.session_id, 'log', source_ids)
        if reason == 'cancelled':
            raise InterruptedError('Task cancellation requested')
        if reason:
            raise StoreError(reason + '; script/stdout/stderr retained in task artifacts', 422, 'execution_limit')
        if process.returncode:
            error = stderr_path.read_text(encoding='utf-8', errors='replace')[-4000:]
            raise StoreError(f'Python execution failed (exit {process.returncode}): {error}', 422, 'execution_failed')
        result, files = self._validate_outputs(output)
        artifacts = {}
        for path in files:
            relative = path.relative_to(output).as_posix()
            if relative not in result.get('charts', []) and any(path == output.joinpath(name).with_suffix('.svg' if Path(name).suffix.lower() == '.png' else '.png') for name in result.get('charts', [])):
                continue
            kind = 'table' if relative in result.get('tables', []) else 'chart' if relative in result.get('charts', []) else 'execution_result'
            artifacts[relative] = self.store.register_artifact(path, task.task_id, task.session_id, kind, source_ids)
        for name in result.get('charts', []):
            companion = output.joinpath(name).with_suffix('.svg' if Path(name).suffix.lower() == '.png' else '.png')
            if companion in files:
                self.store.register_artifact(companion, task.task_id, task.session_id,
                    'chart_svg' if companion.suffix == '.svg' else 'chart_preview', [artifacts[name].artifact_id])
        analysis = AnalysisResult(task_id=task.task_id, session_id=task.session_id,
            input_versions={r['file_id']: r['hash'] for r in manifest if 'file_id' in r},
            db_refs=[r['db_ref'] for r in manifest if r.get('source_type') == 'database'], metrics=result['metrics'],
            metric_definitions={key: dict(definition=value, source_ids=source_ids, execution_id=execution_id,
                result_artifact_id=artifacts['result.json'].artifact_id) for key, value in result['metric_definitions'].items()},
            table_ids=[artifacts[name].artifact_id for name in result.get('tables', [])],
            chart_ids=[artifacts[name].artifact_id for name in result.get('charts', [])], script_ids=[script_artifact.artifact_id],
            findings=result.get('findings', []), limitations=result.get('limitations', []))
        # Recheck source versions after execution, before publishing the result.
        for file_id in task.file_ids:
            self.store.resolve_file(file_id, task.session_id)
        if db_refs:
            from src.database import DatabaseService
            for ref in db_refs:
                DatabaseService(self.store).authorize_snapshot(task, ref)
        return self.store.save_analysis(analysis)
