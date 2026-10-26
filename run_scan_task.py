"""Scheduled-task runner: runs `scanner.py scan` without a console (use with pythonw.exe).

Console-based launches get killed under this machine's Task Scheduler, so this script
captures the scanner's output itself. Writes reports/scan_latest.txt and appends to
reports/scan_log.txt. Exit code 0 = fine, 2 = data check failed, 1 = crash.
"""
import contextlib
import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parent
os.chdir(root)
sys.path.insert(0, str(root))
reports = root / "reports"
reports.mkdir(exist_ok=True)
latest = reports / "scan_latest.txt"
sys.argv = ["scanner.py", "scan"]
code = 0

with open(latest, "w", encoding="utf-8") as out:
    out.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    out.flush()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            runpy.run_path(str(root / "scanner.py"), run_name="__main__")
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception:
            traceback.print_exc()
            code = 1
    out.write(f"\nexit code {code}\n")

with open(reports / "scan_log.txt", "a", encoding="utf-8") as log:
    log.write(latest.read_text(encoding="utf-8") + "\n")
sys.exit(code)
