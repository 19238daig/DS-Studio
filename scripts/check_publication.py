"""Read-only checks of the actual Git index/commit before public publication.

Never prints matched secret values or offending file contents.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


PRIVATE_ROOTS = {'.runtime', '.venv', '.git', '.pytest_cache', 'kb', 'input', 'data',
                 'datasets', 'testing_dataset', 'local', 'state', 'memory',
                 'report', 'script_output', '__pycache__', 'node_modules'}
DATA_EXTENSIONS = {'.csv', '.tsv', '.xls', '.xlsx', '.parquet', '.arrow', '.feather',
                   '.jsonl', '.db', '.sqlite', '.sqlite3', '.dump', '.backup',
                   '.zip', '.7z', '.pem', '.key', '.p12'}
SECRET_PATTERNS = {
    'provider_key': re.compile(rb'\bsk-(?:[0-9a-fA-F]{20,}|(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,})\b'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'aws_access_key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
}
REQUIRED = {'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.env.example'}


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], input=data,
                            capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Git operation failed; initialize the repository and stage the intended files first.')
    return result.stdout


def upstream_design_reference(path: str) -> bool:
    return (path.startswith('skills/design/ui-ux-pro-max/data/') or
            path.startswith('skills/design/ui-ux-pro-max/scripts/tests/fixtures/catalogs/')) and path.endswith('.csv')


def local_secrets(root: Path) -> list[tuple[str, bytes]]:
    path = root / '.env'
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if not match:
            continue
        name, value = match.groups()
        if not re.search(r'(?:API_KEY|PASSWORD|TOKEN|SECRET)$', name):
            continue
        value = value.strip('"\'')
        if len(value) >= 8:
            found.append((name, value.encode('utf-8')))
    return found


def audit(root: Path, revision: str | None = None) -> dict:
    records = []
    if revision:
        entries = git(root, 'ls-tree', '-rz', '--full-tree', revision).split(b'\0')
        for entry in filter(None, entries):
            metadata, path = entry.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            records.append((mode, oid, path.decode('utf-8')))
    else:
        entries = git(root, 'ls-files', '--stage', '-z').split(b'\0')
        for entry in filter(None, entries):
            metadata, path = entry.split(b'\t', 1)
            mode, oid, stage = metadata.decode().split()
            if stage != '0':
                raise RuntimeError('Resolve the Git index conflicts before publication.')
            records.append((mode, oid, path.decode('utf-8')))
    issues = []
    paths = {path for _, _, path in records}
    for path in sorted(REQUIRED - paths):
        issues.append({'path': path, 'reason': 'missing_publication_document'})
    if not records:
        return {'ok': False, 'file_count': 0, 'total_bytes': 0, 'issues': issues}
    stream = io.BytesIO(git(root, 'cat-file', '--batch', data=b'\n'.join(
        oid.encode() for _, oid, _ in records) + b'\n'))
    secrets = local_secrets(root)
    total = 0
    for mode, _, path in records:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b'blob':
            raise RuntimeError('Only regular source files may be published by this check.')
        size = int(header[2]); content = stream.read(size); stream.read(1)
        total += size
        parsed = PurePosixPath(path)
        if (parsed.parts[0].casefold() in PRIVATE_ROOTS or '__pycache__' in parsed.parts or
                (parsed.name.startswith('.env') and path != '.env.example') or
                path.casefold() == '.streamlit/secrets.toml' or
                parsed.suffix.lower() == '.log'):
            issues.append({'path': path, 'reason': 'private_or_runtime_path'})
        if parsed.suffix.lower() in DATA_EXTENSIONS and not upstream_design_reference(path):
            issues.append({'path': path, 'reason': 'dataset_archive_or_private_material'})
        if mode not in {'100644', '100755'}:
            issues.append({'path': path, 'reason': 'non_regular_file'})
        if size > 10 * 1024 * 1024:
            issues.append({'path': path, 'reason': 'unexpected_large_file'})
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                issues.append({'path': path, 'reason': name})
        for name, secret in secrets:
            present = secret in content
            # A short password can also be an ordinary SQL identifier (for
            # example a role name). Require credential context in that case;
            # never publish a literal password assignment or credential URL.
            if present and name.endswith('PASSWORD') and len(secret) < 12:
                escaped = re.escape(secret)
                assignment = rb'(?i)[a-z_]*password[a-z_]*\s*[=:]\s*[\x22\x27]?' + escaped + rb'(?:[\x22\x27\s,;]|$)'
                url = rb'://[^\s/:]+:' + escaped + rb'@'
                present = bool(re.search(assignment, content) or re.search(url, content))
            if present:
                issues.append({'path': path, 'reason': 'local_secret_value', 'configuration_name': name})
    return {'ok': not issues, 'file_count': len(records), 'total_bytes': total,
            'design_reference_csv_count': sum(upstream_design_reference(p) for p in paths),
            'issues': issues}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', help='Inspect a commit such as HEAD instead of the Git index')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = audit(root, args.revision)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
