"""Turn each side's raw answer into an outcome, and compare outcomes.

An outcome is a plain dict that a corpus can store::

    {"status": "found", "value": ...}            --render-as json
    {"status": "found", "text": "..."}           --render-as yaml or s
    {"status": "not_found"}
    {"status": "error", "message": "..."}

``json`` outcomes also carry ``"text"`` when the output is not the compact
rendering Python gives the value (a float spelled differently, for instance).
Paths are normalised to ``<case>`` and ``<iso>`` so the same outcome is
recorded from any machine.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

_NOT_FOUND = re.compile(r"did not find a value for (the name|any of the names)")
_KEY_PREFIX = re.compile(r"Lookup of key '.*?' failed: ")
_ISO = re.compile(r"/tmp/hyera-differential-\d+/\d+")
_CASE_TAIL = re.compile(r"<case>[^\s'\")]*")
_LOG_PREFIX = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \|\s*\w+\s*\| [\w.]+: ", re.M
)
_RUBY_TOP = re.compile(r"^\S+:\d+:in '[^']*': (.*) \(([\w:]+)\)$")
#: Longest error message kept in an outcome.
MESSAGE_LIMIT = 300


def normalize(text: str, *dirs: str) -> str:
    """Replace each scenario directory (in any slash form) with ``<case>``,
    the isolation root with ``<iso>``, and use ``/`` inside ``<case>`` paths."""
    if not text:
        return text
    for directory in dirs:
        if not directory:
            continue
        for form in {directory, directory.replace("\\", "/")}:
            text = text.replace(form, "<case>")
    text = _ISO.sub("<iso>", text)
    return _CASE_TAIL.sub(lambda m: m.group(0).replace("\\", "/"), text)


def scrub(value, *dirs: str):
    """``value`` with every string (and key) passed through :func:`normalize`."""
    if isinstance(value, str):
        return normalize(value, *dirs)
    if isinstance(value, list):
        return [scrub(v, *dirs) for v in value]
    if isinstance(value, dict):
        return {scrub(k, *dirs): scrub(v, *dirs) for k, v in value.items()}
    return value


def render_json(value) -> str:
    """The compact JSON text Puppet's renderer prints for a value."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def aio(text: str) -> str:
    """Ruby 3.4's ``Hash#inspect`` spacing (``"k" => v``) in the Ruby 3.2 form
    (``"k"=>v``) the packages ship; single-quoted keys are Puppet's own format."""
    return re.sub(r"(?<!') => ", "=>", text)


def canon(value, ordered: bool = False) -> str:
    """Type-sensitive text of a JSON value (``1`` is not ``1.0``)."""
    return aio(
        json.dumps(
            value, sort_keys=not ordered, ensure_ascii=False, separators=(",", ":")
        )
    )


def strip_key_prefix(message: str) -> str:
    return _KEY_PREFIX.sub("", message or "")


def _parse_json_output(text: str):
    """``(value, text)`` of a ``--render-as json`` output.

    Puppet writes its debug log and a merge's debug trace to standard output
    around the value; when the whole text is not JSON the last line that is JSON is
    taken.
    """
    try:
        return json.loads(text), text
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line), line
        except ValueError:
            continue
    return {"__unparsed__": text}, text


def puppet_outcome(raw: dict, case_dir: str, render: Optional[str]) -> dict:
    """Puppet's answer for one job from the driver's raw record."""
    out: dict
    stderr = normalize(raw.get("stderr", ""), case_dir)
    if raw["rc"] == 0:
        text = normalize(raw["stdout"], case_dir)
        if render in ("yaml", "s"):
            return {"status": "found", "text": text}
        value, text = _parse_json_output(text)
        out = {"status": "found", "value": value}
        if text.strip() != render_json(value):
            out["text"] = text
        return out
    lookup_error = raw.get("lookup_error")
    if lookup_error:
        message = normalize(lookup_error["message"], case_dir)
        status = "not_found" if _NOT_FOUND.search(message) else "error"
    else:
        lines = stderr.splitlines()
        first = next((i for i, l in enumerate(lines) if l.startswith("Error:")), None)
        status = "error"
        if first is not None:
            message = re.sub(
                r"^Error: (Could not run: )?", "", "\n".join(lines[first:])
            )
        elif lines and _RUBY_TOP.match(lines[0]):
            message = _RUBY_TOP.match(lines[0]).group(1)  # type: ignore[union-attr]
        else:
            message = "<no Error line> " + stderr.strip()[-200:]
        if raw["rc"] == -9:
            message = "timeout"
    if status == "not_found":
        return {"status": "not_found"}
    return {"status": "error", "message": message.strip()[:MESSAGE_LIMIT]}


def _cli_status(rc: int) -> str:
    return {0: "found", 1: "not_found", 2: "error"}.get(rc, "rc%d" % rc)


def cli_outcome(raw: dict, case_dir: str, render: Optional[str]) -> dict:
    """hyera's CLI answer: the same shape as :func:`puppet_outcome`."""
    rc = raw["rc"]
    if rc == -2:
        return {"status": "error", "message": "usage", "usage": True}
    status = _cli_status(rc)
    if status == "found":
        text = normalize(raw["stdout"], case_dir)
        if render in ("yaml", "s"):
            return {"status": "found", "text": text}
        value, text = _parse_json_output(text)
        out = {"status": "found", "value": value}
        if text.strip() != render_json(value):
            out["text"] = text
        return out
    if status == "not_found":
        return {"status": "not_found"}
    message = normalize("\n".join(raw.get("errors", [])), case_dir)
    return {"status": status, "message": message.strip()[:MESSAGE_LIMIT]}


def api_outcome(raw: Optional[dict], case_dir: str) -> Optional[dict]:
    """hyera's API answer, or ``None`` when the query is CLI-only."""
    if raw is None or raw["status"] == "skipped":
        return None
    out = {"status": raw["status"]}
    if raw["status"] == "found":
        out["value"] = scrub(raw["value"], case_dir)
    elif raw["status"] != "not_found":
        out["message"] = normalize(raw.get("message", ""), case_dir)[:MESSAGE_LIMIT]
        if raw["status"] == "crash":
            out["exc"] = raw.get("exc", "")
    return out


def hyera_outcomes(raw: dict, case_dir: str, render: Optional[str]) -> dict:
    """``{"api": outcome-or-None, "cli": outcome}`` for one job."""
    return {
        "api": api_outcome(raw.get("api"), case_dir),
        "cli": cli_outcome(raw["cli"], case_dir, render),
    }


def message_of(outcome: Optional[dict]) -> str:
    return (outcome or {}).get("message", "") or ""


Verdict = Tuple[str, Optional[str]]


def disagreement(job: dict, puppet: dict, hyera: dict) -> Optional[str]:
    """The kind of the first disagreement between Puppet and hyera, or ``None``.

    Kinds: ``CRASH`` (hyera raised something that is not a ``HieraError``),
    ``CHANNEL`` (hyera's API and CLI disagree), ``STATUS`` (found, not found and
    error differ), ``VALUE`` (both found, different values), ``RENDER`` (same
    value, different text or key order).
    """
    api, cli = hyera["api"], hyera["cli"]
    if api is not None and api["status"] == "crash":
        return "CRASH"
    if api is not None and api["status"] != cli["status"]:
        return "CHANNEL"
    ours = api if api is not None else cli
    if puppet["status"] != ours["status"] or puppet["status"] != cli["status"]:
        return "STATUS"
    if puppet["status"] != "found":
        return None
    if job.get("render") in ("yaml", "s"):
        if aio(puppet["text"]) != aio(cli["text"]):
            return "RENDER"
        return None
    if canon(puppet["value"]) != canon(ours["value"]):
        return "VALUE"
    if canon(puppet["value"], True) != canon(ours["value"], True):
        return "RENDER"
    if canon(puppet["value"]) != canon(cli["value"]):
        return "CHANNEL"
    if aio(json_text(puppet)) != aio(json_text(cli)):
        return "RENDER"
    return None


def json_text(outcome: dict) -> str:
    """The ``--render-as json`` text of a found outcome."""
    return (outcome.get("text") or render_json(outcome["value"])).strip()


def message_differs(puppet: dict, hyera: dict) -> bool:
    """For two errors: whether the message texts differ once Puppet's per-key
    wrapper is dropped. Never a disagreement by itself; the answer is the
    status."""
    ours = [strip_key_prefix(message_of(o)) for o in (hyera["api"], hyera["cli"]) if o]
    theirs = strip_key_prefix(message_of(puppet))
    return all(aio(theirs) != aio(m) for m in ours)


def summarize(job: dict, puppet: dict, hyera: dict) -> Dict[str, object]:
    """The facts a report prints for one disagreement."""
    return {
        "id": job["id"],
        "argv": job["argv"],
        "puppet": puppet,
        "api": hyera["api"],
        "cli": hyera["cli"],
    }
