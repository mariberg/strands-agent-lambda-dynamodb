"""
Guardrail tests for the deployed agent tools in src/api/agent_runner.py.

The Lambda runs src.api.router.lambda_handler, which calls run_chat and
run_session_open in agent_runner.py. These tests import that module directly,
so a regression in the code that actually runs will fail here.

The design being locked in: the model never chooses which garden it acts on.
run_chat / run_session_open bind the garden to a ContextVar before the agent
starts, and every garden-scoped tool reads it from there. No tool takes a
garden_id or user_id parameter, so the model has no field to put one in.

Areas covered:
1. Tool schemas      - no garden_id/user_id on any tool; params are pinned.
2. Context binding   - the runners bind the garden and register only scoped tools.
3. No context        - every tool refuses to touch the DB without a bound garden.
4. Plant ownership   - writes to plants outside the bound garden are rejected.
5. Scoped reads      - reads only ever query the bound garden.
6. Input validation  - bad values are rejected (never silently dropped) and
                       the model gets the reason back.
7. Shared write path - the agent tools and the REST routes both write through
                       garden_service, and produce identical DynamoDB items.
8. ID injection      - a model-supplied garden_id/user_id is rejected outright.

The validation rules themselves are tested once, in test_garden_service.py.
"""

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

# conftest.py stubs strands and sets sys.path before these imports.
from src.api import agent_runner as ar
from src.api import router
from src.api.enums import ActionType, Severity
from src.api.services import garden_service

AUTHORIZED_GARDEN = "demo-garden-1"
OTHER_GARDEN = "demo-garden-4"

# Plants that exist in the fake table, keyed by (garden_id, instance_id).
# orchid-07 is real, but it belongs to someone else's garden.
FAKE_PLANTS = {
    (AUTHORIZED_GARDEN, "rose-01"): {"instance_id": "rose-01", "nickname": "Big Rose"},
    (OTHER_GARDEN, "orchid-07"): {"instance_id": "orchid-07", "nickname": "Not Yours"},
}

# Every tool that reads or writes garden data. Each must get its garden from
# the authorized context, never from a parameter.
GARDEN_TOOLS = [
    ar.get_garden_plants,
    ar.get_recent_plant_logs,
    ar.save_dashboard_summary,
    ar.create_action_log,
    ar.add_plant,
]

# The full parameter list of each tool is pinned. Adding any parameter fails a
# test, so a garden_id can't be slipped back in without someone noticing.
EXPECTED_PARAMS = {
    "get_garden_plants": set(),
    "get_recent_plant_logs": {"instance_id"},
    "save_dashboard_summary": {"summary_text", "plant_highlights"},
    "create_action_log": {"instance_id", "action_type", "notes", "title", "severity"},
    "add_plant": {"species_id", "nickname", "emoji", "garden_location"},
}

# Names that would let the model choose or widen whose data it touches.
FORBIDDEN_PARAMS = {"garden_id", "user_id", "owner_id", "authorized_garden", "override"}

# Valid arguments for each tool, used to show the only thing wrong in the
# injection tests is the extra ID.
VALID_ARGS = {
    "get_garden_plants": {},
    "get_recent_plant_logs": {"instance_id": "rose-01"},
    "save_dashboard_summary": {"summary_text": "All good.", "plant_highlights": {}},
    "create_action_log": {"instance_id": "rose-01", "action_type": "watered"},
    "add_plant": {"species_id": "rosa", "nickname": "Rose"},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def authorized_garden():
    """Bind AUTHORIZED_GARDEN for each test, then restore the previous value."""
    token = ar._authorized_garden_id.set(AUTHORIZED_GARDEN)
    yield
    ar._authorized_garden_id.reset(token)


@pytest.fixture
def no_garden_context():
    """Simulate a runner that forgot to call set_authorized_garden."""
    token = ar._authorized_garden_id.set(None)
    yield
    ar._authorized_garden_id.reset(token)


@pytest.fixture
def dal():
    """Replace every DAL call the tools make with a mock. Nothing touches AWS."""
    with patch.object(ar.garden_data, "get_plant",
                      side_effect=lambda g, i: FAKE_PLANTS.get((g, i))) as get_plant, \
         patch.object(ar.garden_data, "put_plant") as put_plant, \
         patch.object(ar.garden_data, "put_log") as put_log, \
         patch.object(ar.garden_data, "list_plants", return_value=[]) as list_plants, \
         patch.object(ar.garden_data, "list_logs_for_plant", return_value=[]) as list_logs, \
         patch.object(ar.garden_data, "put_dashboard_summary") as put_dashboard, \
         patch.object(ar.species_dal, "get_species",
                      side_effect=lambda s: {"common_name": "Rose"} if s == "rosa" else None) as get_species:
        yield {
            "get_plant": get_plant,
            "put_plant": put_plant,
            "put_log": put_log,
            "list_plants": list_plants,
            "list_logs_for_plant": list_logs,
            "put_dashboard_summary": put_dashboard,
            "get_species": get_species,
        }


def assert_nothing_written(dal):
    dal["put_plant"].assert_not_called()
    dal["put_log"].assert_not_called()
    dal["put_dashboard_summary"].assert_not_called()


def is_error(result) -> bool:
    """Tools return either an 'Error...' string or a list with an error dict."""
    if isinstance(result, str):
        return result.startswith("Error") or result.startswith("Invalid")
    if isinstance(result, list):
        return bool(result) and "error" in result[0]
    return False


# ---------------------------------------------------------------------------
# 1. Tool schemas
# ---------------------------------------------------------------------------

class TestToolSchemas:
    """The tool signature is the schema the model sees. Lock it down."""

    @pytest.mark.parametrize("tool", GARDEN_TOOLS, ids=lambda t: t.__name__)
    def test_no_forbidden_params(self, tool):
        params = set(inspect.signature(tool).parameters)
        assert not params & FORBIDDEN_PARAMS, (
            f"{tool.__name__} exposes {params & FORBIDDEN_PARAMS} to the model. "
            "The garden must come from the authorized context, not a parameter."
        )

    @pytest.mark.parametrize("tool", GARDEN_TOOLS, ids=lambda t: t.__name__)
    def test_params_are_exactly_as_expected(self, tool):
        params = set(inspect.signature(tool).parameters)
        assert params == EXPECTED_PARAMS[tool.__name__], (
            f"{tool.__name__} params changed to {params}. If this is intentional "
            "and safe, update EXPECTED_PARAMS in this test."
        )


# ---------------------------------------------------------------------------
# 2. Context binding in the runners
# ---------------------------------------------------------------------------

class TestRunnersBindGarden:
    """The runners must bind the garden before the agent runs."""

    def _capture_garden_during_agent_call(self, run):
        """Run `run()` with a fake Agent that records the bound garden when invoked."""
        seen = {}

        def fake_agent_call(*_args, **_kwargs):
            seen["garden"] = ar._current_garden_id()
            return "ok"

        with patch.object(ar, "Agent") as MockAgent, \
             patch.object(ar.garden_data, "get_garden_metadata", return_value={"city": "Test"}), \
             patch.object(ar.garden_data, "get_chat_session", return_value=[]), \
             patch.object(ar.garden_data, "put_chat_session"):
            MockAgent.return_value.side_effect = fake_agent_call
            run()
            return seen.get("garden"), MockAgent.call_args.kwargs["tools"]

    def test_run_chat_binds_the_requested_garden(self):
        ar._authorized_garden_id.set(None)
        garden, _ = self._capture_garden_during_agent_call(
            lambda: ar.run_chat("demo-garden-9", "hello")
        )
        assert garden == "demo-garden-9"

    def test_run_session_open_binds_the_requested_garden(self):
        ar._authorized_garden_id.set(None)
        garden, _ = self._capture_garden_during_agent_call(
            lambda: ar.run_session_open("demo-garden-9")
        )
        assert garden == "demo-garden-9"

    def test_chat_agent_registers_only_known_tools(self):
        """Adding a tool to the chat agent must be a deliberate, reviewed change."""
        _, tools = self._capture_garden_during_agent_call(
            lambda: ar.run_chat(AUTHORIZED_GARDEN, "hello")
        )
        assert tools == [
            ar.http_request,
            ar.get_garden_plants,
            ar.get_plant_species,
            ar.get_recent_plant_logs,
            ar.add_plant,
            ar.create_action_log,
        ]


# ---------------------------------------------------------------------------
# 3. No authorized context
# ---------------------------------------------------------------------------

class TestNoContextRefuses:
    """If a runner forgets to bind a garden, every tool refuses and nothing is written."""

    @pytest.mark.parametrize("tool", GARDEN_TOOLS, ids=lambda t: t.__name__)
    def test_tool_refuses_without_context(self, tool, dal, no_garden_context):
        result = tool(**VALID_ARGS[tool.__name__])
        assert is_error(result), f"{tool.__name__} ran without a bound garden: {result!r}"
        assert_nothing_written(dal)
        dal["list_plants"].assert_not_called()
        dal["list_logs_for_plant"].assert_not_called()


# ---------------------------------------------------------------------------
# 4. Plant ownership on writes
# ---------------------------------------------------------------------------

class TestPlantOwnership:

    def test_log_for_owned_plant_is_written_to_bound_garden(self, dal):
        result = ar.create_action_log("rose-01", "watered")
        assert not is_error(result), result
        assert dal["put_log"].call_args.kwargs["garden_id"] == AUTHORIZED_GARDEN

    def test_log_for_nonexistent_plant_is_rejected(self, dal):
        result = ar.create_action_log("lemon-tree-99", "watered")
        assert is_error(result)
        assert_nothing_written(dal)

    def test_log_for_another_gardens_plant_is_rejected(self, dal):
        """orchid-07 exists, but only in OTHER_GARDEN. The lookup must use the bound garden."""
        result = ar.create_action_log("orchid-07", "watered")
        assert is_error(result)
        assert_nothing_written(dal)
        dal["get_plant"].assert_called_once_with(AUTHORIZED_GARDEN, "orchid-07")

    def test_add_plant_writes_to_bound_garden(self, dal):
        result = ar.add_plant("rosa", "Rose")
        assert not is_error(result), result
        assert dal["put_plant"].call_args.args[0] == AUTHORIZED_GARDEN


# ---------------------------------------------------------------------------
# 5. Reads are scoped too
# ---------------------------------------------------------------------------

class TestScopedReads:
    """Reads leak data just as surely as writes corrupt it."""

    def test_get_garden_plants_reads_bound_garden(self, dal):
        ar.get_garden_plants()
        dal["list_plants"].assert_called_once_with(AUTHORIZED_GARDEN)

    def test_get_recent_plant_logs_reads_bound_garden(self, dal):
        ar.get_recent_plant_logs("orchid-07")
        dal["list_logs_for_plant"].assert_called_once_with(AUTHORIZED_GARDEN, "orchid-07")

    def test_save_dashboard_summary_writes_bound_garden(self, dal):
        ar.save_dashboard_summary("All good.", {})
        assert dal["put_dashboard_summary"].call_args.args[0] == AUTHORIZED_GARDEN


# ---------------------------------------------------------------------------
# 6. Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:

    @pytest.mark.parametrize("bad_type", ["sprayed", "", "WATERED", "watered; DROP TABLE logs"])
    def test_invalid_action_type_rejected(self, dal, bad_type):
        result = ar.create_action_log("rose-01", bad_type)
        assert is_error(result)
        assert_nothing_written(dal)

    def test_invalid_severity_rejected_with_reason(self, dal):
        """Rejected, not dropped: the model gets the valid values back and can retry."""
        result = ar.create_action_log("rose-01", "issue_found", severity="catastrophic")
        assert is_error(result)
        assert "severity" in result
        assert_nothing_written(dal)

    def test_unknown_plant_error_points_model_to_the_right_tools(self, dal):
        result = ar.create_action_log("lemon-tree-99", "watered")
        assert "get_garden_plants" in result
        assert "add_plant" in result

    def test_unknown_species_rejected(self, dal):
        result = ar.add_plant("not-a-real-species", "Mystery")
        assert is_error(result)
        assert_nothing_written(dal)

    @pytest.mark.parametrize("species_id, nickname", [("", "Rose"), ("rosa", "")])
    def test_missing_required_field_rejected(self, dal, species_id, nickname):
        result = ar.add_plant(species_id, nickname)
        assert is_error(result)
        assert_nothing_written(dal)


# ---------------------------------------------------------------------------
# 7. Shared write path: agent tools and REST routes use garden_service
# ---------------------------------------------------------------------------

FROZEN_NOW = "2026-06-01T09:00:00+00:00"


def function_url_event(method: str, path: str, body: dict) -> dict:
    """Minimal Lambda Function URL (payload v2) event for the real router."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "127.0.0.1", "userAgent": "pytest"},
            "requestId": "test-request",
            "routeKey": "$default",
            "stage": "$default",
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def call_rest(method: str, path: str, body: dict) -> dict:
    """Send a request through src.api.router.lambda_handler, as the Lambda does."""
    return router.lambda_handler(function_url_event(method, path, body), MagicMock())


@pytest.fixture
def frozen_clock():
    """Fix the service's clock so items written at different moments compare equal."""
    with patch.object(garden_service, "_now_iso", return_value=FROZEN_NOW):
        yield


class TestSharedWritePath:
    """
    Both the agent tools and the REST routes write through garden_service.
    Validation and item format live there once. These tests fail if either
    path starts building or validating items on its own again.
    """

    # The REST routes get the garden from require_garden (DEMO_GARDEN_ID),
    # which defaults to the same garden the agent tests are bound to.

    def test_agent_add_plant_uses_service(self, dal):
        with patch.object(garden_service, "create_plant",
                          wraps=garden_service.create_plant) as spy:
            ar.add_plant("rosa", "Rose")
        assert spy.call_args.args[0] == AUTHORIZED_GARDEN

    def test_rest_create_plant_uses_service(self, dal):
        with patch.object(garden_service, "create_plant",
                          wraps=garden_service.create_plant) as spy:
            resp = call_rest("POST", "/api/v1/plants", {"species_id": "rosa", "nickname": "Rose"})
        assert resp["statusCode"] == 201
        assert spy.call_args.args[0] == AUTHORIZED_GARDEN

    def test_agent_action_log_uses_service(self, dal):
        with patch.object(garden_service, "create_action_log",
                          wraps=garden_service.create_action_log) as spy:
            ar.create_action_log("rose-01", "watered")
        assert spy.call_args.args[:2] == (AUTHORIZED_GARDEN, "rose-01")

    def test_rest_action_log_uses_service(self, dal):
        with patch.object(garden_service, "create_action_log",
                          wraps=garden_service.create_action_log) as spy:
            resp = call_rest("POST", "/api/v1/plants/rose-01/action-logs", {"action_type": "watered"})
        assert resp["statusCode"] == 201
        assert spy.call_args.args[:2] == (AUTHORIZED_GARDEN, "rose-01")

    def test_same_plant_produces_identical_items(self, dal, frozen_clock):
        call_rest("POST", "/api/v1/plants", {
            "species_id": "rosa", "nickname": "Rose", "emoji": "🌹", "garden_location": "South bed",
        })
        ar.add_plant("rosa", "Rose", emoji="🌹", garden_location="South bed")

        (rest_call, agent_call) = dal["put_plant"].call_args_list
        assert rest_call.args[0] == agent_call.args[0]   # same garden
        assert rest_call.args[2] == agent_call.args[2]   # same item

    def test_same_action_produces_identical_items(self, dal, frozen_clock):
        # logged_by is the one field that differs by design, so pass it explicitly.
        call_rest("POST", "/api/v1/plants/rose-01/action-logs", {
            "action_type": "issue_found", "severity": "mild",
            "title": "Aphids", "notes": "On new growth", "logged_by": "agent",
        })
        ar.create_action_log("rose-01", "issue_found", notes="On new growth",
                             title="Aphids", severity="mild")

        (rest_call, agent_call) = dal["put_log"].call_args_list
        for field in ("garden_id", "instance_id", "occurred_at", "item"):
            assert rest_call.kwargs[field] == agent_call.kwargs[field], field

    def test_agent_actions_are_marked_as_agent(self, dal):
        ar.create_action_log("rose-01", "watered")
        assert dal["put_log"].call_args.kwargs["item"]["logged_by"] == "agent"

    def test_same_bad_input_rejected_on_both_paths(self, dal):
        """One rule, two paths: REST returns 400, the agent gets the same message."""
        resp = call_rest("POST", "/api/v1/plants/rose-01/action-logs",
                         {"action_type": "watered", "severity": "catastrophic"})
        agent_result = ar.create_action_log("rose-01", "watered", severity="catastrophic")

        assert resp["statusCode"] == 400
        rest_message = json.loads(resp["body"])["message"]
        assert agent_result == f"Error: {rest_message}"
        assert_nothing_written(dal)


class TestModelFacingValuesMatchEnums:
    """The prompt and tool description list exactly what the service accepts."""

    def test_tool_description_lists_every_action_type(self):
        for action in ActionType:
            assert action.value in ar.create_action_log.__doc__

    def test_tool_description_lists_every_severity(self):
        for severity in Severity:
            assert severity.value in ar.create_action_log.__doc__

    def test_chat_prompt_lists_every_action_type(self):
        for action in ActionType:
            assert action.value in ar.CHAT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 8. ID injection
# ---------------------------------------------------------------------------

class TestIdInjection:
    """
    Simulates a model response that adds an ID the schema doesn't offer, e.g.
    {"name": "add_plant", "input": {"species_id": "rosa", ..., "garden_id": "demo-garden-4"}}.
    The call must fail outright rather than silently pick a garden.
    """

    @pytest.mark.parametrize("tool", GARDEN_TOOLS, ids=lambda t: t.__name__)
    @pytest.mark.parametrize("injected", [
        {"garden_id": OTHER_GARDEN},
        {"user_id": "demo-user-4"},
    ], ids=["garden_id", "user_id"])
    def test_injected_id_is_rejected(self, tool, injected, dal):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            tool(**VALID_ARGS[tool.__name__], **injected)
        assert_nothing_written(dal)
