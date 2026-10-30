"""Import local Amap keys into ignored env files without printing secrets."""

from __future__ import annotations

import argparse
from pathlib import Path


def upsert(path: Path, name: str, value: str) -> None:
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    replacement = f'{name}={value}'
    output = []
    replaced = False
    for line in lines:
        if line.lstrip().startswith(f'{name}='):
            if not replaced:
                output.append(replacement)
                replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(replacement)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(output) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    args = parser.parse_args()

    labelled = []
    for raw_line in args.source.read_text(encoding='utf-8').splitlines():
        if '=' not in raw_line:
            continue
        label, value = (part.strip().strip('"\'') for part in raw_line.split('=', 1))
        if value:
            labelled.append((label, value))

    aliases = {'CAREGUIDE_AMAP_WEB_KEY', 'CAREGUIDE_AMAP_WEB_SERVICE_KEY'}
    # Prefer an explicitly human-labelled backend key. Older local notes used
    # CAREGUIDE_AMAP_WEB_KEY for the browser key despite the ambiguous name.
    backend = next((value for label, value in labelled if '后端' in label), '')
    backend = backend or next((value for label, value in labelled if label in aliases), '')
    frontend = next((value for label, value in labelled
                     if label == 'VITE_AMAP_JS_KEY' and value != backend), '')
    frontend = frontend or next((value for _label, value in labelled
                                 if value != backend and len(value) == 32), '')
    if not backend or not frontend:
        raise SystemExit('Expected distinct backend and frontend Amap keys were not found')

    upsert(args.project / '.env', 'CAREGUIDE_AMAP_WEB_SERVICE_KEY', backend)
    upsert(args.project / 'frontend' / '.env.local', 'VITE_AMAP_JS_KEY', frontend)
    print('Imported Amap backend and frontend keys into ignored local env files.')


if __name__ == '__main__':
    main()
