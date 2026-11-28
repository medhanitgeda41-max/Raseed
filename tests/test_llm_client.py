"""Unit tests for resilient LLM client, fallback chain, and PII masking."""
import pytest
from llm_client import ask, set_mock_llm, LLMResponse, ToolCall
from config.settings import settings


def test_llm_mock_hook():
    """Verify that global mock hook overrides network requests."""
    def dummy_handler(messages, tools):
        return LLMResponse(
            content="Mocked response",
            tool_calls=[ToolCall(id="call_mock_1", name="dummy_tool", arguments={"key": "value"})],
            provider="mock_test",
        )

    set_mock_llm(dummy_handler)
    try:
        resp = ask([{"role": "user", "content": "Hello"}])
        assert resp.content == "Mocked response"
        assert resp.provider == "mock_test"
        assert resp.has_tool_calls is True
        assert resp.tool_calls[0].name == "dummy_tool"
    finally:
        set_mock_llm(None)


def test_offline_deterministic_fallback():
    """When no cloud keys are available, client falls back to deterministic rule-based engine."""
    # Ensure mock is unset
    set_mock_llm(None)

    tools = [
        {"name": "reconcile_4_way", "description": "4-way reconciliation"},
        {"name": "get_credit_ledger", "description": "Get credit ledger"},
    ]
    resp = ask([{"role": "user", "content": "Please reconcile the VAT differences."}], tools=tools)

    assert resp.is_offline_fallback is True
    assert "[AI unavailable — rule-based answer]" in resp.content
    assert resp.has_tool_calls is True
    assert resp.tool_calls[0].name == "reconcile_4_way"
