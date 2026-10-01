"""Static-typing consumer checks for :mod:`hyera.types`.

Not collected by pytest (no ``test_*``/``*_test`` name, matching the
project's ``[tool.pytest.ini_options]`` default): this file is never
executed, only type-checked, with ``pyright tests/typing/consumer_types.py``
on both the project's floor and latest Python
(``--pythonversion 3.9``/``3.14``), the way the CI ``types`` job's own
``--verifytypes`` run does for the rest of the package.

See ``src/hyera/AGENTS.md``'s "hyera.types" section for the two pyright
limitations this file works around rather than asserting away (neither is a
``hyera`` defect -- both are typeshed/pyright modeling an existing Python
mechanism for a narrower idiom than this module uses it for):

1. A **subscripted** type object (``Integer[1, 10]``) is a real, working
   ``isinstance`` second argument at runtime (``__instancecheck__``,
   verified in ``tests/test_public_types.py``), but typeshed's own
   ``isinstance`` overloads only accept a ``type``/``UnionType``/tuple of
   those -- a class with a custom ``__instancecheck__`` that is not itself
   a ``type`` is outside what pyright can verify there, so this file never
   passes one directly to ``isinstance``. The **bare** class form is
   unaffected -- it genuinely is a ``type``, so ``isinstance(5,
   types.Integer)`` type-checks clean.
2. ``Sensitive[String]`` is built with ``__class_getitem__`` (the chosen
   design for ``Sensitive``, already a concrete value-wrapper class, unlike
   every other name here, which is built on a dedicated metaclass instead).
   Pyright hard-codes ``ClassName[args]`` through ``__class_getitem__`` to
   ``type[ClassName]`` -- the common `Generic`/`NamedTuple` idiom it is
   built for -- regardless of the method's own declared return annotation,
   so ``reveal_type(Sensitive[String])`` always reads ``type[Sensitive]``,
   never the real ``SensitiveType`` instance it returns at runtime. The
   metaclass approach every other class uses does not have this problem
   (confirmed by (3) below correctly reading a non-``Any`` type), but
   retrofitting ``Sensitive`` onto one was out of scope.
"""

from hyera import types

# -- bare classes answer isinstance, with no subclassing involved ----------

if isinstance(5, types.Integer):
    reveal_type(5)  # int, unaffected by `types.Integer` not being `int`

# -- subscripting returns the true private type-object class (verified by
#    its NOT reading back as `Any`/`type[Integer]` the way (1)/(2) above do
#    for the two mechanisms this module cannot make pyright see through).

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
