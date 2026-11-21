"""Demo middleware: injects a hardcoded garden_id and user_id for Lambda URL testing.

There is no authentication in this demo — garden_id and user_id are read from
environment variables and injected into every request handler. To target a
different garden/user, change the env vars:
  DEMO_GARDEN_ID  (default: "demo-garden-1")
  DEMO_USER_ID    (default: "demo-user-1")
"""

import functools
import os
from typing import Callable


DEMO_GARDEN_ID = os.environ.get("DEMO_GARDEN_ID", "demo-garden-1")
DEMO_USER_ID = os.environ.get("DEMO_USER_ID", "demo-user-1")


def require_garden(fn: Callable) -> Callable:
    """Decorator that injects demo garden_id and user_id into handler kwargs."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        kwargs["garden_id"] = DEMO_GARDEN_ID
        kwargs["user_id"] = DEMO_USER_ID
        return fn(*args, **kwargs)

    return wrapper
