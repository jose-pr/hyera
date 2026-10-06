"""``python -m hyera``: the same entry point as the ``hyera`` console
script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
