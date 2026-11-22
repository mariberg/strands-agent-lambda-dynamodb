"""
Pytest bootstrap: install module stubs before any project source is imported.

conftest.py is loaded by pytest before test-module imports, so the stubs
are in place the first time a test file does `from src.api import agent_runner`.
"""

import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# sys.path
# ---------------------------------------------------------------------------
# Project root: lets tests import the deployed code as `src.api.agent_runner`,
# exactly as the Lambda handler (src.api.router.lambda_handler) does.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# strands
# ---------------------------------------------------------------------------
# Make @tool a pass-through decorator so each tool stays a plain function with
# its real signature. That signature is what Strands turns into the JSON schema
# sent to the model, so the schema tests can inspect it directly. Agent and
# BedrockModel are mocks, so nothing ever calls a real model.
#
# No AWS stubbing is needed: the DAL modules create their DynamoDB tables
# lazily, and the guardrail tests patch every DAL function they touch.
_strands_stub = MagicMock()
_strands_stub.tool = lambda f: f
sys.modules["strands"] = _strands_stub
sys.modules["strands.models"] = MagicMock()
sys.modules["strands.models.bedrock"] = MagicMock()
