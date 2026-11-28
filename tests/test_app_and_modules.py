"""Tests verifying list_businesses() access control and importability of all project modules."""
import importlib
import pkgutil
from pathlib import Path
import pytest

from database.connection import (
    init_db,
    create_business,
    create_user,
    list_businesses,
    get_db_connection,
)
from models.schemas import BusinessProfile


def test_list_businesses_returns_only_user_businesses(temp_db):
    """list_businesses(username) must enforce tenant access control and return masked TRN."""
    # 1. Initialize schema
    init_db(db_path=temp_db)

    # 2. Create isolated businesses
    biz1_id = create_business("Alpha Logistics LLC", "100111222333001", db_path=temp_db)
    biz2_id = create_business("Beta Trading LLC", "100444555666002", db_path=temp_db)

    # 3. Create users with assigned tenant roles
    create_user(business_id=biz1_id, username="alice", role="OWNER", db_path=temp_db)
    create_user(business_id=biz2_id, username="bob", role="ACCOUNTANT", db_path=temp_db)
    create_user(business_id=biz1_id, username="charlie", role="APPROVER", db_path=temp_db)
    create_user(business_id=biz2_id, username="charlie", role="APPROVER", db_path=temp_db)

    # 4. Set a legal profile for business 1 to test legal_name resolution
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
                biz1_id,
                "Alpha Logistics Global LLC",
                "100111222333001",
                "2020-01-01",
                "quarterly",
                "2020-01-01",
                "Logistics",
                "Dubai",
                "Ahmed Al Mansoori",
                "ahmed@alphalogistics.ae",
                "AE23****************1234",
            ),
        )
    conn.close()

    # 5. Alice should only see Alpha Logistics
    alice_biz = list_businesses(username="alice", db_path=temp_db)
    assert len(alice_biz) == 1
    assert alice_biz[0]["id"] == biz1_id
    assert alice_biz[0]["legal_name"] == "Alpha Logistics Global LLC"
    assert alice_biz[0]["name"] == "Alpha Logistics LLC"
    assert alice_biz[0]["trn"].startswith("100")
    assert "XXXXXXXXX" in alice_biz[0]["trn"]
    assert alice_biz[0]["trn"].endswith("001")
    assert alice_biz[0]["role"] == "OWNER"
    assert alice_biz[0]["created_date"] != ""

    # 6. Bob should only see Beta Trading
    bob_biz = list_businesses(username="bob", db_path=temp_db)
    assert len(bob_biz) == 1
    assert bob_biz[0]["id"] == biz2_id
    assert bob_biz[0]["legal_name"] == "Beta Trading LLC"
    assert bob_biz[0]["role"] == "ACCOUNTANT"

    # 7. Charlie has access to both businesses
    charlie_biz = list_businesses(username="charlie", db_path=temp_db)
    assert len(charlie_biz) == 2
    charlie_ids = {b["id"] for b in charlie_biz}
    assert charlie_ids == {biz1_id, biz2_id}

    # 8. Non-existent user should see nothing
    unknown_biz = list_businesses(username="unknown_user", db_path=temp_db)
    assert unknown_biz == []

    # 9. Calling without username should return all businesses
    all_biz = list_businesses(username=None, db_path=temp_db)
    # Default business + biz1 + biz2 = at least 2 or 3
    all_ids = {b["id"] for b in all_biz}
    assert biz1_id in all_ids
    assert biz2_id in all_ids


def test_import_all_project_modules_and_app():
    """Verify that app.py and all modules in the project can be imported cleanly without error."""
    # List of all key project modules to import
    modules_to_test = [
        "config",
        "config.settings",
        "config.rules_loader",
        "database",
        "database.connection",
        "models",
        "models.schemas",
        "models.events",
        "onboarding",
        "onboarding.requirements",
        "guards",
        "guards.injection_filter",
        "guards.privacy",
        "guards.invoice_validator",
        "guards.transaction_classifier",
        "guards.eligibility_rules",
        "guards.deadline_engine",
        "guards.refund_guard",
        "ingest",
        "ingest.file_storage",
        "ingest.excel_parser",
        "ingest.pdf_parser",
        "ingest.ocr_engine",
        "ingest.extractor",
        "tools",
        "tools.read_tools",
        "tools.compute_tools",
        "tools.write_gateway",
        "tools.package_exporter",
        "agent",
        "agent.state",
        "agent.recovery_planner",
        "agent.evaluator",
        "agent.graph",
        "agent.chat_agent",
        "agent.visual_panel",
        "llm_client",
        "cli",
        "app",
    ]

    for mod_name in modules_to_test:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"Failed to import {mod_name}"

    # Also dynamically discover all top-level python files and subpackages
    root_dir = Path(__file__).parent.parent
    for py_file in root_dir.glob("*/*.py"):
        rel_parts = py_file.relative_to(root_dir).with_suffix("").parts
        if rel_parts[0] in ("tests", ".venv", "venv", "__pycache__", "data", "uploads"):
            continue
        dotted = ".".join(rel_parts)
        imported = importlib.import_module(dotted)
        assert imported is not None
