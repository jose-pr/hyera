"""``python -m hyera.cli``: the same entry point as ``python -m hyera``."""

from __future__ import annotations

import sys

from . import main

sys.exit(main())
