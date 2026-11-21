"""
Guardrail tests for the garden-action tool.

Five areas covered (in order):

1. SCHEMA SHAPE
   The tool schema contains no user_id or garden_id field.  Any future
   addition of those parameters will break these tests immediately.

2. PLANT OWNERSHIP
   A write to a plant that doesn't exist in the user's garden, or belongs
   to a different user, is rejected.

3. INPUT VALIDATION
   Invalid action_type values and malformed dates are rejected before
   anything reaches the database.

4. SHARED VALIDATION PATH
   Both the agent-tool path and the REST endpoint call the same validation
   functions — validate_garden_action and validate_user_id — so tightening
   a rule in one place automatically tightens it everywhere.

5. ID INJECTION PREVENTION
   An attempt to pass user_id or garden_id in the tool input — simulating
   a mocked model response that tries to smuggle extra fields — is rejected
   by Python's own call convention.
"""

import inspect
import json
from unittest.mock import patch

import pytest

# conftest.py handles strands/boto3 stubs and sys.path before this is imported.
from agent import lambda_handler, validate_user_id
from garden_tool import (
    VALID_ACTION_TYPES,
    clear_authorized_plants,
    record_garden_action,
    set_authorized_plants,
    validate_garden_action,
)


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def lambda_url_event(body: dict) -> dict:
    """Minimal Lambda URL (HTTP) event."""
    return {
        "requestContext": {"http": {"method": "POST"}},
        "body": json.dumps(body),
    }


def direct_event(user_id) -> dict:
    """Minimal direct-invoke Lambda event."""
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# 1. Tool schema shape — no user_id or garden_id
# ---------------------------------------------------------------------------

class TestToolSchema:
    """
    The @tool decorator generates a JSON schema from the Python function
    signature.  If user_id or garden_id ever appear in the parameters, the
    LLM can supply them directly, bypassing the authorization layer.

    These tests fail the moment someone adds a forbidden parameter.
    """

    def _params(self) -> set:
        return set(inspect.signature(record_garden_action).parameters)

    def test_no_user_id_in_schema(self):
        assert "user_id" not in self._params(), (
            "user_id MUST NOT be in the tool schema; ownership is resolved "
            "from the authorization context set by lambda_handler, not from "
            "LLM-supplied input."
        )

    def test_no_garden_id_in_schema(self):
        assert "garden_id" not in self._params(), (
            "garden_id MUST NOT be in the tool schema."
        )

    def test_expected_params_are_present(self):
        """Guard against accidental removal of required parameters."""
        params = self._params()
        assert "action_type" in params
        assert "plant_id" in params
        assert "date" in params

    def test_only_known_params_are_present(self):
        """Enumerate all allowed parameters so new additions are flagged."""
        allowed = {"action_type", "plant_id", "date", "notes"}
        unexpected = self._params() - allowed
        assert not unexpected, (
            f"Unexpected parameter(s) in tool schema: {unexpected}.  "
            "If this is intentional, add the name to 'allowed' in this test."
        )


# ---------------------------------------------------------------------------
# 2. Plant ownership authorization
# ---------------------------------------------------------------------------

class TestPlantOwnership:
    """
    Writes must be rejected when the plant_id is not in the authenticated
    user's garden — whether it truly doesn't exist or belongs to someone else.
    """

    def setup_method(self):
        clear_authorized_plants()

    def teardown_method(self):
        clear_authorized_plants()

    def test_write_to_owned_plant_succeeds(self):
        set_authorized_plants(frozenset({"rose_1", "sunflower_2"}))
        result = record_garden_action("watered", "rose_1", "2024-06-01")
        assert result.get("status") == "recorded"
        assert "error" not in result

    def test_write_to_nonexistent_plant_rejected(self):
        set_authorized_plants(frozenset({"rose_1"}))
        result = record_garden_action("watered", "cactus_99", "2024-06-01")
        assert "error" in result

    def test_error_does_not_leak_other_plant_ids(self):
        """The error message must not reveal what plants the user does own."""
        set_authorized_plants(frozenset({"rose_1"}))
        result = record_garden_action("watered", "cactus_99", "2024-06-01")
        assert "rose_1" not in result.get("error", "")

    def test_write_to_another_users_plant_rejected(self):
        """User A's context must not allow writing to User B's plant."""
        set_authorized_plants(frozenset({"rose_1"}))          # user A
        result = record_garden_action("watered", "orchid_7", "2024-06-01")
        assert "error" in result

    def test_no_authorization_context_always_rejected(self):
        """
        If lambda_handler forgot to call set_authorized_plants, every write
        must be refused — including writes to real plant IDs.
        """
        # clear_authorized_plants called in setup_method; no set call here
        result = record_garden_action("watered", "rose_1", "2024-06-01")
        assert "error" in result
        assert "authorization" in result["error"].lower()


# ---------------------------------------------------------------------------
# 3. Input validation — action types and dates
# ---------------------------------------------------------------------------

class TestActionTypeValidation:

    def test_all_valid_action_types_accepted(self):
        for action in VALID_ACTION_TYPES:
            is_valid, msg = validate_garden_action(action, "rose_1", "2024-06-01")
            assert is_valid, f"Expected '{action}' to be valid, got: {msg}"

    def test_unknown_action_type_rejected(self):
        is_valid, msg = validate_garden_action("sprayed", "rose_1", "2024-06-01")
        assert not is_valid
        assert "action_type" in msg

    def test_empty_action_type_rejected(self):
        is_valid, _ = validate_garden_action("", "rose_1", "2024-06-01")
        assert not is_valid

    def test_action_type_with_injection_attempt_rejected(self):
        """SQL / shell injection in action_type must not reach the database."""
        is_valid, _ = validate_garden_action(
            "watered; DROP TABLE actions;", "rose_1", "2024-06-01"
        )
        assert not is_valid


class TestDateValidation:

    def test_valid_iso_date_accepted(self):
        is_valid, _ = validate_garden_action("watered", "rose_1", "2024-06-15")
        assert is_valid

    def test_datetime_string_rejected(self):
        """A datetime with a time component must not be accepted as a date."""
        is_valid, msg = validate_garden_action("watered", "rose_1", "2024-06-15T12:00:00")
        assert not is_valid
        assert "date" in msg.lower()

    def test_dd_mm_yyyy_format_rejected(self):
        """European date format must not silently parse as a different date."""
        is_valid, _ = validate_garden_action("watered", "rose_1", "15-06-2024")
        assert not is_valid

    def test_nonsense_string_rejected(self):
        is_valid, _ = validate_garden_action("watered", "rose_1", "not-a-date")
        assert not is_valid

    def test_month_out_of_range_rejected(self):
        is_valid, _ = validate_garden_action("watered", "rose_1", "2024-13-01")
        assert not is_valid

    def test_empty_date_rejected(self):
        is_valid, _ = validate_garden_action("watered", "rose_1", "")
        assert not is_valid


# ---------------------------------------------------------------------------
# 4. Shared validation path
# ---------------------------------------------------------------------------

class TestSharedValidationPath:
    """
    Both the agent-tool path and the REST endpoint must route through the
    same validation functions so that tightening one rule tightens both.
    """

    # --- validate_user_id is called by both lambda_handler entry points ---

    def test_rest_path_calls_validate_user_id(self):
        """Lambda URL handler must delegate user_id checking to validate_user_id."""
        with patch("agent.validate_user_id", wraps=validate_user_id) as mock_validate:
            with patch("agent.Agent") as MockAgent:
                MockAgent.return_value.return_value = {"summary": "ok", "details": {}}
                lambda_handler(lambda_url_event({"user_id": "user_42"}), None)
            mock_validate.assert_called_once_with("user_42")

    def test_direct_path_calls_validate_user_id(self):
        """Direct Lambda invocation must also delegate to validate_user_id."""
        with patch("agent.validate_user_id", wraps=validate_user_id) as mock_validate:
            with patch("agent.Agent") as MockAgent:
                MockAgent.return_value.return_value = {"summary": "ok", "details": {}}
                lambda_handler(direct_event("user_42"), None)
            mock_validate.assert_called_once_with("user_42")

    def test_same_invalid_user_id_rejected_on_both_paths(self):
        """
        An input that validate_user_id rejects must be rejected identically
        by both entry points — there must be no second path that skips validation.
        """
        bad_id = "bad id!"

        rest_response = lambda_handler(lambda_url_event({"user_id": bad_id}), None)
        direct_response = lambda_handler(direct_event(bad_id), None)

        # REST path returns a 400 HTTP response
        assert rest_response["statusCode"] == 400
        rest_body = json.loads(rest_response["body"])
        assert "invalid characters" in rest_body["message"].lower()

        # Direct path returns an error in the summary field
        assert "Error:" in direct_response.get("summary", "")
        assert "invalid characters" in direct_response["summary"].lower()

    # --- validate_garden_action is called by the tool, not duplicated ---

    def test_tool_delegates_to_validate_garden_action(self):
        """
        record_garden_action must call validate_garden_action rather than
        duplicating the logic inline.  If someone adds a new validation rule
        to validate_garden_action, the tool picks it up automatically.
        """
        with patch(
            "garden_tool.validate_garden_action", wraps=validate_garden_action
        ) as mock_validate:
            clear_authorized_plants()
            set_authorized_plants(frozenset({"rose_1"}))
            record_garden_action("watered", "rose_1", "2024-06-01")
            mock_validate.assert_called_once_with("watered", "rose_1", "2024-06-01")

    def test_validation_error_surfaces_through_tool(self):
        """
        An input that validate_garden_action rejects must produce an error
        response from the tool — not a silent pass-through.
        """
        clear_authorized_plants()
        set_authorized_plants(frozenset({"rose_1"}))
        result = record_garden_action("watered", "rose_1", "not-a-date")
        assert "error" in result
        assert "date" in result["error"].lower()


# ---------------------------------------------------------------------------
# 5. ID injection prevention
# ---------------------------------------------------------------------------

class TestIdInjectionPrevention:
    """
    These tests simulate a model that attempts to smuggle user_id or
    garden_id into a tool call.  Python's own call convention is the first
    line of defence: extra keyword arguments raise TypeError for functions
    whose signatures don't include them.

    With @tool as a pass-through, record_garden_action IS the real function,
    so its signature is fully enforced at call time.
    """

    def test_injecting_user_id_as_kwarg_raises_type_error(self):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            record_garden_action(
                action_type="watered",
                plant_id="rose_1",
                date="2024-06-01",
                user_id="attacker_id",          # LLM-injected field
            )

    def test_injecting_garden_id_as_kwarg_raises_type_error(self):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            record_garden_action(
                action_type="watered",
                plant_id="rose_1",
                date="2024-06-01",
                garden_id="someone_elses_garden",  # LLM-injected field
            )

    def test_valid_plant_id_format_still_requires_ownership(self):
        """
        Even a correctly formatted plant_id that exists somewhere in the
        system must be rejected if it is not in the current user's garden.
        """
        clear_authorized_plants()
        set_authorized_plants(frozenset({"rose_1"}))
        result = record_garden_action("watered", "orchid_7", "2024-06-01")
        assert "error" in result

    def test_tool_has_no_parameter_that_expands_authorization(self):
        """
        Enumerate suspicious parameter names.  Any of these would give the
        model a mechanism to widen its own permissions.
        """
        params = set(inspect.signature(record_garden_action).parameters)
        forbidden = {
            "user_id", "garden_id", "authorized_plants",
            "owner", "owner_id", "override",
        }
        intruders = params & forbidden
        assert not intruders, (
            f"Parameter(s) {intruders} found in tool signature — the model "
            "could use these to override authorization."
        )
