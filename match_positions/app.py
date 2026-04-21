"""
Web-based viewer for match_step_component_positions.py output.

Shows the assembly STEP file in 3-D with matched component instances highlighted,
plus a panel of per-match pose data (xyz, rpy, anchor origin, rotation matrix).

Usage:
    python app.py --step assembly.step --matches matches.json
    python app.py --step assembly.step --matches matches.json --port 9000
"""

import json
import sys
import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

SCRIPT_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.mount('/static', StaticFiles(directory=SCRIPT_DIR / 'static'), name='static')

session: dict = {
    'filename': None,
    'original_text': None,
    'matches': None,
}


def _load_matches(matches_path: Path) -> dict:
    data = json.loads(matches_path.read_text(encoding='utf-8'))
    matches = data.get('matches')
    if not isinstance(matches, list):
        raise ValueError(f'Match file has no "matches" list: {matches_path}')
    return data


@app.get('/')
async def index():
    html = (SCRIPT_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    return HTMLResponse(html)


@app.get('/api/step-file')
async def get_step_file():
    if not session['original_text']:
        return JSONResponse({'error': 'No file loaded'}, status_code=404)
    return Response(
        content=session['original_text'].encode('utf-8'),
        media_type='application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{session["filename"]}"'},
    )


@app.get('/api/session')
async def get_session():
    if not session['original_text']:
        return {'loaded': False}
    return {
        'loaded': True,
        'filename': session['filename'],
        'matches': session['matches'],
    }


def preload(step_path: Path, matches_path: Path) -> None:
    text = step_path.read_text(encoding='utf-8', errors='replace')
    session.update({
        'filename': step_path.name,
        'original_text': text,
        'matches': _load_matches(matches_path),
    })
    count = len((session['matches'] or {}).get('matches', []))
    print(f'Pre-loaded: {step_path.name} ({count} matched instances)', file=sys.stderr)


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='STEP match viewer')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--step', metavar='FILE', required=True, help='Assembly STEP file to render')
    parser.add_argument('--matches', metavar='JSON', required=True, help='Match JSON from match_step_component_positions.py')
    args = parser.parse_args()

    preload(Path(args.step), Path(args.matches))

    print(f'Starting viewer at http://{args.host}:{args.port}', file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')
