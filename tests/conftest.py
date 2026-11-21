"""
Pytest bootstrap: install module stubs before any project source is imported.

conftest.py is loaded by pytest before test-module imports, so the stubs
are in place the first time any test file does `from agent import ...` or
`from garden_tool import ...`.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# sys.path — let test files import from src/ without installing the package
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ---------------------------------------------------------------------------
# strands
# ---------------------------------------------------------------------------
# Make @tool a simple pass-through decorator so the underlying function and
# its signature are fully preserved.  This is safe because Agent is also
# mocked — no tool object is ever sent to a real Strands runtime in tests.
_strands_stub = MagicMock()
_strands_stub.tool = lambda f: f   # pass-through; __signature__ survives
sys.modules["strands"] = _strands_stub
sys.modules["strands_tools"] = MagicMock()

# ---------------------------------------------------------------------------
# boto3
# ---------------------------------------------------------------------------
# agent.py makes module-level boto3.resource() calls that would fail without
# real AWS credentials.  Patch them once for the whole session.
_boto3_patch = patch("boto3.resource", return_value=MagicMock())
_boto3_patch.start()
