"""The only seam that knows hyera's Python API and CLI.

Every conformance test reaches hyera through this module.
"""

import contextlib
import io
import json
import logging
import os

from hyera import Hiera, HieraError, KeyNotFoundError, Scope, Sensitive, load_facts
from hyera.cli import main as _cli_main

import _golden
from _golden import SENSITIVE_JSON

#: None: the CLI accepts puppet lookup's flags; per-query markers apply to
#: both channels.
CLI_CHANNEL_DIVERGENCE = None

#: puppet_args flags this adapter understands well enough to translate.
_KNOWN_PUPPET_FLAGS = ("--strict", "--environment", "--modulepath")
_MODULEPATH_FLAG = "--modulepath"


class AdapterUnsupported(Exception):
    """A query needs a feature neither channel exercises yet."""


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


@contextlib.contextmanager
def _chdir(path):
    """``os.chdir`` for the duration of the block, restored in ``finally``
    (``contextlib.chdir`` is 3.11+; this project's floor is 3.9). A relative
    ``pkcs7_private_key`` resolves against the process cwd, exactly like
    hiera-eyaml itself -- ``record.py`` runs Puppet with ``wsl.exe --cd
    <case dir>``, so replaying a query here needs the same cwd."""
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


def _load_facts(case_dir) -> dict:
    """Puppet's own ``--facts`` path: a ``BackendError`` here (a malformed
    facts file, a disallowed YAML class, the hostname/domain/fqdn/clientcert
    all-or-none rule) is an ``error`` outcome like any other
    :class:`~hyera.HieraError`, so this is called from inside ``run_api``'s
    try/except, not before it."""
    return load_facts(case_dir / "facts.yaml")


def as_puppet_json(value):
    """Project a Python value onto what Puppet's ``--render-as json`` emits."""

    def default(obj):
        if isinstance(obj, Sensitive):
            return SENSITIVE_JSON
        raise TypeError("not representable in Puppet JSON: {!r}".format(obj))

    return json.loads(json.dumps(value, default=default, allow_nan=False))


def is_ordered(query: dict) -> bool:
    """Key order is part of a value unless the query sets ``ordered: false``."""
    return query.get("ordered", True)


def canonical(value, ordered=True) -> str:
    return json.dumps(
        value, sort_keys=not ordered, ensure_ascii=False, separators=(",", ":")
    )


def comparable_warning(message: str, case_dir) -> str:
    """A warning as both sides compare it: paths normalized, trailing ``;`` and
    ``.`` dropped (Puppet ends some warnings with ``;`` and the node-parameter
    collision warning with no full stop)."""
    text = _golden.normalize_message(message, case_dir).replace("\\", "/")
    return text.rstrip(";. ")


class _WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _captured_warnings():
    handler = _WarningCapture()
    logger = logging.getLogger("hyera")
    previous = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous[0])
        logger.propagate = previous[1]


def missing_warnings(golden_result: dict, actual: dict, case_dir) -> list:
    """Warnings Puppet recorded that the run did not log. Warnings only hyera
    logs are not asserted."""
    got = {comparable_warning(m, case_dir) for m in actual.get("warnings", [])}
    return [
        w
        for w in golden_result.get("warnings", [])
        if comparable_warning(w, case_dir) not in got
    ]


def expected(query: dict, golden_result: dict) -> dict:
    """What a query must match: a `deviation`'s `expect`, else the golden."""
    deviation = query.get("deviation")
    if deviation:
        return deviation["expect"]
    return golden_result


def _check_common(case_dir, case, query):
    key = query["key"]
    args = _puppet_args(case, query)
    bad = _unrecognized_flags(args)
    if bad:
        raise AdapterUnsupported("unrecognized puppet flag(s): {}".format(bad))
    strict = _flag_value(args, "--strict") or "warning"
    env = _flag_value(args, "--environment") or "production"
    return key, env, strict


def _layer_kwargs(case_dir, args):
    """The three layer keywords for a query's argv, mirroring the recorder's
    own ``--environmentpath ./environments --basemodulepath ./modules`` plus
    a golden's own ``--modulepath`` override (Puppet-on-Linux syntax: a
    single value split on ``:``)."""
    kwargs = {
        "environmentpath": [case_dir / "environments"],
        "basemodulepath": [case_dir / "modules"],
    }
    modulepath = _flag_value(args, _MODULEPATH_FLAG)
    if modulepath is not None:
        kwargs["modulepath"] = [case_dir / p for p in modulepath.split(":")]
    return kwargs


def _build(case_dir, case: dict, query: dict, golden: dict):
    """Build the :class:`hyera.Hiera` instance a query is resolved through,
    exactly as ``run_api``/``run_explain`` both need it: scope from the
    case's facts/environment/strict, layered per the case's own
    ``environments``/``modules`` (and any golden ``--modulepath``
    override)."""
    key, env, strict = _check_common(case_dir, case, query)
    args = _puppet_args(case, query)
    facts = _load_facts(case_dir)
    scope = Scope(
        facts=facts,
        environment=env,
        server_facts={"serverversion": golden["puppet_version"]},
        strict=strict,
        node_name=golden["node"],
    )
    hiera = Hiera(
        str(case_dir / "hiera.yaml"),
        scope=scope,
        # Mirrors the recorder's empty isolation `--codedir` (`_iso_args`): a version 3
        # config's default per-backend datadir must not depend on this box's Puppet
        # codedir, so it names a directory that never exists.
        codedir=str(case_dir / "_no_codedir"),
        **_layer_kwargs(case_dir, args),
    )
    return hiera, key


def _lookup_kwargs(query: dict) -> dict:
    kwargs = {"value_type": query.get("type"), "merge": query.get("merge")}
    if query.get("default") is not None:
        kwargs["default_value"] = query["default"]
    return kwargs


def run_api(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """:func:`_run_api` plus the warnings hyera logged while resolving."""
    with _captured_warnings() as messages:
        result = _run_api(case_dir, case, query, golden)
    result["warnings"] = list(messages)
    return result


def _run_api(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one query through :class:`hyera.Hiera`, projected like Puppet.

    ``Hiera(...)`` is inside the same try/except as the lookup, so a construction-time
    ``ConfigError`` reports as ``{"status": "error", ...}`` like a lookup-time one.

    ``as_puppet_json`` has its own try/except: several divergences are exactly "hyera
    raises a raw exception where Puppet also errors", and catching every
    ``ValueError`` from ``hiera.lookup()`` would turn them into accidental passes.
    Only ``as_puppet_json``'s ``ValueError`` (NaN/Infinity, which Puppet's
    ``--render-as json`` also rejects) is a harness concern.
    """
    try:
        with _chdir(case_dir):
            hiera, key = _build(case_dir, case, query, golden)
            value = hiera.lookup(key, **_lookup_kwargs(query))
    except KeyNotFoundError as e:
        # The recorder's "not_found" heuristic (_NOT_FOUND in record.py) matches only
        # Puppet's singular miss message; a multi-name miss is filed as a generic
        # "error", so mirror that split.
        if isinstance(e.name, (list, tuple)) and len(e.name) != 1:
            return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
        return {"status": "not_found"}
    except HieraError as e:
        return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
    try:
        rendered = as_puppet_json(value)
    except ValueError as e:
        return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
    return {"status": "found", "value": rendered}


def run_explain(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one ``explain:`` query through :class:`hyera.Hiera.explain`,
    projected like ``puppet lookup --explain``/``--explain-options``.

    Mirrors ``run_api``: ``Hiera(...)``/``.explain(...)`` share one
    try/except, so a construction-time ``ConfigError`` reports the same as
    a lookup-time one.
    """
    try:
        with _chdir(case_dir):
            hiera, key = _build(case_dir, case, query, golden)
            kwargs = _lookup_kwargs(query)
            kwargs["explain_options"] = query["explain"] == "options"
            result = hiera.explain(key, **kwargs)
    except HieraError as e:
        return {"status": "error", "message": str(e), "exc_class": type(e).__name__}
    tree = _golden.normalize_tree_paths(as_puppet_json(result.to_hash()), case_dir)
    text = [_golden.normalize_paths(l, case_dir) for l in result.text().splitlines()]
    return {"status": "explained", "tree": tree, "text": text}


def run_cli(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one query through ``hyera.cli.main``, invoked in-process.

    Passes the golden's own recorded ``puppet lookup`` argv straight to our
    CLI.
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
    argv += ["--codedir", str(case_dir / "_no_codedir")]
    argv += ["--render-as", "json"] + list(result["argv"])

    out = io.StringIO()
    try:
        with _chdir(case_dir), contextlib.redirect_stdout(out):
            rc = _cli_main(argv)
    except SystemExit as e:
        return {"status": "usage", "code": e.code}

    text = out.getvalue()
    if rc == 0:
        return {"status": "found", "value": json.loads(text) if text.strip() else None}
    if rc == 1:
        # Mirrors run_api's name-list case: a multi-name miss was recorded as a generic
        # "error", though both channels agree it misses every name (rc 1 here,
        # LookupError there).
        key = query.get("key")
        if isinstance(key, (list, tuple)) and len(key) != 1:
            return {"status": "error"}
        return {"status": "not_found"}
    if rc == 2:
        return {"status": "error"}
    raise AssertionError("unexpected hyera CLI exit code {!r}".format(rc))


def run_cli_explain(case_dir, case: dict, query: dict, golden: dict) -> dict:
    """Resolve one ``explain:`` query through ``hyera.cli.main``, in-process,
    twice: once for the tree (``--render-as json``) and once for the text
    (``--render-as s``), the same way ``record.py`` calls real Puppet twice.
    """
    result = golden["results"][_golden.query_id(query)]
    base_argv = [
        "--hiera_config",
        str(case_dir / "hiera.yaml"),
        "--facts",
        str(case_dir / "facts.yaml"),
        "--node",
        _golden.NODE,
    ]
    if (case_dir / "environments").is_dir():
        base_argv += ["--environmentpath", str(case_dir / "environments")]
    if (case_dir / "modules").is_dir():
        base_argv += ["--basemodulepath", str(case_dir / "modules")]

    def _call(render_as):
        argv = base_argv + ["--render-as", render_as] + list(result["argv"])
        out = io.StringIO()
        with _chdir(case_dir), contextlib.redirect_stdout(out):
            rc = _cli_main(argv)
        return rc, out.getvalue()

    try:
        rc_json, out_json = _call("json")
        rc_s, out_s = _call("s")
    except SystemExit as e:
        return {"status": "usage", "code": e.code}

    if rc_json == 0 and rc_s == 0:
        tree = _golden.normalize_tree_paths(
            json.loads(out_json) if out_json.strip() else {}, case_dir
        )
        text = [_golden.normalize_paths(l, case_dir) for l in out_s.splitlines()]
        return {"status": "explained", "tree": tree, "text": text}
    if rc_json == 2 or rc_s == 2:
        return {"status": "error"}
    raise AssertionError(
        "unexpected hyera CLI exit codes {!r}/{!r}".format(rc_json, rc_s)
    )
