#!/usr/bin/env python3
"""Entry point for the FireMon retrieval retry/alert tool.

    python3 run_retry_alert.py --config config.ini --dry-run

Kept as a thin wrapper so the tool can be run straight out of its install
directory without needing to be on PYTHONPATH or installed as a package.

On FMOS the default `python3` may be too old for dataclasses, but newer
interpreters are installed alongside. The bootstrap below re-executes this
script under the newest suitable one, so the command above works whatever
`python3` happens to point at.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must come before any other package import: everything else needs 3.7+.
from retry_alert._bootstrap import ensure_interpreter  # noqa: E402

ensure_interpreter(__file__)

from retry_alert.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
