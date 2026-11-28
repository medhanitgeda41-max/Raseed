"""Comprehensive unit tests for the Chat-First Agent architecture in Raseed.

Verifies:
1. 'hello' returns an LLM answer with the current journey step.
2. Follow-up conversational turns use and preserve history.
3. 'what is about to expire?' calls show_panel(at_risk) and uses verified ledger numbers.
4. Approval card appears for a refund submission.
5. Approve updates the panel data and executes the gateway action.
6. Only an Approver (or Owner) can approve consequential refund submissions.
7. An automatic DUE_SOON / URGENT alert is posted into the chat stream.
8. Empty workspace shows no numbers and guides user to Step 1 onboarding.
"""
from datetime import date, timedelta
from pathlib import Path
import pytest

from database.connection import init_db, create_business, create_user, get_db_connection
from agent.chat_agent import (
    get_journey_step,
    show_panel,
    execute_chat_turn,
    check_automatic_alerts,
)
from tools.read_tools import get_refund_status, get_credit_ledger, get_staged_actions
from tools.write_gateway import stage_action, execute_approved_action
from llm_client import set_mock_llm, LLMResponse, ToolCall


@pytest.fixture(autouse=True)
def reset_mock():
    """Ensure mock LLM handler is cleaned up after each test."""
    yield
    set_mock_llm(None)


def test_hello_returns_llm_answer_with_current_step(temp_db):
    """'hello' must return a conversational answer referencing the active journey step."""
    init_db(db_path=temp_db)
    biz_id = create_business("Desert Star Tech LLC", "100111222333001", db_path=temp_db)

    # Mock LLM handler that observes the context and replies conversationally
    def mock_llm_handler(messages, tools):
        # Verify context was injected
        sys_msg = messages[0]["content"]
        assert "Step 1 of 5" in sys_msg or "Business Registration" in sys_msg
        return LLMResponse(
            content="Hello! I am Raseed, your UAE VAT recovery assistant. We are currently at Step 1 of 5 — Business Registration. Let's register your profile.",
            tool_calls=[],
            provider="mock:groq-120b",
        )

    set_mock_llm(mock_llm_handler)

    res = execute_chat_turn(
        user_message="hello",
        history=[],
        business_id=biz_id,
        active_role="APPROVER",
        db_path=temp_db,
    )

    assert "Hello!" in res["content"]
    assert "Step 1 of 5" in res["content"]
    assert res["how_i_got_this"]["provider"] == "mock:groq-120b"


def test_followup_uses_history(temp_db):
    """Follow-up questions must include and preserve previous conversational context."""
    init_db(db_path=temp_db)
    biz_id = create_business("Emirates Logistics LLC", "100222333444002", db_path=temp_db)

    received_messages = []

    def mock_llm_handler(messages, tools):
        nonlocal received_messages
        received_messages = list(messages)
        return LLMResponse(
            content="Under Article 53 of the Executive Regulations, entertainment expenses for non-employees are blocked from recovery.",
            tool_calls=[],
            provider="mock:groq-120b",
        )

    set_mock_llm(mock_llm_handler)

    history = [
        {"role": "user", "content": "What is input tax recovery?"},
        {"role": "assistant", "content": "Input tax is the VAT paid on business expenses, recoverable under Article 54."},
    ]

    res = execute_chat_turn(
        user_message="Can I claim it for client dinners?",
        history=history,
        business_id=biz_id,
        active_role="APPROVER",
        db_path=temp_db,
    )

    # Check that previous turns were present in prompt sent to model
    contents = [m.get("content", "") for m in received_messages]
    assert any("What is input tax recovery?" in c for c in contents)
    assert any("Article 54" in c for c in contents)
    assert "Article 53" in res["content"]


def test_what_is_about_to_expire_calls_show_panel_and_uses_ledger_numbers(temp_db):
    """'what is about to expire?' must call show_panel(at_risk) and cite real ledger numbers."""
    init_db(db_path=temp_db)
    biz_id = create_business("Apex Trading FZE", "100333444555003", db_path=temp_db)

    # Insert a credit ledger row with URGENT status (e.g. 41 days left, 6300 AED)
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (
                business_id, period_code, period_end_date,
                output_tax_aed, verified_input_tax_aed, net_excess_aed,
                expiry_date, days_remaining, status, transitional_deadline_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "2021-Q1", "2021-03-31",
                1000.0, 7300.0, 6300.0,
                "2026-03-31", 41, "URGENT", 1,
            ),
        )
    conn.close()

    call_count = 0

    def mock_llm_handler(messages, tools):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First turn: model calls show_panel(view="at_risk")
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="show_panel", arguments={"view": "at_risk"})],
                provider="mock:groq-120b",
            )
        else:
            # Second turn: receives tool response with 6,300 AED and 41 days, explains to user
            tool_msg = next((m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1"), None)
            assert tool_msg is not None
            assert "6,300" in tool_msg["content"]
            assert "41" in tool_msg["content"]
            return LLMResponse(
                content="Period 2021-Q1 has 6,300.00 AED expiring in 41 days on 2026-03-31 under Decree-Law 17/2025. I have opened the At risk panel view for you.",
                tool_calls=[],
                provider="mock:groq-120b",
            )

    set_mock_llm(mock_llm_handler)

    res = execute_chat_turn(
        user_message="what is about to expire?",
        history=[],
        business_id=biz_id,
        active_role="APPROVER",
        follow_chat=True,
        db_path=temp_db,
    )

    assert res["panel_view"] == "at_risk"
    assert "6,300" in res["content"]
    assert "41 days" in res["content"]
    assert "show_panel" in res["how_i_got_this"]["tools"]


def test_approval_card_appears_for_refund_submission(temp_db):
    """When a refund is staged, an approval card record is created and ready for chat display."""
    init_db(db_path=temp_db)
    biz_id = create_business("Gulf Coast Freight LLC", "100444555666004", db_path=temp_db)

    # Stage refund action
    action_res = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload={"period_codes": ["2021-Q1"], "claimed_amount": 6300.0},
        reason="Recovering eligible input VAT before statutory expiry",
        business_id=biz_id,
        db_path=temp_db,
    )
    action_id = action_res[0] if isinstance(action_res, tuple) else action_res

    staged = get_staged_actions(status="PENDING_APPROVAL", business_id=biz_id, db_path=temp_db)
    assert len(staged) == 1
    act = staged[0]
    assert act["id"] == action_id
    assert act["action_type"] == "SUBMIT_REFUND_APPLICATION"
    assert act["status"] == "PENDING_APPROVAL"
    assert act["payload"]["claimed_amount"] == 6300.0
    assert act["payload"]["period_codes"] == ["2021-Q1"]


def test_approve_updates_panel_data(temp_db):
    """Approving a staged action commits the gateway transaction and updates refund records."""
    init_db(db_path=temp_db)
    biz_id = create_business("Falcon Express LLC", "100555666777005", db_path=temp_db)

    # Setup profile with masked IBAN
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO business_profile (
                business_id, legal_name, trn, vat_registration_date,
                tax_period_type, first_tax_period_start, business_activity,
                emirate, signatory_name, signatory_email, masked_iban
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "Falcon Express LLC", "100555666777005", "2020-01-01",
                "quarterly", "2020-01-01", "Logistics", "Dubai",
                "Sultan Al Qasimi", "sultan@falcon.ae", "AE23****************1234",
            ),
        )
        conn.execute(
            """
            INSERT INTO credit_ledger (
                business_id, period_code, period_end_date,
                output_tax_aed, verified_input_tax_aed, net_excess_aed,
                expiry_date, days_remaining, status, transitional_deadline_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "2021-Q1", "2021-03-31",
                0.0, 5000.0, 5000.0, "2026-03-31", 60, "DUE_SOON", 1,
            ),
        )
    conn.close()

    # Stage refund action
    action_res = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload={"period_codes": ["2021-Q1"], "claimed_amount": 5000.0},
        reason="Claim Q1 2021 refund",
        business_id=biz_id,
        db_path=temp_db,
    )
    action_id = action_res[0] if isinstance(action_res, tuple) else action_res

    # Approve with APPROVER role
    success, msg = execute_approved_action(
        action_id=action_id,
        approver_note="Authorized by CFO for EmaraTax submission",
        approver_role="APPROVER",
        business_id=biz_id,
        db_path=temp_db,
    )
    assert success is True
    assert "executed and committed" in msg

    # Panel data must reflect the refund record
    ref_status = get_refund_status(business_id=biz_id, db_path=temp_db)
    assert len(ref_status["records"]) == 1
    assert ref_status["records"][0]["total_refund_claimed_aed"] == 5000.0


def test_only_approver_sees_and_can_use_buttons(temp_db):
    """ACCOUNTANT cannot approve consequential actions; APPROVER or OWNER can."""
    init_db(db_path=temp_db)
    biz_id = create_business("Secure Trade LLC", "100666777888006", db_path=temp_db)

    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (
                business_id, period_code, period_end_date,
                output_tax_aed, verified_input_tax_aed, net_excess_aed,
                expiry_date, days_remaining, status, transitional_deadline_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "2021-Q2", "2021-06-30",
                0.0, 4000.0, 4000.0, "2026-06-30", 120, "OK", 1,
            ),
        )
    conn.close()

    action_res = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload={"period_codes": ["2021-Q2"], "claimed_amount": 4000.0},
        reason="Refund claim",
        business_id=biz_id,
        db_path=temp_db,
    )
    action_id = action_res[0] if isinstance(action_res, tuple) else action_res

    # ACCOUNTANT attempt -> BLOCKED
    acc_success, acc_msg = execute_approved_action(
        action_id=action_id,
        approver_note="Trying to approve",
        approver_role="ACCOUNTANT",
        business_id=biz_id,
        db_path=temp_db,
    )
    assert acc_success is False
    assert "not authorized" in acc_msg.lower() or "permission denied" in acc_msg.lower()

    # APPROVER attempt -> SUCCESS
    appr_success, appr_msg = execute_approved_action(
        action_id=action_id,
        approver_note="Approved by authorized signatory",
        approver_role="APPROVER",
        business_id=biz_id,
        db_path=temp_db,
    )
    assert appr_success is True
    assert "executed and committed" in appr_msg


def test_automatic_due_soon_message_is_posted(temp_db):
    """When a credit period enters DUE_SOON or URGENT, an alert message is generated."""
    init_db(db_path=temp_db)
    biz_id = create_business("Rapid Cargo LLC", "100777888999007", db_path=temp_db)

    # Insert DUE_SOON credit
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (
                business_id, period_code, period_end_date,
                output_tax_aed, verified_input_tax_aed, net_excess_aed,
                expiry_date, days_remaining, status, transitional_deadline_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "2021-Q3", "2021-09-30",
                500.0, 8500.0, 8000.0, "2026-06-30", 90, "DUE_SOON", 1,
            ),
        )
    conn.close()

    alerts = check_automatic_alerts(business_id=biz_id, existing_alert_keys=set(), db_path=temp_db)
    assert len(alerts) >= 1
    expiry_alert = next((a for a in alerts if "2021-Q3" in a["content"]), None)
    assert expiry_alert is not None
    assert "8,000.00 AED" in expiry_alert["content"]
    assert "DUE SOON" in expiry_alert["content"]


def test_empty_workspace_shows_no_numbers(temp_db):
    """An empty workspace must show 0.00 AED, no hallucinated numbers, and guide to Step 1."""
    init_db(db_path=temp_db)
    biz_id = create_business("Brand New Entity LLC", "100888999000008", db_path=temp_db)

    # 1. Financial totals must be 0
    ref_status = get_refund_status(business_id=biz_id, db_path=temp_db)
    assert ref_status["total_available_credits_aed"] == 0.0
    assert ref_status["at_risk_credits_aed"] == 0.0

    # 2. Panel must indicate no records
    panel_res = show_panel(view="all_periods", business_id=biz_id, db_path=temp_db)
    assert panel_res["data"] == []
    assert "No tax periods recorded" in panel_res["summary"]

    # 3. Journey must be Step 1 of 5
    journey = get_journey_step(business_id=biz_id, db_path=temp_db)
    assert journey["step_number"] == 1
    assert "Step 1 of 5" in journey["step_label"]
    assert journey["name"] == "Business Registration"


def test_conversational_onboarding_registers_business_in_chat(temp_db):
    """Business owner can provide TRN and company name directly in chat, creating the profile."""
    init_db(db_path=temp_db)
    biz_id = create_business("Default Entity", "100000000000003", db_path=temp_db)

    # Empty profile initially
    conn = get_db_connection(db_path=temp_db)
    conn.execute("DELETE FROM business_profile WHERE business_id = ?;", (biz_id,))
    conn.commit()
    conn.close()

    res = execute_chat_turn(
        user_message="My company name is Al Diwan Logistics LLC with TRN 100999888777005 and quarterly VAT filing",
        history=[],
        business_id=biz_id,
        active_role="OWNER",
        db_path=temp_db,
    )

    assert "Al Diwan Logistics LLC" in res["content"]
    assert "100XXXXXXXXX005" in res["content"]
    assert "Step 2" in res["content"]

    # Verify database was updated
    from tools.read_tools import get_business_profile
    prof = get_business_profile(business_id=biz_id, db_path=temp_db)
    assert prof is not None
    assert prof["legal_name"] == "Al Diwan Logistics LLC"
    assert prof["trn"] == "100999888777005"
    assert prof["tax_period_type"] == "quarterly"


def test_owner_is_approver_and_can_authorize(temp_db):
    """OWNER role can approve consequential refund submissions without needing an external accountant."""
    init_db(db_path=temp_db)
    biz_id = create_business("Star Trading LLC", "100111999222001", db_path=temp_db)

    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (
                business_id, period_code, period_end_date,
                output_tax_aed, verified_input_tax_aed, net_excess_aed,
                expiry_date, days_remaining, status, transitional_deadline_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "2021-Q1", "2021-03-31",
                0.0, 7500.0, 7500.0, "2026-03-31", 60, "DUE_SOON", 1,
            ),
        )
    conn.close()

    action_res = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload={"period_codes": ["2021-Q1"], "claimed_amount": 7500.0},
        reason="Owner refund claim",
        business_id=biz_id,
        db_path=temp_db,
    )
    action_id = action_res[0] if isinstance(action_res, tuple) else action_res

    # OWNER approves directly
    success, msg = execute_approved_action(
        action_id=action_id,
        approver_note="Authorized directly by business owner",
        approver_role="OWNER",
        business_id=biz_id,
        db_path=temp_db,
    )
    assert success is True
    assert "executed and committed" in msg
