"""Environment checks and explicitly requested, small paid model probes."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def request(base_url, api_key, body):
    req = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        except Exception:
            payload = {}
        error = payload.get('error', {})
        return exc.code, {'safe_error': {'code': error.get('code'), 'type': error.get('type')}}
    except Exception as exc:
        return 0, {'safe_error': {'type': type(exc).__name__}}

def probe_models(tool_round=False, thinking=False):
    from src.config import get_settings
    settings = get_settings()
    for model in dict.fromkeys([settings.main_model, settings.analyst_model]):
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 16, 'enable_thinking': False}
        status, data = request(settings.base_url, settings.api_key.get_secret_value(), body)
        print(json.dumps({'http': status, 'requested_model': model, 'returned_model': data.get('model'), 'usage': data.get('usage'), 'error': data.get('safe_error')}, ensure_ascii=True))
        if status in (401, 403):
            break
        if status != 200 or not tool_round:
            continue
        body['messages'] = [{'role': 'user', 'content': 'Use add to compute 2 + 3, then answer the tool result.'}]
        body['tools'] = [{'type': 'function', 'function': {'name': 'add', 'description': 'Add two integers.', 'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b']}}}]
        body['tool_choice'] = {'type': 'function', 'function': {'name': 'add'}}
        body['max_tokens'] = 128
        if thinking and model == settings.analyst_model:
            body['enable_thinking'] = True
            body['thinking_budget'] = 256
            body['max_tokens'] = 512
            body.pop('tool_choice', None)
        status, data = request(settings.base_url, settings.api_key.get_secret_value(), body)
        calls = data.get('choices', [{}])[0].get('message', {}).get('tool_calls', [])
        if status == 200 and calls:
            message = data['choices'][0]['message']
            call = calls[0]
            args = json.loads(call['function']['arguments'])
            body['messages'].extend([message, {'role': 'tool', 'tool_call_id': call['id'], 'content': str(args['a'] + args['b'])}])
            body.pop('tool_choice', None)
            status2, data2 = request(settings.base_url, settings.api_key.get_secret_value(), body)
            final = data2.get('choices', [{}])[0].get('message', {}).get('content', '')
            print(json.dumps({'model': model, 'tool_http': status, 'final_http': status2, 'tool_id_preserved': bool(call['id']), 'tool_round_ok': '5' in (final or '')}))
        else:
            print(json.dumps({'model': model, 'tool_http': status, 'tool_round_ok': False, 'error': data.get('safe_error')}))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', action='store_true', help='Make small paid API probes.')
    parser.add_argument('--tool-round', action='store_true')
    parser.add_argument('--thinking', action='store_true', help='Verify analyst thinking/tool compatibility with a 256-token budget.')
    args = parser.parse_args()
    print(json.dumps({'python': sys.version.split()[0], 'docker': bool(shutil.which('docker')), 'git': bool(shutil.which('git'))}))
    for package in ['deepagents', 'fastapi', 'streamlit', 'langchain-openai']:
        try:
            print(f'{package}=={importlib.metadata.version(package)}')
        except importlib.metadata.PackageNotFoundError:
            print(f'{package}: not installed')
    if args.models:
        probe_models(args.tool_round, args.thinking)

if __name__ == '__main__':
    main()
