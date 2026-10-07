"""The scenario builders, one module per area.

Importing this package registers every builder in
:data:`differential.scenario.REGISTRY`; the module's name is the area's name.
"""

from __future__ import annotations

from . import (  # noqa: F401
    backends,
    config,
    extra,
    interp,
    interp_sweep,
    keys,
    layers,
    locations,
    lopts,
    strategies,
    yaml_data,
)

#: Every scenario area, in the order a full run reports them.
AREAS = (
    "backends",
    "config",
    "extra",
    "interp",
    "interp_sweep",
    "keys",
    "layers",
    "locations",
    "lopts",
    "strategies",
    "yaml_data",
)
