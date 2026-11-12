import pytest

from mcqq_bridge.security import bearer_matches


def test_bearer_token_uses_constant_time_comparison():
    assert bearer_matches("Bearer abc", "abc") is True
    assert bearer_matches("Bearer wrong", "abc") is False
    assert bearer_matches(None, "abc") is False
    assert bearer_matches("Bearer abc", "") is False


def test_event_payload_limits_are_enforced():
    from mcqq_bridge.models import ChatEvent

    with pytest.raises(ValueError):
        ChatEvent(player="x" * 257, message="hello")
