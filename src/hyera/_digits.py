"""Decimal integer parsing that does not depend on the interpreter's digit limit."""

__all__ = ["parse_decimal_int"]

#: Python 3.11+ refuses ``int(str)`` above 4300 decimal digits; chunks stay
#: well under that.
_CHUNK = 3000


def parse_decimal_int(text: str) -> int:
    """``int(text)`` for an optionally signed run of ASCII digits, with no
    upper bound on the digit count (Ruby's ``Integer`` has none).

    :param text: ``[+-]?[0-9]+``.
    :returns: the integer.
    :raises ValueError: ``text`` is not such a run.
    """
    if len(text) <= _CHUNK:
        return int(text)
    negative = text[0] == "-"
    digits = text[1:] if text[0] in "+-" else text
    if not digits.isascii() or not digits.isdigit():
        raise ValueError("invalid decimal integer")
    value = _unsigned(digits)
    return -value if negative else value


def _unsigned(digits: str) -> int:
    count = len(digits)
    if count <= _CHUNK:
        return int(digits)
    low_len = count // 2
    return _unsigned(digits[:-low_len]) * 10**low_len + _unsigned(digits[-low_len:])
