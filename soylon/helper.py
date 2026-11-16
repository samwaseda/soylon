from typing import TypeVar, cast

T = TypeVar("T")

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on", "enable", "enabled"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", "disable", "disabled"})


def convert_input(target_type: type[T], value: str) -> T:
    """Convert user input using the type's constructor, with explicit boolean parsing.

    Boolean values are case-insensitive and ignore surrounding whitespace.
    Unrecognized boolean values raise ValueError.
    """
    if target_type is bool:
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return cast(T, True)
        if normalized in _FALSE_VALUES:
            return cast(T, False)
        raise ValueError(f"Invalid boolean value: {value!r}")
    return target_type(value)
