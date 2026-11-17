from types import UnionType
from typing import Any, TypeVar, Union, cast, get_args, get_origin, overload

T = TypeVar("T")

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on", "enable", "enabled"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", "disable", "disabled"})


@overload
def convert_input(target_type: type[T], value: str) -> T: ...


@overload
def convert_input(target_type: UnionType, value: str) -> Any: ...


def convert_input(target_type: type[T] | UnionType, value: str) -> T:
    """Convert user input using a type's constructor or the first matching union member.

    Boolean values are case-insensitive and ignore surrounding whitespace.
    Unrecognized boolean values raise ValueError. Union members are tried in
    order; if none can convert the value, ValueError is raised.
    """
    if get_origin(target_type) in (UnionType, Union):
        last_error = None
        for member in get_args(target_type):
            try:
                return convert_input(member, value)
            except (TypeError, ValueError) as error:
                last_error = error
        raise ValueError(f"Cannot convert {value!r} to {target_type}") from last_error

    if target_type is bool:
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return cast(T, True)
        if normalized in _FALSE_VALUES:
            return cast(T, False)
        raise ValueError(f"Invalid boolean value: {value!r}")
    return target_type(value)
