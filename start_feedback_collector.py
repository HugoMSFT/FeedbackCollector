import os
import sys

# Force UTF-8 on stdout/stderr so emoji-laden log lines survive when this
# script is launched with redirected output (e.g. PowerShell's
# ``Start-Process -RedirectStandardOutput``) or run as a frozen exe on a
# Windows console using cp1252. ``errors='replace'`` keeps the program
# alive if some terminal still can't render a character.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, 'src')

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from run_web import main


if __name__ == '__main__':
    main()