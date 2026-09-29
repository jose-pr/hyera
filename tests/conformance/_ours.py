"""The only seam that knows hyera's Python API and CLI.

Every conformance test reaches hyera through this module. A plan that
changes the lookup API (``.get`` -> ``.lookup``), scope building, the
strict option, an exception class, or the CLI edits this file in the same
commit, removing the ``AdapterUnsupported`` branches it makes expressible.
"""

import contextlib
import io
import json

import yaml

from hyera import Hiera, HieraError, KeyNotFoundError, Sensitive
from hyera.cli import main as _cli_main

import _golden
from _golden import SENSITIVE_JSON

#: Strict xfail reason for every CLI-channel query until hyera's CLI
#: accepts puppet lookup's own flags (cli_puppet_lookup_parity fidelity
#: plan). Set to None there, in the same commit that removes this marker.
CLI_CHANNEL_DIVERGENCE = "spec-layers-backends/cli-flag-parity"

#: puppet_args flags this adapter understands well enough to translate.
_KNOWN_PUPPET_FLAGS = ("--strict", "--environment")


class AdapterUnsupported(Exception):
    """A query needs a feature neither channel exercises yet."""


def puppet_scope(facts: dict, env: str, puppet_version: str) -> dict:
    """The scope ``puppet lookup --node N --facts facts.yaml`` builds.

    Re-measured 2026-09-29 against the real oracle: every fact is also a
    top-scope variable, ``trusted`` carries lookup-mode's empty identity,
    and ``server_facts`` carries only the portable version/environment
    fields (never the recording host's name/IPs).
    """
    return {
        **facts,
        "facts": facts,
        "environment": env,
        "trusted": {
            "authenticated": "local",
            "certname": None,
            "extensions": {},
            "hostname": None,
            "domain": None,
            "external": {},
        },
        "server_facts": {"serverversion": puppet_version, "environment": env},
    }


def _puppet_args(case: dict, query: dict) -> list:
    return list(case.get("puppet_args") or []) + list(query.get("puppet_args") or [])


def _flag_value(args: list, name: str):
    value = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == name and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if a.startswith(name + "="):
            value = a.split("=", 1)[1]
            i += 1
            continue
        i += 1
    return value


def _unrecognized_flags(args: list) -> list:
    bad = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name not in _KNOWN_PUPPET_FLAGS:
                bad.append(name)
            if "=" not in a and i + 1 < len(args) and not args[i + 1].startswith("--"):
                i += 2
                continue
        i += 1
    return bad


def _load_facts(case_dir) -> dict:
    return yaml.safe_load((case_dir / "facts.yaml").read_text(encoding="utf-8")) or {}


def as_puppet_json(value):
    """Project a Python value onto what Puppet's ``--render-as json`` emits."""

    def default(obj):
        if isinstance(obj, Sensitive):
            return SENSITIVE_JSON
        raise TypeError("not representable in Puppet JSON: {!r}".format(obj))

    return json.loads(json.dumps(value, default=default, allow_nan=False))


def canonical(value, ordered=False) -> str:
    return json.dumps(
        value, sort_keys=not ordered, ensure_ascii=False, separators=(",", ":")
    )


def expected(query: dict, golden_result: dict) -> dict:
    """What a query must match: a `deviation`'s `expect`, else the golden."""
    deviation = query.get("deviation")
    if deviation:
        return deviation["expect"]
    return golden_result


def _check_common(case_dir, case, query):
    key = query["key"]
    if isinstance(key, list) or query.get("type"):
        raise AdapterUnsupported(
            "spec-lookup-options-types/lookup-api-missing-type-names-defaults"
        )
    args = _puppet_args(case, query)
    bad = _unrecognized_flags(args)
    if bad:
        raise AdapterUnsupported("unrecognized puppet flag(s): {}".format(bad))
    strict = _flag_value(args, "--strict") or "warning"
    if strict == "error":
        raise AdapterUnsupported("spec-context-facts/undefined-variable-semantics")
    if (case_dir / "environments").is_dir() or (case_dir / "modules").is_dir():
        raise AdapterUnsupported("spec-layers-backends/no-config-layers")
    env = _flag_value(args, "--environment") or "production"
    return key, env


def run_api(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one query through :class:`hyera.Hiera`, projected like Puppet.

    A config-schema divergence (most of the ``config`` area) raises during
    construction, not during ``.get()`` -- ``Hiera(...)`` is inside the same
    try/except as the lookup call so a ``ConfigError`` there is reported as
    ``{"status": "error", ...}`` exactly like one raised during the lookup,
    instead of escaping as a raw pytest error on a case that otherwise
    matches Puppet (both sides error).

    ``as_puppet_json`` is deliberately in its OWN try/except, not folded into
    the one above: several divergences (``code-io-security/dotted-subkey-raw-
    exceptions``, ``spec-lookup-options-types/merge-errors-escape-as-
    valueerror``, ``spec-merge/bad-merge-strategy-uncaught-valueerror``) are
    *exactly* "hyera raises a raw, unwrapped exception (often ValueError)
    where Puppet also errors" -- catching every ``ValueError`` from
    ``hiera.get()`` itself would silently launder that divergence into a
    clean status match (found as an XPASS(strict) regression the first time
    this was tried: it turned three existing raw-exception divergences into
    accidental passes). Only ``as_puppet_json``'s own ``ValueError`` (a
    NaN/Infinity value, which fails Puppet's own ``--render-as json`` the
    same way ``allow_nan=False`` does here) is a harness-projection concern,
    not a hyera-behavior one, so only that call is guarded.
    """
    key, env = _check_common(case_dir, case, query)
    facts = _load_facts(case_dir)
    scope = puppet_scope(facts, env, golden["puppet_version"])
    merge = query.get("merge")
    try:
        hiera = Hiera(str(case_dir / "hiera.yaml"), context=scope)
        if query.get("default") is not None:
            value = hiera.get(key, default=query["default"], merge=merge)
        else:
            value = hiera.get(key, merge=merge, throw=True)
    except KeyNotFoundError:
        return {"status": "not_found"}
    except HieraError as e:
        return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
    try:
        rendered = as_puppet_json(value)
    except ValueError as e:
        return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
    return {"status": "found", "value": rendered}


def run_cli(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one query through ``hyera.cli.main``, invoked in-process.

    Passes the golden's own recorded ``puppet lookup`` argv straight to our
    CLI. Today that argv (``--hiera_config``/``--facts``/``--node``/
    ``--render-as``) is refused by argparse before ``Lookup.__call__`` runs,
    so this always ends in the ``usage`` branch -- which is why every
    CLI-channel query carries `CLI_CHANNEL_DIVERGENCE` until a later plan
    teaches the CLI Puppet's own flags.
    """
    result = golden["results"][_golden.query_id(query)]
    argv = [
        "--hiera_config",
        str(case_dir / "hiera.yaml"),
        "--facts",
        str(case_dir / "facts.yaml"),
        "--node",
        _golden.NODE,
    ]
    if (case_dir / "environments").is_dir():
        argv += ["--environmentpath", str(case_dir / "environments")]
    if (case_dir / "modules").is_dir():
        argv += ["--basemodulepath", str(case_dir / "modules")]
    argv += ["--render-as", "json"] + list(result["argv"])

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = _cli_main(argv)
    except SystemExit as e:
        return {"status": "usage", "code": e.code}

    text = out.getvalue()
    if rc == 0:
        return {"status": "found", "value": json.loads(text) if text.strip() else None}
    if rc == 1:
        return {"status": "not_found"}
    if rc == 2:
        return {"status": "error"}
    raise AssertionError("unexpected hyera CLI exit code {!r}".format(rc))
