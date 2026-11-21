"""
Pytest bootstrap: install module stubs before any project source is imported.

conftest.py is loaded by pytest before test-module imports, so the stubs
are in place the first time a test file does `from src.api import agent_runner`
or `from agent import ...`.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# sys.path
# ---------------------------------------------------------------------------
# Project root: lets tests import the deployed code as `src.api.agent_runner`,
# exactly as the Lambda handler (src.api.router.lambda_handler) does.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)
# src/: lets test_validate_user_id.py import the example module as `agent`.
sys.path.insert(0, os.path.join(_ROOT, "src"))

# ---------------------------------------------------------------------------
# strands
# ---------------------------------------------------------------------------
# Make @tool a pass-through decorator so each tool stays a plain function with
# its real signature. That signature is what Strands turns into the JSON schema
# sent to the model, so the schema tests can inspect it directly. Agent and
# BedrockModel are mocks, so nothing ever calls a real model.
_strands_stub = MagicMock()
_strands_stub.tool = lambda f: f
sys.modules["strands"] = _strands_stub
sys.modules["strands.models"] = MagicMock()
sys.modules["strands.models.bedrock"] = MagicMock()
sys.modules["strands.vended_tools"] = MagicMock()
sys.modules["strands_tools"] = MagicMock()

# ---------------------------------------------------------------------------
# boto3
# ---------------------------------------------------------------------------
# src/agent.py calls boto3.resource() at import time. Patch it for the whole
# session so no test needs AWS credentials. The deployed DAL modules create
# their tables lazily, and the guardrail tests patch the DAL functions anyway.
_boto3_patch = patch("boto3.resource", return_value=MagicMock())
_boto3_patch.start()
