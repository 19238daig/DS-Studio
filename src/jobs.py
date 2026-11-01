"""Durable Worker with isolated, supervised task processes and bounded capacity."""
from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable
from contextlib import nullcontext

from filelock import FileLock, Timeout

from src.config import Settings
from src.schemas import TaskSpec, new_id, utcnow
from src.storage import FILE_TYPES, KB_TYPES, Store, StoreError, TERMINAL

WARM_HANDOFF = 'state/worker.handoff.json'
WARM_EXECUTOR_FILE = 'state/worker.executor.json'
WARM_RESPAWN_AFTER_TASKS = 20
WARM_CANCEL_GRACE_SECONDS = 30


def _span(store, task_id, name):
    if not getattr(store.settings, 'perf_trace_enabled', False):
        return nullcontext()
    from src.performance import span
    return span(store, task_id, name)


def redact_error(exc: Exception, store: Store) -> str:
    text = f'{type(exc).__name__}: {exc}'
    key = store.settings.api_key.get_secret_value()
    if key:
        text = text.replace(key, '[REDACTED]')
    import re
    return re.sub(r'sk-[a-zA-Z0-9_-]+', '[REDACTED]', text)[:1500]


def dispatch_task(task: TaskSpec, store: Store, event: Callable, cancel_check: Callable) -> dict:
    record = store.get_task(task.task_id)
    kind = record.get('kind', task.intent)
    if kind == 'kb_refresh':
        files = store.refresh_kb()
        event('kb_refreshed', f'KB 索引已刷新，共 {len(files)} 个文件')
        dataset_ids = [f['file_id'] for f in files if Path(f['name']).suffix.lower() in KB_TYPES and f.get('profile_status') != 'ready']
        result = profile_files(task, store, dataset_ids, event, cancel_check) if dataset_ids else {}
        return dict(result, final_response=f'KB 索引已刷新，共 {len(files)} 个文件。', files=files)
    if kind == 'profile':
        return profile_files(task, store, task.file_ids, event, cancel_check)
    # KB sources are indexed at API startup and profiled lazily on first use.
    pending = []
    for file_id in task.file_ids:
        item = store.get_file(file_id, task.session_id)
        if Path(item['name']).suffix.lower() in FILE_TYPES and item.get('profile_status') != 'ready':
            pending.append(file_id)
    if pending:
        result = profile_files(task, store, pending, event, cancel_check)
        if result.get('status') == 'failed':
            return result
    try:
        with _span(store, task.task_id, 'task.import_agents'):
            module = importlib.import_module('src.agents')
    except ModuleNotFoundError as exc:
        raise RuntimeError('Agent execution is not ready; complete Agent module installation before retrying') from exc
    return module.run_task(task, store, event, cancel_check)


def profile_files(task: TaskSpec, store: Store, file_ids: list[str], event: Callable, cancel_check: Callable) -> dict:
    try:
        service = importlib.import_module('src.data').DataService(store)
    except (ModuleNotFoundError, AttributeError) as exc:
        raise RuntimeError('Dataset profiling service is not ready') from exc
    results, errors = {}, {}
    for file_id in file_ids:
        if cancel_check():
            raise InterruptedError('Task cancellation requested')
        store.update_file_metadata(file_id, task.session_id, profile_status='running', profile_task_id=task.task_id, profile_error=None)
        event('profiling', '正在解析数据概览', file_id=file_id)
        try:
            with _span(store, task.task_id, 'input.profile'):
                profiles = service.profiles(file_id, task.session_id)
            values = [p.model_dump() if hasattr(p, 'model_dump') else p for p in profiles]
            if cancel_check():
                raise InterruptedError('Task cancellation requested')
            store.update_file_metadata(file_id, task.session_id, profile_status='ready', profiles=values,
                                       profile=values[0] if values else None, profile_error=None)
            results[file_id] = values
            event('profile_ready', '数据概览解析完成', file_id=file_id, table_count=len(values))
        except InterruptedError:
            raise
        except Exception as exc:
            reason = redact_error(exc, store)
            store.update_file_metadata(file_id, task.session_id, profile_status='failed', profile_error=reason)
            errors[file_id] = reason
            event('profile_failed', reason, file_id=file_id)
    return {'status': 'failed' if errors else 'completed', 'profiles': results, 'errors': errors,
            **({'error': '; '.join(errors.values())} if errors else {})}


def execute_task(task_id: str, settings: Settings, runner: Callable | None = None) -> None:
    """Child entrypoint. Tests may supply an explicit deterministic runner."""
    store = Store(settings)
    task = TaskSpec.model_validate(store.get_task(task_id)['spec'])

    def cancelled():
        current = store.get_task(task_id)
        return current['cancel_requested'] or current['status'] in {'cancelled', 'interrupted'}

    def event(event_type: str = 'progress', message: str = '', **details):
        # Preserve already returned provider accounting even when cancellation
        # arrives during a model request. Never log prompts or model content.
        if event_type not in {'model_usage', 'model_failed'} and cancelled():
            raise InterruptedError('Task cancellation requested')
        saved = store.add_event(task_id, event_type, message, **details)
        from src.task_stream import best_effort_lifecycle
        best_effort_lifecycle(store, task_id, event_type, message, **details)
        return saved

    try:
        if cancelled():
            raise InterruptedError('Task cancellation requested')
        with _span(store, task_id, 'task.dispatch'):
            result = (runner or dispatch_task)(task, store, event, cancelled)
        if not isinstance(result, dict):
            raise TypeError('Agent runner must return a result dictionary')
        result_status = result.get('status', 'completed')
        if result_status in {'failed', 'error'}:
            raise RuntimeError(str(result.get('error') or result.get('message') or result.get('final_response') or 'Agent reported task failure'))
        if result_status not in {'completed', 'succeeded', 'waiting_user', 'needs_clarification'}:
            raise RuntimeError(f'Unsupported Agent result status: {result_status}')
        waiting = result.get('status') in {'waiting_user', 'needs_clarification'}
        status = 'waiting_user' if waiting else 'succeeded'
        response = result.get('final_response', result.get('answer', ''))
        with store.lock:
            updated = store.update_task(task_id, status=status, stage=status, result=result, final_response=response, finished_at=utcnow())
            if updated['status'] in {'succeeded', 'waiting_user'}:
                if response and store.get_task(task_id).get('kind', 'message') == 'message':
                    store.append_message(task.session_id, 'assistant', response, task_id=task_id)
                store.add_event(task_id, updated['status'], '需要补充信息' if waiting else '任务已完成')
    except InterruptedError:
        store.update_task(task_id, status='cancelled', stage='cancelled', finished_at=utcnow())
        store.finalize_profile_task(task_id, 'cancelled', 'Profile task was cancelled')
        store.add_event(task_id, 'cancelled', '任务已取消')
    except Exception as exc:
        reason = redact_error(exc, store)
        if cancelled():
            store.update_task(task_id, status='cancelled', stage='cancelled', finished_at=utcnow())
            store.finalize_profile_task(task_id, 'cancelled', 'Profile task was cancelled')
            store.add_event(task_id, 'cancelled', '任务已取消')
        else:
            store.update_task(task_id, status='failed', stage='failed', error=reason, finished_at=utcnow())
            store.finalize_profile_task(task_id, 'failed', reason)
            store.add_event(task_id, 'failed', reason)
    finally:
        from src.task_stream import best_effort_finish
        best_effort_finish(store, task_id)


def warm_executor_loop(settings: Settings, runner: Callable | None = None) -> None:
    """Resident executor body for WARM_EXECUTOR=true.

    Imports the agent stack once, then serves claimed tasks handed over via
    the handoff file addressed to this executor's id. execute_task persists a
    terminal state for every path before returning, so the coordinator keys
    completion on the task record and this process stays alive for the next
    handoff. Any crash inside execute_task is swallowed here - the terminal
    state is already on disk and the resident must survive to serve again.
    """
    store = Store(settings)
    executor_id = new_id()
    # Importing src.agents here (once) is the whole point of warm mode; it
    # delays registration by a few seconds, which the coordinator tolerates.
    importlib.import_module('src.agents')
    store.write_json(WARM_EXECUTOR_FILE, dict(executor_id=executor_id, pid=os.getpid(), updated_at=utcnow()))
    while True:
        handoff = store.read_json(WARM_HANDOFF) or {}
        if handoff.get('executor_id') == executor_id and handoff.get('task_id'):
            task_id = handoff['task_id']
            try:
                execute_task(task_id, settings, runner)
            except BaseException:
                pass
            with store.lock:
                current = store.read_json(WARM_HANDOFF) or {}
                if current.get('task_id') == task_id and current.get('executor_id') == executor_id:
                    store.write_json(WARM_HANDOFF, {})
            continue
        time.sleep(0.15)


def stop_task_containers(task_id: str) -> None:
    """Only containers carrying this exact task label may be terminated."""
    try:
        result = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=ds.task_id={task_id}'],
                                capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ids = [x for x in result.stdout.split() if all(c in '0123456789abcdef' for c in x) and len(x) >= 12]
        if ids:
            subprocess.run(['docker', 'rm', '-f', *ids], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        pass


class Worker:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.owner = new_id()
        self.lock = FileLock(str(self.store.root / 'state/worker.lock'), timeout=0)
        self.stopping = False
        self._active: dict[str, dict] = {}
        self._last_heartbeat = 0.0
        self._last_claim_attempt = 0.0
        self.executor: dict | None = None
        self.warm = bool(getattr(self.store.settings, 'warm_executor', False))

    def heartbeat(self, task_id: str | None = None) -> None:
        tasks = list(self._active) or ([task_id] if task_id else [])
        self.store.write_json('state/worker.json', dict(owner=self.owner, pid=os.getpid(), heartbeat_epoch=time.time(),
                                                      updated_at=utcnow(), task_id=tasks[0] if tasks else None,
                                                      task_ids=tasks, capacity=self._capacity(),
                                                      warm_executor=self.warm,
                                                      executor_id=self.executor['executor_id'] if self.executor else None))
        self._last_heartbeat = time.monotonic()

    def _capacity(self):
        requested = max(1, min(2, getattr(self.store.settings, 'worker_concurrency', 1)))
        if requested > 1:
            try:
                import psutil
                if psutil.virtual_memory().available < getattr(self.store.settings, 'worker_memory_reserve_mb', 1024) * 1024**2:
                    return 1
            except (ImportError, OSError):
                return 1
        return requested

    def _start_claimed(self, task, runner=None):
        tid = task['task_id']
        child = multiprocessing.get_context('spawn').Process(target=execute_task, args=(tid, self.store.settings, runner))
        try:
            with _span(self.store, tid, 'worker.spawn'):
                child.start()
            try:
                import psutil
                child_started_at = psutil.Process(child.pid).create_time()
            except (ImportError, OSError):
                child_started_at = time.time()
            self.store.update_task(tid, child_pid=child.pid, child_started_at=child_started_at)
            self._active[tid] = {'child': child, 'started': time.monotonic(), 'session_id': task['session_id']}
        except BaseException:
            # A failed spawn/accounting write must not leave an untracked job.
            if child.pid is not None:
                if child.is_alive():
                    child.terminate(); child.join(timeout=5)
                if child.is_alive():
                    child.kill(); child.join(timeout=5)
                stop_task_containers(tid)
            raise

    def _poll_active(self):
        changed = False
        for tid, entry in list(self._active.items()):
            child = entry['child']
            try:
                record = self.store.get_task(tid)
            except StoreError as exc:
                if exc.status != 404:
                    raise
                # A completed session can be deleted while its task child is
                # still exiting. Do not crash the coordinator or recreate it.
                if child.is_alive():
                    child.terminate(); child.join(timeout=5)
                if child.is_alive():
                    child.kill(); child.join(timeout=5)
                child.join(timeout=1)
                stop_task_containers(tid)
                del self._active[tid]
                changed = True
                continue
            timed_out = time.monotonic() - entry['started'] > self.store.settings.task_timeout_seconds
            terminate = child.is_alive() and (self.stopping or record['cancel_requested'] or timed_out)
            if terminate:
                child.terminate(); child.join(timeout=5)
                if child.is_alive():
                    child.kill(); child.join(timeout=5)
                stop_task_containers(tid)
                status = 'cancelled' if record['cancel_requested'] else 'failed' if timed_out else 'interrupted'
                self.store.update_task(tid, status=status, stage=status,
                    error='Task exceeded total time limit' if timed_out else 'Task execution stopped', finished_at=utcnow())
                self.store.finalize_profile_task(tid, status, 'Profile execution stopped')
                self.store.add_event(tid, status, '任务超时' if timed_out else '任务已停止')
            if not child.is_alive():
                child.join(timeout=1)
                if self.store.get_task(tid)['status'] == 'running':
                    self.store.update_task(tid, status='failed', stage='failed',
                        error=f'Task process exited without a result (code {child.exitcode})', finished_at=utcnow())
                    self.store.finalize_profile_task(tid, 'failed', 'Profile process exited without a result')
                    self.store.add_event(tid, 'failed', '执行进程异常退出')
                from src.task_stream import best_effort_finish
                best_effort_finish(self.store, tid)
                del self._active[tid]
                changed = True
        return changed


    def _warm_settings_fingerprint(self) -> str:
        import hashlib
        s = self.store.settings
        payload = json.dumps({f: str(getattr(s, f, '')) for f in (
            'api_key', 'base_url', 'workspace', 'main_model', 'analyst_model', 'report_model',
            'docker_image', 'task_timeout_seconds', 'max_tool_calls')}, sort_keys=True)
        return hashlib.md5(payload.encode()).hexdigest()

    def _stop_executor(self) -> None:
        executor = self.executor
        self.executor = None
        if not executor:
            return
        process = executor['process']
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        self.store.safe_path(WARM_EXECUTOR_FILE).unlink(missing_ok=True)

    def _ensure_executor(self, runner: Callable | None = None) -> bool:
        """Spawn the resident executor when missing or stale; True when ready."""
        if self.executor is not None:
            process = self.executor['process']
            stale = (not process.is_alive()
                     or self.executor['completed'] >= WARM_RESPAWN_AFTER_TASKS
                     or self.executor['fingerprint'] != self._warm_settings_fingerprint())
            if stale:
                self._stop_executor()
            else:
                return True
        self.executor = None
        self.store.safe_path(WARM_EXECUTOR_FILE).unlink(missing_ok=True)
        fingerprint = self._warm_settings_fingerprint()
        try:
            process = multiprocessing.get_context('spawn').Process(
                target=warm_executor_loop, args=(self.store.settings, runner), daemon=True)
            process.start()
        except Exception:
            return False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            registration = self.store.read_json(WARM_EXECUTOR_FILE) or {}
            if registration.get('pid') == process.pid and process.is_alive():
                self.executor = {'executor_id': registration['executor_id'], 'pid': process.pid,
                                 'process': process, 'fingerprint': fingerprint, 'completed': 0}
                return True
            if not process.is_alive():
                return False
            time.sleep(0.1)
        return False

    def _handoff_to_executor(self, task_id: str) -> bool:
        executor = self.executor
        if not executor:
            return False
        self.store.write_json(WARM_HANDOFF, dict(task_id=task_id, executor_id=executor['executor_id'],
                                                 handed_at=utcnow()))
        return True

    def _poll_warm(self):
        """Warm supervision: completion by task record, cooperative cancel
        with a force-kill grace window, resident-death respawn."""
        changed = False
        for tid, entry in list(self._active.items()):
            try:
                record = self.store.get_task(tid)
            except StoreError as exc:
                if exc.status != 404:
                    raise
                del self._active[tid]
                changed = True
                continue
            if record['status'] in TERMINAL:
                if self.executor:
                    self.executor['completed'] += 1
                del self._active[tid]
                changed = True
                continue
            if record['cancel_requested'] and 'cancel_since' not in entry:
                entry['cancel_since'] = time.monotonic()
            timed_out = time.monotonic() - entry['started'] > self.store.settings.task_timeout_seconds
            grace_elapsed = 'cancel_since' in entry and time.monotonic() - entry['cancel_since'] > WARM_CANCEL_GRACE_SECONDS
            if timed_out or grace_elapsed:
                status = 'cancelled' if record['cancel_requested'] else 'failed'
                self._stop_executor()
                stop_task_containers(tid)
                self.store.update_task(tid, status=status, stage=status,
                    error='Task exceeded total time limit' if timed_out else 'Task execution stopped', finished_at=utcnow())
                self.store.finalize_profile_task(tid, status, 'Profile execution stopped')
                self.store.add_event(tid, status, '任务超时' if timed_out else '任务已停止')
                del self._active[tid]
                changed = True
                continue
            if not self.executor or not self.executor['process'].is_alive():
                stop_task_containers(tid)
                self.store.update_task(tid, status='failed', stage='failed',
                    error='Task executor exited without a result', finished_at=utcnow())
                self.store.finalize_profile_task(tid, 'failed', 'Task executor exited without a result')
                self.store.add_event(tid, 'failed', '执行进程异常退出')
                del self._active[tid]
                changed = True
        return changed

    def _cycle(self, runner=None):
        """Coordinator tick. Caller owns the exclusive Worker lifetime lock.

        Every claim is still serialized by Store.lock and same-session predecessor
        rules. Different sessions may use the second explicitly enabled slot.
        No ThreadPool shares a TaskRuntime or model graph across jobs.
        """
        if self.warm:
            changed = self._poll_warm()
            if not self.stopping and time.monotonic() - self._last_claim_attempt >= .4:
                self._last_claim_attempt = time.monotonic()
                while len(self._active) < self._capacity():
                    if not self._ensure_executor(runner):
                        break  # resident not ready yet; retry next tick
                    task = self.claim()
                    if task is None:
                        break
                    if self._handoff_to_executor(task['task_id']):
                        self._active[task['task_id']] = {'started': time.monotonic(),
                                                         'session_id': task['session_id']}
                        changed = True
        else:
            changed = self._poll_active()
            if not self.stopping and time.monotonic() - self._last_claim_attempt >= .4:
                self._last_claim_attempt = time.monotonic()
                while len(self._active) < self._capacity():
                    task = self.claim()
                    if task is None:
                        break
                    try:
                        self._start_claimed(task, runner)
                    except Exception as exc:
                        self.store.update_task(task['task_id'], status='failed', stage='failed',
                            error=redact_error(exc, self.store), finished_at=utcnow())
                        from src.task_stream import best_effort_finish
                        best_effort_finish(self.store, task['task_id'])
                    changed = True
        if changed or time.monotonic() - self._last_heartbeat >= .5:
            self.heartbeat()
        return bool(self._active) or changed

    def recover(self) -> None:
        # Caller holds the exclusive Worker lock. A restarted API never runs this.
        if self.warm:
            # A resident executor from a previous worker must not survive the
            # coordinator that owns it; its in-flight task is failed below.
            registration = self.store.read_json(WARM_EXECUTOR_FILE) or {}
            if registration.get('pid'):
                try:
                    import psutil
                    process = psutil.Process(registration['pid'])
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        process.kill()
                except (ImportError, OSError, ProcessLookupError):
                    pass
                except Exception:
                    pass
            self.store.safe_path(WARM_EXECUTOR_FILE).unlink(missing_ok=True)
            self.store.safe_path(WARM_HANDOFF).unlink(missing_ok=True)
        for task in self.store.list_tasks():
            if task['status'] == 'running':
                child_pid = task.get('child_pid')
                if child_pid:
                    try:
                        import psutil
                        process = psutil.Process(child_pid)
                        if abs(process.create_time() - task.get('child_started_at', 0)) < 2:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except psutil.TimeoutExpired:
                                process.kill()
                    except (ImportError, OSError, ProcessLookupError):
                        pass
                    except Exception:
                        pass
                stop_task_containers(task['task_id'])
                status = 'cancelled' if task['cancel_requested'] else 'interrupted'
                self.store.update_task(task['task_id'], status=status, stage=status, error='Previous Worker stopped before task completion', finished_at=utcnow())
                self.store.finalize_profile_task(task['task_id'], status, 'Previous Worker stopped before profile completion')
                self.store.add_event(task['task_id'], status, '前次执行中断；可以手动重新运行')

    def claim(self) -> dict | None:
        with self.store.lock:
            tasks = self.store.list_tasks()
            for task in tasks:
                if task['status'] != 'queued' or task['cancel_requested']:
                    continue
                if task['session_id'] in {entry['session_id'] for entry in self._active.values()}:
                    continue
                prior = [x for x in tasks if x['session_id'] == task['session_id'] and x['created_at'] < task['created_at']]
                if any(x['status'] not in TERMINAL for x in prior):
                    continue
                claimed = self.store.update_task(task['task_id'], status='running', stage='starting', owner=self.owner,
                                                 execution_id=new_id(), started_at=utcnow(),
                                                 estimate_seconds=self.store.estimate_seconds('message'))
                self.store.add_event(task['task_id'], 'running', 'Worker 已领取任务')
                from src.task_stream import best_effort_lifecycle
                best_effort_lifecycle(self.store, task['task_id'], 'running')
                return claimed
            return None

    def run_once(self, runner: Callable | None = None, subprocess_mode: bool = True) -> bool:
        with self.lock:
            task = self.claim()
            if task is None:
                self.heartbeat()
                return False
            task_id = task['task_id']
            self.heartbeat(task_id)
            if not subprocess_mode:
                execute_task(task_id, self.store.settings, runner)
                self.heartbeat()
                return True
            with _span(self.store, task_id, 'worker.spawn'):
                child = multiprocessing.get_context('spawn').Process(target=execute_task, args=(task_id, self.store.settings, runner))
                child.start()
            started = time.monotonic()
            try:
                import psutil
                child_started_at = psutil.Process(child.pid).create_time()
            except ImportError:
                child_started_at = time.time()
            self.store.update_task(task_id, child_pid=child.pid, child_started_at=child_started_at)
            while child.is_alive():
                self.heartbeat(task_id)
                current = self.store.get_task(task_id)
                timed_out = time.monotonic() - started > self.store.settings.task_timeout_seconds
                if self.stopping or current['cancel_requested'] or timed_out:
                    child.terminate()
                    child.join(timeout=5)
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=5)
                    stop_task_containers(task_id)
                    status = 'cancelled' if current['cancel_requested'] else 'failed' if timed_out else 'interrupted'
                    self.store.update_task(task_id, status=status, stage=status,
                                           error='Task exceeded total time limit' if timed_out else 'Task execution stopped', finished_at=utcnow())
                    self.store.finalize_profile_task(task_id, status, 'Profile execution stopped')
                    self.store.add_event(task_id, status, '任务超时' if timed_out else '任务已停止')
                    break
                child.join(timeout=0.5)
            child.join(timeout=1)
            if self.store.get_task(task_id)['status'] == 'running':
                self.store.update_task(task_id, status='failed', stage='failed', error=f'Task process exited without a result (code {child.exitcode})', finished_at=utcnow())
                self.store.finalize_profile_task(task_id, 'failed', 'Profile process exited without a result')
                self.store.add_event(task_id, 'failed', '执行进程异常退出')
            from src.task_stream import best_effort_finish
            best_effort_finish(self.store, task_id)
            self.heartbeat()
            return True

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        def stop(signum, frame):
            self.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        acquired = False
        try:
            with self.lock:
                acquired = True
                self.recover()
                while not self.stopping or self._active:
                    self._cycle()
                    time.sleep(min(poll_seconds, .2))
        except Timeout as exc:
            raise RuntimeError('Another Worker already owns the task coordinator') from exc
        finally:
            self._stop_executor()
            if acquired:
                with self.store.lock:
                    current = self.store.read_json('state/worker.json', {})
                    if current.get('owner') == self.owner:
                        self.store.write_json('state/worker.json', dict(owner=self.owner, pid=os.getpid(), heartbeat_epoch=0, updated_at=utcnow(), task_id=None))


def main():
    multiprocessing.freeze_support()
    Worker().run_forever()


if __name__ == '__main__':
    main()
