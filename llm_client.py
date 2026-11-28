"""Resilient LLM client with multi-provider fallback chain, tool calling, and PII masking.

Fallback order (based on settings.llm_mode):
- Demo mode:
    1. Groq (openai/gpt-oss-120b)
    2. Groq (openai/gpt-oss-20b)
    3. Gemini Flash (gemini-2.5-flash via OpenAI endpoint)
    4. Ollama local (qwen3)
    5. Deterministic offline engine ("AI unavailable — rule-based answer")
- Local mode:
    1. Ollama local (qwen3)
    2. Groq (openai/gpt-oss-120b)
    3. Groq (openai/gpt-oss-20b)
    4. Gemini Flash
    5. Deterministic offline engine

Privacy Guarantee:
- TRN, IBAN, names, and emails are masked using guards.privacy BEFORE cloud dispatch.
- Responses are unmasked transparently.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable
from openai import OpenAI

from config.settings import settings
from guards.privacy import mask_payload, unmask_payload

logger = logging.getLogger("raseed.llm")

# Global mock hook for deterministic testing
_MOCK_LLM_HANDLER: Optional[Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], "LLMResponse"]] = None


def set_mock_llm(handler: Optional[Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], "LLMResponse"]]) -> None:
    """Set or reset a mock LLM handler for unit testing."""
    global _MOCK_LLM_HANDLER
    _MOCK_LLM_HANDLER = handler


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    provider: str = "offline_deterministic"
    is_offline_fallback: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


def _build_provider_chain(local_ai_only: bool = False) -> List[Dict[str, Any]]:
    """Construct ordered provider specifications according to settings.llm_mode and local_ai_only."""
    groq_120b = {
        "name": "groq:openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": settings.groq_api_key,
        "model": "openai/gpt-oss-120b",
        "is_cloud": True,
    }
    groq_20b = {
        "name": "groq:openai/gpt-oss-20b",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": settings.groq_api_key,
        "model": "openai/gpt-oss-20b",
        "is_cloud": True,
    }
    gemini_flash = {
        "name": "gemini:gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": settings.gemini_api_key,
        "model": "gemini-2.5-flash",
        "is_cloud": True,
    }
    ollama_qwen = {
        "name": "ollama:qwen3",
        "base_url": settings.ollama_base_url,
        "api_key": "ollama",
        "model": "qwen3",
        "is_cloud": False,
    }

    if local_ai_only:
        # Strictly local only: no cloud providers under any condition
        return [ollama_qwen]

    if settings.llm_mode == "local":
        return [ollama_qwen, groq_120b, groq_20b, gemini_flash]

    cloud = [p for p in (groq_120b, gemini_flash, groq_20b) if p["api_key"]]
    if cloud:
        # Cloud keys configured: answer only with Groq / Gemini (no local or rule-based fallback).
        return cloud
    return [groq_120b, groq_20b, gemini_flash, ollama_qwen]


def _cloud_only(providers: List[Dict[str, Any]]) -> bool:
    return bool(providers) and all(p["is_cloud"] and p["api_key"] for p in providers)


def _deterministic_offline_engine(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> LLMResponse:
    """Deterministic, zero-cloud fallback engine when all AI providers are unavailable.
    
    Inspects user intent and conversation context, executing rule-based routing or planning.
    Every answer is clearly labelled: '[AI unavailable — basic answer]'.
    """
    last_user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_message = str(msg.get("content", "")).lower()
            break

    tool_names = [t.get("name") or (t.get("function", {}).get("name") if isinstance(t.get("function"), dict) else "") for t in (tools or [])]

    # Greeting / Introduction
    if any(g in last_user_message for g in ["hello", "hi", "hey", "marhaba", "salam", "greeting"]):
        return LLMResponse(
            content=(
                "[AI unavailable — basic answer] Hello! I am Raseed (رصيد), your UAE VAT recovery assistant. "
                "I help you recover all eligible excess input tax before statutory expiry under Federal Decree-Law No. 8 of 2017 "
                "and Decree-Law No. 17 of 2025. How can I help you today?"
            ),
            tool_calls=[],
            provider="offline_deterministic",
            is_offline_fallback=True,
        )

    # Expiry / Due Soon query
    if any(w in last_user_message for w in ["expire", "expiry", "at risk", "due soon", "urgent", "deadline"]):
        if "show_panel" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Displaying credits approaching statutory expiry on your VAT Credit Panel.",
                tool_calls=[ToolCall(id="offline_call_exp", name="show_panel", arguments={"view": "at_risk"})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # Recovery Plan / Steps query
    if any(w in last_user_message for w in ["plan", "next step", "steps", "roadmap"]):
        if "show_panel" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Showing your prioritized VAT recovery plan.",
                tool_calls=[ToolCall(id="offline_call_plan", name="show_panel", arguments={"view": "recovery_plan"})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # Reconciliation / Numbers discrepancy query
    if any(w in last_user_message for w in ["reconcile", "reconciliation", "match", "difference", "variance", "why don't"]):
        if "reconcile_4_way" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] [AI unavailable — rule-based answer] Running 4-way statutory VAT reconciliation across registers, GL, and returns.",
                tool_calls=[ToolCall(id="offline_call_2", name="reconcile_4_way", arguments={})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )
        elif "show_panel" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] [AI unavailable — rule-based answer] Running 4-way statutory reconciliation across registers, GL, and VAT returns.",
                tool_calls=[ToolCall(id="offline_call_recon", name="show_panel", arguments={"view": "reconciliation"})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # Missing documents / Checklist query
    if any(w in last_user_message for w in ["missing", "documents", "document", "upload", "checklist"]):
        if "show_panel" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Showing the required and recommended documents checklist.",
                tool_calls=[ToolCall(id="offline_call_docs", name="show_panel", arguments={"view": "documents"})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # How much / Recover / Credits query
    if any(w in last_user_message for w in ["how much", "recover", "credit", "credits", "ledger", "balance"]):
        if "show_panel" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Querying statutory excess credit ledger balances across all tax periods.",
                tool_calls=[ToolCall(id="offline_call_credits", name="show_panel", arguments={"view": "all_periods"})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # Prepare refund query
    if any(w in last_user_message for w in ["prepare refund", "claim refund", "submit refund", "apply refund"]):
        if "stage_refund_claim" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Preparing official Form 201 VAT refund application for authorization.",
                tool_calls=[ToolCall(id="offline_call_ref", name="stage_refund_claim", arguments={})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # 1. Onboarding / Start Check
    if "start" in last_user_message or "status" in last_user_message or "where am i" in last_user_message:
        if "get_onboarding_status" in tool_names:
            return LLMResponse(
                content="[AI unavailable — basic answer] Checking onboarding status to determine required documents.",
                tool_calls=[ToolCall(id="offline_call_1", name="get_onboarding_status", arguments={})],
                provider="offline_deterministic",
                is_offline_fallback=True,
            )

    # Default Rule-based guidance
    return LLMResponse(
        content=(
            "[AI unavailable — basic answer] [AI unavailable — rule-based answer] Raseed is operating in deterministic offline mode. "
            "All statutory VAT rules, calculations, deadlines, and guards remain fully functional."
        ),
        tool_calls=[],
        provider="offline_deterministic",
        is_offline_fallback=True,
    )


def ask(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
    local_ai_only: bool = False,
) -> LLMResponse:
    """Execute LLM request with strict fallback chain, PII masking, and timeout safety."""
    # 0. Global mock hook (for tests)
    if _MOCK_LLM_HANDLER is not None:
        return _MOCK_LLM_HANDLER(messages, tools)

    prepared_messages: List[Dict[str, Any]] = []
    if system_prompt:
        prepared_messages.append({"role": "system", "content": system_prompt})
    prepared_messages.extend(messages)

    # Format tools for OpenAI client
    formatted_tools = None
    if tools:
        formatted_tools = []
        for t in tools:
            if "type" in t and "function" in t:
                formatted_tools.append(t)
            elif "name" in t:
                formatted_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })

    providers = _build_provider_chain(local_ai_only=local_ai_only)
    cloud_only = _cloud_only(providers)
    # In cloud-only mode keep trying (Groq -> Gemini -> Groq 20b) for a few rounds
    # instead of dropping to the rule-based engine; rate limits usually clear in seconds.
    rounds = 3 if cloud_only else 1
    attempts = [(r, p) for r in range(rounds) for p in providers]

    import time as _time
    last_round = -1
    for round_idx, provider_config in attempts:
        if round_idx != last_round:
            if round_idx > 0:
                _time.sleep(1.5 * round_idx)
            last_round = round_idx
        p_name = provider_config["name"]
        api_key = provider_config["api_key"]
        base_url = provider_config["base_url"]
        model = provider_config["model"]
        is_cloud = provider_config["is_cloud"]

        # Skip providers missing required API keys
        if is_cloud and not api_key:
            logger.debug("Skipping provider %s: API key not set.", p_name)
            continue

        try:
            # 1. PII Masking before cloud dispatch
            masked_messages = []
            combined_mapping: Dict[str, str] = {}

            for m in prepared_messages:
                content = m.get("content", "")
                if is_cloud and isinstance(content, str):
                    masked_content, mapping = mask_payload(content)
                    combined_mapping.update(mapping)
                    masked_messages.append({**m, "content": masked_content})
                else:
                    masked_messages.append(m)

            # 2. Execute Client Request (fast timeout <= 1.5s and 0 retries for local Ollama when offline)
            provider_timeout = 1.5 if not is_cloud else max(settings.request_timeout_seconds, 45)
            provider_retries = 0 if not is_cloud else max(settings.max_llm_retries, 2)

            client = OpenAI(
                api_key=api_key or "local",
                base_url=base_url,
                timeout=provider_timeout,
                max_retries=provider_retries,
            )

            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": masked_messages,
                "temperature": 0.0,
            }
            if formatted_tools:
                kwargs["tools"] = formatted_tools
                kwargs["tool_choice"] = "auto"

            logger.info("Attempting LLM call via provider: %s (%s)", p_name, model)
            completion = client.chat.completions.create(**kwargs)
            choice = completion.choices[0].message

            raw_content = choice.content or ""
            # Transparent unmasking
            clean_content = unmask_payload(raw_content, combined_mapping) if combined_mapping else raw_content

            parsed_tool_calls: List[ToolCall] = []
            if choice.tool_calls:
                for tc in choice.tool_calls:
                    fn_name = tc.function.name
                    raw_args = tc.function.arguments or "{}"
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {"raw": raw_args}
                    parsed_tool_calls.append(ToolCall(id=tc.id, name=fn_name, arguments=args))

            logger.info("Provider %s responded successfully. Tool calls: %d", p_name, len(parsed_tool_calls))
            return LLMResponse(
                content=clean_content,
                tool_calls=parsed_tool_calls,
                provider=p_name,
                is_offline_fallback=False,
            )

        except Exception as e:
            logger.warning("Provider %s failed: %s. Falling back to next provider...", p_name, str(e))
            continue

    if cloud_only:
        logger.error("Groq and Gemini both failed after %d rounds.", rounds)
        return LLMResponse(
            content=(
                "Sorry, the AI service is busy right now and didn't answer in time. "
                "Your data is safe. Please send your message again in a few seconds."
            ),
            tool_calls=[],
            provider="cloud_unavailable",
            is_offline_fallback=True,
        )

    # No cloud keys configured -> Deterministic offline engine
    logger.warning("All LLM providers failed or unavailable. Falling back to deterministic offline engine.")
    return _deterministic_offline_engine(prepared_messages, tools)
