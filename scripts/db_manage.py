"""Local administrator CLI; credentials are loaded from .env, never CLI arguments."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.config import Settings
from src.database.importer import bootstrap, import_dataset, migrate, status
from src.storage import StoreError


def main():
    parser=argparse.ArgumentParser(description='DS Project PostgreSQL 18 本机管理（非 Agent 工具）')
    parser.add_argument('command',choices=['status','bootstrap','migrate','import'])
    args=parser.parse_args()
    try:
        result={'status':status,'bootstrap':bootstrap,'migrate':migrate,'import':import_dataset}[args.command](Settings())
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0
    except StoreError as exc:
        print(json.dumps({'error':str(exc),'code':exc.code},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
