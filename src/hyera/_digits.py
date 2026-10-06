"""Decimal integer parsing that does not depend on the interpreter's digit limit."""

__all__ = ["format_decimal_int", "parse_decimal_int"]

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


#: ``str(int)`` is safe up to this bit length (about 3600 digits).
_FORMAT_BITS = 12000


def format_decimal_int(value: int) -> str:
    """``str(value)`` for an ``int`` of any size (Python 3.11+ refuses to
    format more than 4300 decimal digits).

    :param value: the integer.
    :returns: its decimal digits, with a leading ``-`` when negative.
    """
    if value.bit_length() <= _FORMAT_BITS:
        return str(value)
    if value < 0:
        return "-" + _format_unsigned(-value)
    return _format_unsigned(value)


def _format_unsigned(value: int) -> str:
    if value.bit_length() <= _FORMAT_BITS:
        return str(value)
    half = int(value.bit_length() * 0.30103) // 2
    high, low = divmod(value, 10**half)
    return _format_unsigned(high) + _format_unsigned(low).zfill(half)
