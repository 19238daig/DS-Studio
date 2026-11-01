"""Serve only the built React application; API and asset 404s stay 404s."""
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from src.config import ROOT

def register_web(application: FastAPI, dist: Path | None = None) -> None:
    directory = (dist or ROOT/'frontend'/'dist').resolve()
    if (directory/'assets').is_dir():
        application.mount('/assets', StaticFiles(directory=directory/'assets'), name='web-assets')

    def index():
        path = directory/'index.html'
        if not path.is_file():
            return HTMLResponse('<html lang="zh-CN"><meta charset="utf-8"><title>DS Studio</title>'
                '<h1>DS Studio 前端尚未构建</h1><p>请在 frontend 目录运行 npm ci 和 npm run build。</p></html>',
                status_code=503)
        return FileResponse(path, media_type='text/html', headers={'Cache-Control':'no-cache'})

    application.add_api_route('/', index, methods=['GET'], include_in_schema=False)

    @application.get('/sessions/{web_path:path}', include_in_schema=False)
    @application.get('/settings/{web_path:path}', include_in_schema=False)
    def spa(web_path: str):
        parts = web_path.split('/')
        if ('..' in parts or any('.' in part or '\\' in part for part in parts)):
            raise HTTPException(404, 'Not found')
        return index()
