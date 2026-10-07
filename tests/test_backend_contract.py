"""Every built-in backend passes ``hyera.testing.BackendContract``, and the suite
fails a deliberately broken backend on exactly the check that backend breaks."""

from __future__ import annotations

import copy
import json
import sys

import pytest

from hyera import BackendError
from hyera.backends import (
    Backend,
    DotenvBackend,
    EyamlBackend,
    HOCONBackend,
    JSONBackend,
    SopsBackend,
    YAMLBackend,
)
from hyera.testing import BackendContract


def _write_json(name):
    def write(self, directory, data):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(json.dumps(data).encode("utf-8"))
        return {"path": name}

    return write


class TestYAMLBackend(BackendContract):
    backend = YAMLBackend
    write_source = _write_json("a.yaml")


class TestJSONBackend(BackendContract):
    backend = JSONBackend
    write_source = _write_json("a.json")


class TestHOCONBackend(BackendContract):
    backend = HOCONBackend
    write_source = _write_json("a.conf")

    @pytest.fixture(autouse=True)
    def _pyhocon(self):
        pytest.importorskip("pyhocon")


class TestEyamlBackend(BackendContract):
    backend = EyamlBackend
    write_source = _write_json("a.eyaml")

    @pytest.fixture(autouse=True)
    def _cryptography(self):
        pytest.importorskip("cryptography")


class TestSopsBackend(BackendContract):
    backend = SopsBackend
    write_source = _write_json("a.yaml")

    @pytest.fixture(autouse=True)
    def _fake_sops(self, fake_program):
        # Decrypting is the identity: the plaintext is the file itself.
        fake_program(
            "sops",
            "import sys\n"
            "try:\n"
            "    data = open(sys.argv[-1], 'rb').read()\n"
            "except OSError:\n"
            "    sys.stderr.write('no such file')\n"
            "    sys.exit(1)\n"
            "sys.stdout.buffer.write(data)\n",
        )


class TestDotenvBackend(BackendContract):
    backend = DotenvBackend
    data = {"CONTRACT_TEXT": "value", "CONTRACT_OTHER": "two words"}

    def write_source(self, directory, data):
        directory.mkdir(parents=True, exist_ok=True)
        text = "".join("{}={}\n".format(k, v) for k, v in data.items())
        (directory / "a.env").write_bytes(text.encode("utf-8"))
        return {"path": "a.env"}


# --- the suite fails a backend that breaks one rule ----------------------

_CHECKS = sorted(name for name in dir(BackendContract) if name.startswith("test_"))


@pytest.fixture
def registry(monkeypatch):
    """A registry copy, so a broken backend's names never reach another test."""
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))


def _failures(contract, tmp_path):
    """The checks of ``contract`` that fail, by name."""
    failed = set()
    suite = contract()
    for name in _CHECKS:
        method = getattr(suite, name)
        try:
            if "tmp_path" in method.__code__.co_varnames[: method.__code__.co_argcount]:
                method(tmp_path / name)
            else:
                method()
        except Exception:
            failed.add(name)
    return failed


def _contract(backend_cls, write=None):
    namespace = {"backend": backend_cls}
    namespace["write_source"] = write or _write_json("a.yaml")
    return type("Broken", (BackendContract,), namespace)


class _Good(Backend):
    """A correct ``data_hash`` backend reading JSON text."""

    def loads(self, text):
        try:
            return json.loads(text)
        except ValueError as e:
            raise BackendError("bad json") from None


def test_a_correct_third_party_backend_passes_every_check(registry, tmp_path):
    class Good(_Good):
        NAMES = {"function": ("contract_good_data",)}

    assert _failures(_contract(Good), tmp_path) == set()


def test_a_name_that_is_not_lowercase_fails_only_the_names_check(registry, tmp_path):
    class Broken(_Good):
        NAMES = {"function": ("Contract_Upper_Data",)}

    assert _failures(_contract(Broken), tmp_path) == {
        "test_every_name_resolves_to_the_class"
    }


def test_implements_that_lies_fails_only_the_implements_check(registry, tmp_path):
    class Broken(_Good):
        NAMES = {"function": ("contract_lying_data",)}

        @classmethod
        def implements(cls, op):
            return True

    failed = _failures(_contract(Broken), tmp_path)
    assert "test_implements_agrees_with_what_the_class_overrides" in failed
    assert "test_every_name_resolves_to_the_class" not in failed


def test_a_backend_implementing_no_hook_fails_the_implements_check(registry, tmp_path):
    class Broken(Backend):
        NAMES = {"function": ("contract_empty_data",)}

    assert "test_implements_agrees_with_what_the_class_overrides" in _failures(
        _contract(Broken), tmp_path
    )


def test_a_check_available_that_raises_another_type_fails_only_that_check(
    registry, tmp_path
):
    class Broken(_Good):
        NAMES = {"function": ("contract_avail_data",)}

        @classmethod
        def check_available(cls):
            raise RuntimeError("no")

    # The engine calls check_available() too, so the lookup fails with it.
    assert _failures(_contract(Broken), tmp_path) == {
        "test_check_available_returns_or_raises_backend_error",
        "test_a_lookup_through_hiera_returns_the_same_values",
    }


def test_wrong_values_fail_only_the_values_check(registry, tmp_path):
    class Broken(_Good):
        NAMES = {"function": ("contract_wrong_data",)}

        def loads(self, text):
            return {key: "wrong" for key in json.loads(text)}

    failed = _failures(_contract(Broken), tmp_path)
    assert failed == {
        "test_a_hook_returns_the_values_of_present_keys",
        "test_a_lookup_through_hiera_returns_the_same_values",
    }


def test_finding_every_key_fails_only_the_missing_key_check(registry, tmp_path):
    class Broken(_Good):
        NAMES = {"function": ("contract_missing_data",)}

        def loads(self, text):
            return _Everything(json.loads(text))

    class _Everything(dict):
        def __contains__(self, key):
            return dict.__contains__(self, key) or key.startswith("hyera-contract")

    assert _failures(_contract(Broken), tmp_path) == {"test_a_missing_key_is_not_found"}


def test_a_value_outside_puppet_data_fails_only_the_data_types_check(
    registry, tmp_path
):
    class Broken(_Good):
        NAMES = {"function": ("contract_types_data",)}

        def loads(self, text):
            data = json.loads(text)
            data["contract_extra"] = {1, 2}
            return data

    failed = _failures(_contract(Broken), tmp_path)
    assert "test_every_returned_value_is_puppet_data" in failed
    assert "test_a_hook_returns_the_values_of_present_keys" not in failed
    assert "test_a_missing_key_is_not_found" not in failed


def test_a_foreign_exception_on_a_malformed_source_fails_only_that_check(
    registry, tmp_path
):
    class Broken(_Good):
        NAMES = {"function": ("contract_foreign_data",)}

        def loads(self, text):
            return json.loads(text)

        def load(self, source):
            with open(str(source), "rb") as fh:
                return self.loads(fh.read().decode("utf-8"))

    failed = _failures(_contract(Broken), tmp_path)
    assert failed == {
        "test_a_malformed_source_raises_backend_error",
        "test_an_unreadable_source_raises_backend_error",
    }


def test_swallowing_a_malformed_source_fails_only_the_malformed_check(
    registry, tmp_path
):
    class Broken(_Good):
        NAMES = {"function": ("contract_swallow_data",)}

        def load(self, source):
            try:
                return super().load(source)
            except BackendError:
                return {}

    failed = _failures(_contract(Broken), tmp_path)
    assert failed == {
        "test_a_malformed_source_raises_backend_error",
        "test_an_unreadable_source_raises_backend_error",
    }


def test_a_source_the_hierarchy_cannot_find_fails_only_the_hiera_check(
    registry, tmp_path
):
    class Broken(_Good):
        NAMES = {"function": ("contract_hiera_data",)}

    # Written one level too deep: the hierarchy's path no longer finds it.
    def write(self, directory, data):
        deeper = directory / "deeper" if directory.name == "data" else directory
        deeper.mkdir(parents=True, exist_ok=True)
        (deeper / "a.yaml").write_bytes(json.dumps(data).encode("utf-8"))
        return {"path": "a.yaml"}

    failed = _failures(_contract(Broken, write), tmp_path)
    assert failed == {"test_a_lookup_through_hiera_returns_the_same_values"}


def test_the_suite_leaves_out_the_hook_kinds_a_backend_lacks(registry, tmp_path):
    class LookupOnly(Backend):
        NAMES = {"function": ("contract_lookup_only",)}

        def lookup_key(self, key, options, context):
            def parse(text):
                try:
                    return json.loads(text)
                except ValueError:
                    raise BackendError("not json") from None

            data = context.cached_file_data(options["path"], parse)
            if key not in data:
                context.not_found()
            return data[key]

    assert _failures(_contract(LookupOnly), tmp_path) == set()
    assert LookupOnly.implements("lookup_key") and not LookupOnly.implements(
        "data_hash"
    )


def test_the_readme_example_is_a_backend_that_passes_its_own_contract(
    registry, tmp_path
):
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    (source,) = [b for b in blocks if "class TestJSONFileBackend" in b]
    namespace = {}
    exec(compile(source, "README.md", "exec"), namespace)
    assert _failures(namespace["TestJSONFileBackend"], tmp_path) == set()
