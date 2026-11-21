"""
Unit tests for the validate_user_id guardrail in agent.py.

validate_user_id(user_id) -> (is_valid: bool, error_message: str)

Rules enforced:
- Must be provided (not None)
- Must be a str
- Must not be blank / whitespace-only
- May only contain letters, digits, underscores, and hyphens
- Must be 50 characters or fewer (after stripping)
"""

# conftest.py installs all stubs (strands, boto3) before this file is imported.
from agent import validate_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_valid(user_id):
    is_valid, msg = validate_user_id(user_id)
    assert is_valid is True, f"Expected valid for {user_id!r}, got error: {msg}"
    assert msg == ""


def assert_invalid(user_id, fragment: str):
    """Assert the call is rejected and the error contains *fragment*."""
    is_valid, msg = validate_user_id(user_id)
    assert is_valid is False, f"Expected invalid for {user_id!r}, but got is_valid=True"
    assert fragment in msg, f"Expected {fragment!r} in error message, got: {msg!r}"


# ---------------------------------------------------------------------------
# Valid inputs
# ---------------------------------------------------------------------------

class TestValidInputs:
    def test_simple_alphanumeric(self):
        assert_valid("user123")

    def test_underscore_allowed(self):
        assert_valid("user_name")

    def test_hyphen_allowed(self):
        assert_valid("user-name")

    def test_mixed_case(self):
        assert_valid("UserABC")

    def test_exactly_50_chars(self):
        assert_valid("a" * 50)

    def test_leading_trailing_whitespace_stripped(self):
        # Surrounding whitespace should be stripped before validation
        assert_valid("  valid_id  ")


# ---------------------------------------------------------------------------
# Missing / wrong type
# ---------------------------------------------------------------------------

class TestPresenceAndType:
    def test_none_is_required(self):
        assert_invalid(None, "required")

    def test_integer_rejected(self):
        assert_invalid(42, "string")

    def test_list_rejected(self):
        assert_invalid(["user123"], "string")

    def test_dict_rejected(self):
        assert_invalid({"id": "user123"}, "string")


# ---------------------------------------------------------------------------
# Empty / blank
# ---------------------------------------------------------------------------

class TestBlankInputs:
    def test_empty_string(self):
        assert_invalid("", "empty")

    def test_whitespace_only(self):
        assert_invalid("   ", "empty")

    def test_tab_only(self):
        assert_invalid("\t", "empty")


# ---------------------------------------------------------------------------
# Invalid characters
# ---------------------------------------------------------------------------

class TestInvalidCharacters:
    def test_space_inside(self):
        assert_invalid("user name", "invalid characters")

    def test_at_symbol(self):
        assert_invalid("user@domain", "invalid characters")

    def test_dot(self):
        assert_invalid("user.name", "invalid characters")

    def test_slash(self):
        assert_invalid("user/name", "invalid characters")

    def test_sql_injection_attempt(self):
        assert_invalid("'; DROP TABLE users; --", "invalid characters")

    def test_unicode_character(self):
        assert_invalid("usér", "invalid characters")


# ---------------------------------------------------------------------------
# Length limit
# ---------------------------------------------------------------------------

class TestLengthLimit:
    def test_51_chars_rejected(self):
        assert_invalid("a" * 51, "50 characters")

    def test_100_chars_rejected(self):
        assert_invalid("a" * 100, "50 characters")

    def test_51_chars_with_whitespace_still_rejected(self):
        # Whitespace is stripped first; the stripped value is still 51 chars.
        assert_invalid("  " + "a" * 51 + "  ", "50 characters")
