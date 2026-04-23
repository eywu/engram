#!/usr/bin/env python3
"""CLI wrapper for the packaged nightly harvest job."""
from __future__ import annotations

import sys

from engram.nightly.harvest import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
