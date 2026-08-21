#!/usr/bin/env python
"""Entry point: python orchestrator.py <doctor|run|status|api|import-inbox|projects>"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
