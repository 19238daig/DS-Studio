"""Read-only catalog of delivered report versions for one session."""
import json
from pathlib import PurePosixPath

from src.schemas import Artifact
from src.storage import Store, StoreError


def list_report_versions(store: Store, session_id: str, tasks: list[dict]) -> list[dict]:
    # One index read for the whole session; never infer outputs by scanning disk.
    by_task: dict[str, list[Artifact]] = {}
    for value in store._index('artifacts').values():
        if value.get('session_id') == session_id:
            artifact = Artifact.model_validate(value)
            by_task.setdefault(artifact.task_id, []).append(artifact)
    versions = []
    for task in tasks:
        if task['session_id'] != session_id or task['status'] != 'succeeded':
            continue
        delivered = set((task.get('result') or {}).get('report_ids', []))
        artifacts = by_task.get(task['task_id'], [])
        groups: dict[str, list[Artifact]] = {}
        for artifact in artifacts:
            if artifact.kind == 'report' and artifact.artifact_id in delivered:
                directory = str(PurePosixPath(artifact.relative_path.replace('\\', '/')).parent)
                groups.setdefault(directory, []).append(artifact)
        for directory, reports in groups.items():
            manifests = [a for a in artifacts if a.kind == 'report_manifest'
                         and str(PurePosixPath(a.relative_path.replace('\\', '/')).parent) == directory]
            manifest = {}
            if len(manifests) == 1:
                try:
                    candidate = json.loads(store.resolve_artifact(manifests[0].artifact_id).read_text(encoding='utf-8'))
                    if candidate.get('session_id') == session_id and candidate.get('task_id') == task['task_id']:
                        manifest = candidate
                except (StoreError, ValueError, OSError):
                    # History remains visible if optional descriptive metadata
                    # is unavailable; downloads still enforce file checksums.
                    pass
            versions.append(dict(version=PurePosixPath(directory).name,
                task_id=task['task_id'], created_at=min(a.created_at for a in reports),
                title=(manifest.get('presentation') or {}).get('title') or task['spec']['request'],
                request=task['spec']['request'], revision_of=manifest.get('revision_of'),
                artifacts=[dict(a.model_dump(), downloadable=True) for a in sorted(reports, key=lambda a: a.name)]))
    versions.sort(key=lambda item: (item['created_at'], item['version']))
    for number, version in enumerate(versions, 1):
        version['number'] = number
    numbers = {artifact['artifact_id']: version['number'] for version in versions for artifact in version['artifacts']}
    for version in versions:
        version['base_number'] = numbers.get((version.get('revision_of') or {}).get('report_id'))
    return list(reversed(versions))
