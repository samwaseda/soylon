import builtins
import inspect
from functools import wraps
from typing import get_type_hints, get_origin, get_args


def human(func):
    # This runs at decoration time, not function-call time.
    hints = get_type_hints(func)
    return_type = hints.get("return", inspect.Signature.empty)

    if return_type is inspect.Signature.empty:
        raise TypeError(
            f"{func.__qualname__} must have a return type hint"
        )

    if return_type is type(None):
        raise TypeError(
            f"{func.__qualname__} must declare an output type"
        )

    if get_origin(return_type) is tuple:
        output_types = get_args(return_type)
    else:
        output_types = (return_type,)

    if not output_types:
        raise TypeError(
            f"{func.__qualname__} must declare at least one output type"
        )

    @wraps(func)
    def wrapper(*args, **kwargs):
        original_input = builtins.input
        input_count = 0

        def checked_input(prompt=""):
            nonlocal input_count

            value = original_input(prompt)

            if value.startswith("Error:"):
                raise RuntimeError(value)

            if input_count >= len(output_types):
                raise TypeError(
                    f"{func.__qualname__} called input() more times "
                    "than its return annotation specifies"
                )

            target_type = output_types[input_count]
            input_count += 1

            if target_type is bool:
                normalized = value.strip().lower()
                if normalized not in ("true", "false"):
                    raise ValueError(
                        f"Expected 'True' or 'False', got {value!r}"
                    )
                return normalized == "true"

            return target_type(value)

        builtins.input = checked_input
        try:
            return func(*args, **kwargs)
        finally:
            builtins.input = original_input

    return wrapper
