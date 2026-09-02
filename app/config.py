import os
from collections.abc import Mapping


def required_env(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = (source.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value
