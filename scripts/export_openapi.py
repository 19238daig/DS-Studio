"""Export public contracts without including local credentials or datasets."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.api import create_app
from src.config import Settings

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='ds-contract-') as directory:
        schema = create_app(Settings(workspace=Path(directory), _env_file=None)).openapi()
        (ROOT / 'frontend/openapi.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Exported frontend/openapi.json; no local configuration included.')
