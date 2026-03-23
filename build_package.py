import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / 'dist' / 'FeedbackCollector'


def main():
    command = [
        sys.executable,
        '-m',
        'PyInstaller',
        'FeedbackCollector.spec',
        '--noconfirm',
        '--clean',
    ]

    subprocess.run(command, cwd=PROJECT_ROOT, check=True)

    for env_candidate in [PROJECT_ROOT / '.env', PROJECT_ROOT / 'src' / '.env']:
        if env_candidate.exists() and DIST_DIR.exists():
            shutil.copy2(env_candidate, DIST_DIR / '.env')
            break

    print(f'Build complete: {DIST_DIR}')


if __name__ == '__main__':
    main()