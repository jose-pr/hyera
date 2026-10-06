"""Static-typing consumer checks for :mod:`hyera.types`, type-checked and never run.

``pyright --pythonversion 3.9``/``3.14 tests/typing/consumer_types.py``. It works
around two pyright limits listed under "hyera.types" in ``src/hyera/AGENTS.md``.
"""

from hyera import types

# -- bare classes answer isinstance, with no subclassing involved ----------

if isinstance(5, types.Integer):
    reveal_type(5)  # int, unaffected by `types.Integer` not being `int`

# subscripting returns the true private type-object class (it does not read back as
# `Any`/`type[Integer]` like (1)/(2) above, which pyright cannot see through).

integer_range = types.Integer[1, 10]
reveal_type(integer_range)  # the private `_types.types.Integer` instance's
# own class -- shown by pyright as `Any` only because that happens to also
# be this project's own (unrelated) private base class's name.

# -- calling is annotated `Any` on the shared metaclass; an explicit target
#    annotation (or `cast`) is how a caller gets a precise static type back.

an_int: int = types.Integer("42")
a_float: float = types.Float("1.5")
a_str: str = types.String(42)
a_bool: bool = types.Boolean("true")
an_array: list = types.Array("ab")
a_hash: dict = types.Hash([["a", 1]])

reveal_type(types.Integer("42"))  # Any (documented limitation)
reveal_type(an_int)  # int, once narrowed by the annotation above

# -- Sensitive stays the existing value wrapper; only `Sensitive[T]` (a
#    type, not a value) is new here. See limitation (2) above for why
#    pyright calls this `type[Sensitive]` rather than the real `SensitiveType`.

sensitive_type = types.Sensitive[types.String]
reveal_type(sensitive_type)  # type[Sensitive] (documented limitation)
