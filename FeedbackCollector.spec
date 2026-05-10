# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import importlib.util

block_cipher = None

project_root = Path.cwd()
src_dir = project_root / 'src'

datas = [
    (str(src_dir / 'templates'), 'src/templates'),
    (str(src_dir / 'static'), 'src/static'),
    (str(src_dir / 'categories.json'), 'src'),
    (str(src_dir / 'impact_types.json'), 'src'),
    (str(src_dir / 'keywords.json'), 'src'),
]

for env_candidate in [project_root / '.env', src_dir / '.env']:
    if env_candidate.exists():
        datas.append((str(env_candidate), '.'))
        break


def available_hiddenimports(*modules):
    return [module for module in modules if importlib.util.find_spec(module) is not None]

a = Analysis(
    ['start_feedback_collector.py'],
    pathex=[str(src_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=available_hiddenimports(
        'praw',
        'requests',
        'pandas',
        'pyodbc',
        'flask',
        'jinja2',
        'dotenv',
    ) + [
        # Modules under src/ that are loaded indirectly by the
        # Flask routes (so PyInstaller's static analyser doesn't
        # always pick them up). pathex above lets it resolve them.
        'run_web',
        'app',
        'config',
        'collectors',
        'utils',
        'state_manager',
        'id_generator',
        'ado_client',
        'local_store',
        'runtime_paths',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FeedbackCollector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Show console for logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FeedbackCollector',
)
