"""A minimal, runnable ``hyera`` lookup: load facts, build a ``Hiera``
instance, and print a handful of resolved values.

Run it from anywhere: ``python examples/lookup.py``. See ``README.md`` in
this directory for the equivalent command-line invocation.
"""

import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent / "src"))

from hyera import Hiera, Scope, load_facts  # noqa: E402

hiera = Hiera(
    str(here / "hiera.yaml"), scope=Scope(facts=load_facts(here / "facts.yaml"))
)

for key in ("ntp::servers", "nginx::workers", "packages::manager", "motd", "users"):
    value = hiera.lookup(key)
    print("{} = {}".format(key, json.dumps(value)))
