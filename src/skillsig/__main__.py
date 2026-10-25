"""
STAGE: package
MODULE: __main__
PURPOSE: Entry point for `python -m skillsig`.
INPUTS: command-line arguments
OUTPUTS: none (delegates to skillsig.cli)
RQ LINK: none (infrastructure)
"""

from __future__ import annotations

import sys

from skillsig.cli import main

if __name__ == "__main__":
    sys.exit(main())
