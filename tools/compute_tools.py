import re
"""Pure Python deterministic computation, ledger reconciliation, and transaction credit tracking."""
from database.request_cache import request_cached
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional
from database.connection import get_db_connection, get_or_create_default_business
from guards.deadline_engine import calculate_credit_expiry


AUTO_DUP_PREFIX = "Also in "


def _norm_inv_no(v: Any) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    return re.sub(r"^(INVOICE|INV|NO)+", "", s) or s


def dedupe_invoices(business_id: int, db_path: Optional[Path] = None) -> int:
    """Mark the same invoice found in several files as DUPLICATE (kept once). Returns how many are duplicates.

    Same invoice = same direction, invoice number (ignoring spaces/dashes/'INV'), and the same VAT
    amount (within AED 1) and, when both have one, the same TRN. The copy from a tax-invoice file is
    kept in preference to a register line. Re-run safely: earlier automatic marks are reset first.
    """
    conn = get_db_connection(db_path=db_path, readonly=False)
    marked = 0
    try:
        cur = conn.cursor()
        with conn:
            cur.execute("UPDATE invoices SET eligibility_status = 'VALID', eligibility_reason = NULL "
                        "WHERE business_id = ? AND eligibility_status = 'DUPLICATE' AND eligibility_reason LIKE ?;",
                        (business_id, AUTO_DUP_PREFIX + "%"))
            cur.execute(
                "SELECT i.id, i.direction, i.invoice_number, i.supplier_trn, i.vat_amount, i.eligibility_status, "
                "d.original_filename AS fname, d.document_type AS dtype FROM invoices i "
                "LEFT JOIN uploaded_documents d ON d.id = i.document_id "
                "WHERE i.business_id = ? AND i.eligibility_status != 'DUPLICATE' ORDER BY i.id;",
                (business_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            groups: Dict[tuple, List[Dict[str, Any]]] = {}
            for r in rows:
                key = (r["direction"], _norm_inv_no(r["invoice_number"]))
                if not key[1] or key[1].startswith("NONUMBER"):
                    continue
                groups.setdefault(key, []).append(r)
            for _key, items in groups.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda r: (r["dtype"] != "invoice", r["eligibility_status"] != "VALID", r["id"]))
                keep = items[0]
                for r in items[1:]:
                    same_vat = abs(float(r["vat_amount"] or 0) - float(keep["vat_amount"] or 0)) <= 1.0
                    # for sales the TRN column may hold your TRN (invoice) or the customer's (register): ignore it
                    same_trn = (r["direction"] == "sales" or not (r["supplier_trn"] and keep["supplier_trn"])
                                or r["supplier_trn"] == keep["supplier_trn"])
                    if same_vat and same_trn:
                        cur.execute("UPDATE invoices SET eligibility_status = 'DUPLICATE', eligibility_reason = ? WHERE id = ?;",
                                    (f"{AUTO_DUP_PREFIX}{keep['fname'] or 'another file'} (counted once)", r["id"]))
                        marked += 1
    finally:
        conn.close()
    return marked


def rebuild_credit_ledger(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
    current_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Recompute credit ledger per tax period using verified inputs and declared outputs for a business.
    
    If business makes exempt supplies, applies partial recovery apportionment %.
    Cross-checks with filed Form 201 VAT returns and reports variance.
    Updates the credit_ledger SQLite table atomically.
    """
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    # The same invoice uploaded twice (PDF + Excel register, or two registers) must count once.
    try:
        dedupe_invoices(biz_id, db_path=db_path)
    except Exception:
        pass
    # Match General Ledger VAT lines to their tax invoices before computing.
    try:
        from ingest.returns_ledger_parser import link_ledger_to_invoices
        link_ledger_to_invoices(biz_id, db_path=db_path)
    except Exception:
        pass
    conn = get_db_connection(db_path=db_path, readonly=False)
    now = current_date or date.today()

    try:
        cursor = conn.cursor()

        # Check apportionment percentage from business profile
        cursor.execute(
            "SELECT makes_exempt_supplies, apportionment_percentage FROM business_profile WHERE business_id = ? LIMIT 1;",
            (biz_id,),
        )
        prof = cursor.fetchone()
        apportionment_ratio = 1.0
        if prof and prof["makes_exempt_supplies"]:
            apportionment_ratio = float(prof["apportionment_percentage"]) / 100.0

        # Fetch all filed VAT Returns for this business
        cursor.execute("SELECT * FROM vat_returns WHERE business_id = ? ORDER BY period_end ASC;", (biz_id,))
        returns = cursor.fetchall()

        updated_rows: List[Dict[str, Any]] = []

        with conn:
            # Periods whose VAT return was deleted or excluded from the analysis no longer count.
            live_periods = [r["period"] for r in returns]
            if live_periods:
                ph = ",".join("?" for _ in live_periods)
                cursor.execute(
                    f"DELETE FROM credit_ledger WHERE business_id = ? AND status != 'CLAIMED' AND period_code NOT IN ({ph});",
                    [biz_id] + live_periods,
                )
            else:
                cursor.execute("DELETE FROM credit_ledger WHERE business_id = ? AND status != 'CLAIMED';", (biz_id,))
            for ret in returns:
                period_code = ret["period"]
                period_start = ret["period_start"]
                period_end = ret["period_end"]
                output_vat = float(ret["output_tax"])
                declared_input_vat = float(ret["input_tax"])

                # Sum ONLY purchase invoices that are VALID and within this period for this business
                # Accounts for credit notes reducing input tax and reverse-charge/import supplies
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(
                        CASE 
                            WHEN description_lines LIKE '%credit note%' OR description_lines LIKE '%credit-note%' OR description_lines LIKE '%cr note%' OR taxable_amount < 0
                            THEN -ABS(vat_amount)
                            ELSE vat_amount
                        END
                    ), 0.0) as verified_input
                    FROM invoices
                    WHERE business_id = ?
                      AND direction = 'purchase'
                      AND eligibility_status = 'VALID'
                      AND date >= ? AND date <= ?;
                    """,
                    (biz_id, period_start, period_end),
                )
                raw_verified_input = round(float(cursor.fetchone()["verified_input"]), 2)
                # Apply partial recovery apportionment if applicable
                verified_input = round(raw_verified_input * apportionment_ratio, 2)
                # A refund can only cover input VAT that was declared on the filed return:
                # extra evidence (e.g. import VAT not put on the return) needs a voluntary disclosure first.
                verified_input = min(verified_input, round(declared_input_vat, 2))

                # Net excess recoverable VAT credit (only when verified_input > output_vat)
                net_position = round(verified_input - output_vat, 2)
                net_excess = net_position if net_position > 0 else 0.0

                # Compute statutory expiry
                period_end_d = date.fromisoformat(period_end)
                deadline = calculate_credit_expiry(
                    period_code=period_code,
                    period_end_date=period_end_d,
                    current_date=now,
                )

                # Upsert into credit_ledger
                cursor.execute(
                    """
                    INSERT INTO credit_ledger (
                        business_id, period_code, period_end_date, output_tax_aed,
                        verified_input_tax_aed, net_excess_aed, expiry_date,
                        transitional_deadline_applicable, days_remaining, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(business_id, period_code) DO UPDATE SET
                        output_tax_aed = excluded.output_tax_aed,
                        verified_input_tax_aed = excluded.verified_input_tax_aed,
                        net_excess_aed = excluded.net_excess_aed,
                        expiry_date = excluded.expiry_date,
                        transitional_deadline_applicable = excluded.transitional_deadline_applicable,
                        days_remaining = excluded.days_remaining,
                        status = excluded.status,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (
                        biz_id,
                        period_code,
                        period_end,
                        output_vat,
                        verified_input,
                        net_excess,
                        deadline.effective_expiry_date.isoformat(),
                        1 if deadline.is_transitional else 0,
                        deadline.days_remaining,
                        deadline.status,
                    ),
                )

                row_dict = {
                    "business_id": biz_id,
                    "period_code": period_code,
                    "period_end_date": period_end,
                    "output_vat": output_vat,
                    "declared_input_vat": declared_input_vat,
                    "verified_input_vat": verified_input,
                    "apportionment_applied": apportionment_ratio < 1.0,
                    "net_excess": net_excess,
                    "expiry_date": deadline.effective_expiry_date.isoformat(),
                    "days_remaining": deadline.days_remaining,
                    "status": deadline.status,
                    "discrepancy_with_return": round(declared_input_vat - verified_input, 2),
                }
                updated_rows.append(row_dict)

        return updated_rows
    finally:
        conn.close()


def allocate_credit(
    credit_ledger_entry_id: int,
    allocation_type: str,
    allocated_amount: float,
    allocation_ref: str,
    business_id: Optional[int] = None,
    target_period_code: Optional[str] = None,
    refund_record_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Allocate a portion or all of a transaction credit to carry-forward or refund.
    
    Guarantees Anti-Double Counting:
    1. Allocation_ref is globally unique in SQLite.
    2. Allocated amount cannot exceed remaining_balance.
    3. Transactionally reduces remaining_balance and updates status.
    """
    if allocated_amount <= 0:
        raise ValueError(f"Allocated amount must be positive. Received: {allocated_amount}")

    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, remaining_balance, eligible_recoverable_vat, credit_used, refunds_requested
                FROM credit_ledger_entries
                WHERE id = ? AND business_id = ?;
                """,
                (credit_ledger_entry_id, biz_id),
            )
            entry = cursor.fetchone()
            if not entry:
                raise ValueError(f"Credit entry #{credit_ledger_entry_id} not found for business #{biz_id}.")

            rem_balance = round(float(entry["remaining_balance"]), 2)
            alloc_amt = round(float(allocated_amount), 2)

            if alloc_amt > rem_balance:
                raise ValueError(
                    f"Double counting prevented: Cannot allocate {alloc_amt} AED from credit #{credit_ledger_entry_id}; "
                    f"only {rem_balance} AED remaining balance is available."
                )

            # Insert allocation (Enforces UNIQUE allocation_ref constraint)
            cursor.execute(
                """
                INSERT INTO credit_allocations (
                    business_id, credit_ledger_entry_id, allocation_type,
                    allocated_amount, target_period_code, refund_record_id, allocation_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    biz_id,
                    credit_ledger_entry_id,
                    allocation_type,
                    alloc_amt,
                    target_period_code,
                    refund_record_id,
                    allocation_ref,
                ),
            )

            new_balance = round(rem_balance - alloc_amt, 2)
            new_status = "USED" if new_balance == 0.0 else "ACTIVE"

            cursor.execute(
                """
                UPDATE credit_ledger_entries
                SET remaining_balance = ?,
                    credit_used = credit_used + ?,
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (new_balance, alloc_amt, new_status, credit_ledger_entry_id),
            )

            return {
                "allocation_ref": allocation_ref,
                "credit_ledger_entry_id": credit_ledger_entry_id,
                "allocated_amount": alloc_amt,
                "remaining_balance": new_balance,
                "status": new_status,
            }
    finally:
        conn.close()


def reconcile_invoices_with_ledger(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile purchase invoices against General Ledger entries for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()

        # Find total GL debit entries on VAT accounts
        cursor.execute("SELECT COALESCE(SUM(debit_aed), 0.0) as gl_vat_total FROM ledger_entries WHERE business_id = ?;", (biz_id,))
        gl_total = round(float(cursor.fetchone()["gl_vat_total"]), 2)

        # Find total verified invoices
        cursor.execute(
            """
            SELECT COALESCE(SUM(vat_amount), 0.0) as inv_vat_total 
            FROM invoices WHERE business_id = ? AND direction = 'purchase' AND eligibility_status = 'VALID';
            """,
            (biz_id,),
        )
        inv_total = round(float(cursor.fetchone()["inv_vat_total"]), 2)

        diff = round(gl_total - inv_total, 2)
        is_reconciled = abs(diff) <= 0.05

        return {
            "business_id": biz_id,
            "gl_vat_debit_total_aed": gl_total,
            "invoice_vat_verified_total_aed": inv_total,
            "reconciliation_variance_aed": diff,
            "is_reconciled": is_reconciled,
        }
    finally:
        conn.close()


def detect_missing_documents(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Detect discrepancies and missing records for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()

        # 1. Unreviewed invoices
        cursor.execute(
            """
            SELECT id, invoice_number, supplier_name, vat_amount, eligibility_status, eligibility_reason
            FROM invoices WHERE business_id = ? AND (eligibility_status = 'NEEDS_REVIEW' OR (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') NOT LIKE 'Also in %'));
            """,
            (biz_id,),
        )
        unreviewed = [dict(r) for r in cursor.fetchall()]

        # 2. Open document requests
        cursor.execute("SELECT * FROM document_requests WHERE business_id = ? AND status = 'OPEN';", (biz_id,))
        open_requests = [dict(r) for r in cursor.fetchall()]

        # 3. GL entries unreconciled
        cursor.execute("SELECT * FROM ledger_entries WHERE business_id = ? AND reconciled_invoice_id IS NULL AND debit_aed > 0;", (biz_id,))
        unreconciled_gl = [dict(r) for r in cursor.fetchall()]

        return {
            "business_id": biz_id,
            "unreviewed_invoices": unreviewed,
            "unreviewed_invoices_count": len(unreviewed),
            "open_document_requests": open_requests,
            "unreconciled_gl_entries": unreconciled_gl,
        }
    finally:
        conn.close()


def recalculate_credit_ledger_from_scratch(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
    current_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Recalculate credit ledger completely from raw invoices and transactions,
    then verify whether the results match the stored credit_ledger table.
    
    Guarantees deterministic Python mathematical reproduction.
    Returns diagnostic breakdown with 'matches_stored' boolean.
    """
    conn = get_db_connection(db_path=db_path, readonly=True)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    now = current_date or date.today()

    try:
        cursor = conn.cursor()

        # Check apportionment percentage
        cursor.execute(
            "SELECT makes_exempt_supplies, apportionment_percentage FROM business_profile WHERE business_id = ? LIMIT 1;",
            (biz_id,),
        )
        prof = cursor.fetchone()
        apportionment_ratio = 1.0
        if prof and prof["makes_exempt_supplies"]:
            apportionment_ratio = float(prof["apportionment_percentage"]) / 100.0

        # Discover all periods from vat_returns
        cursor.execute("SELECT * FROM vat_returns WHERE business_id = ? ORDER BY period_end ASC;", (biz_id,))
        returns = cursor.fetchall()

        period_results: List[Dict[str, Any]] = []
        all_match = True
        total_calculated_excess = 0.0
        total_stored_excess = 0.0

        for ret in returns:
            period_code = ret["period"]
            period_start = ret["period_start"]
            period_end = ret["period_end"]
            declared_output_vat = float(ret["output_tax"])
            declared_input_vat = float(ret["input_tax"])

            # 1. Calculate input tax from scratch (strictly VALID invoices, credit notes reduce input)
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE 
                        WHEN description_lines LIKE '%credit note%' OR description_lines LIKE '%credit-note%' OR description_lines LIKE '%cr note%' OR taxable_amount < 0
                        THEN -ABS(vat_amount)
                        ELSE vat_amount
                    END
                ), 0.0) as raw_input
                FROM invoices
                WHERE business_id = ?
                  AND direction = 'purchase'
                  AND eligibility_status = 'VALID'
                  AND date >= ? AND date <= ?;
                """,
                (biz_id, period_start, period_end),
            )
            raw_input = round(float(cursor.fetchone()["raw_input"]), 2)
            calc_verified_input = min(round(raw_input * apportionment_ratio, 2), round(declared_input_vat, 2))

            # 2. Output tax from return or sales invoices
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE 
                        WHEN description_lines LIKE '%credit note%' OR description_lines LIKE '%credit-note%' OR description_lines LIKE '%cr note%' OR taxable_amount < 0
                        THEN -ABS(vat_amount)
                        ELSE vat_amount
                    END
                ), 0.0) as sales_vat
                FROM invoices
                WHERE business_id = ?
                  AND direction = 'sales'
                  AND date >= ? AND date <= ?;
                """,
                (biz_id, period_start, period_end),
            )
            sales_vat = round(float(cursor.fetchone()["sales_vat"]), 2)
            calc_output_vat = declared_output_vat if declared_output_vat > 0 else sales_vat

            # 3. Calculated Net Excess Credit
            calc_net_excess = round(max(0.0, calc_verified_input - calc_output_vat), 2)
            total_calculated_excess += calc_net_excess

            # 4. Check against stored credit_ledger entry
            cursor.execute(
                "SELECT * FROM credit_ledger WHERE business_id = ? AND period_code = ?;",
                (biz_id, period_code),
            )
            stored = cursor.fetchone()
            stored_net_excess = round(float(stored["net_excess_aed"]), 2) if stored else 0.0
            total_stored_excess += stored_net_excess

            diff = round(abs(calc_net_excess - stored_net_excess), 2)
            period_matches = (diff <= 0.05) if stored else False
            if not period_matches:
                all_match = False

            period_end_d = date.fromisoformat(period_end)
            deadline = calculate_credit_expiry(period_code, period_end_d, now)

            period_results.append({
                "period_code": period_code,
                "calc_verified_input_aed": calc_verified_input,
                "calc_output_aed": calc_output_vat,
                "calc_net_excess_aed": calc_net_excess,
                "stored_net_excess_aed": stored_net_excess,
                "matches_stored": period_matches,
                "expiry_date": deadline.effective_expiry_date.isoformat(),
                "status": deadline.status,
            })

        return {
            "business_id": biz_id,
            "matches_stored": all_match and len(period_results) > 0,
            "periods": period_results,
            "total_calculated_excess_aed": round(total_calculated_excess, 2),
            "total_stored_excess_aed": round(total_stored_excess, 2),
            "apportionment_ratio_applied": apportionment_ratio,
        }
    finally:
        conn.close()


@request_cached
def reconcile_4_way(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform comprehensive 4-way reconciliation across:
    1. Invoice registers (invoices table)
    2. General Ledger VAT accounts (ledger_entries table)
    3. Filed VAT returns (vat_returns table)
    4. Credit ledger (credit_ledger table)

    Discrepancies are categorized with specific statutory causes:
    - missing_invoice: entry in GL/return without corresponding tax invoice
    - duplicate: duplicate invoice or GL voucher
    - classification_difference: rate/eligibility mismatch (e.g. entertainment claimed)
    - timing_difference: transaction date falls outside the filing period
    - unrecorded_credit_note: credit note missing from either registers or GL
    """
    conn = get_db_connection(db_path=db_path, readonly=True)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    discrepancies: List[Dict[str, Any]] = []

    try:
        cursor = conn.cursor()

        # ------------------------------------------------------------------
        # 1. DUPLICATE CHECK (Invoices & GL entries)
        # ------------------------------------------------------------------
        cursor.execute(
            """
            SELECT id, invoice_number, supplier_name, vat_amount, date
            FROM invoices
            WHERE business_id = ? AND eligibility_status = 'DUPLICATE';
            """,
            (biz_id,),
        )
        for dup in cursor.fetchall():
            discrepancies.append({
                "cause": "duplicate",
                "source_pair": "Invoices Register",
                "reference": dup["invoice_number"],
                "period_code": None,
                "amount_aed": float(dup["vat_amount"]),
                "description": f"Duplicate invoice detected: {dup['invoice_number']} from {dup['supplier_name']} dated {dup['date']}",
            })

        # ------------------------------------------------------------------
        # 2. MISSING INVOICE CHECK (GL vs Invoices & Returns vs Invoices)
        # ------------------------------------------------------------------
        # GL debit entries with no linked invoice
        cursor.execute(
            """
            SELECT id, entry_date, account_code, description, debit_aed
            FROM ledger_entries
            WHERE business_id = ? AND debit_aed > 0 AND reconciled_invoice_id IS NULL;
            """,
            (biz_id,),
        )
        for gl_entry in cursor.fetchall():
            discrepancies.append({
                "cause": "missing_invoice",
                "source_pair": "GL <-> Invoices Register",
                "reference": gl_entry["account_code"],
                "period_code": None,
                "amount_aed": float(gl_entry["debit_aed"]),
                "description": f"General Ledger VAT debit of {gl_entry['debit_aed']} AED ('{gl_entry['description']}') has no matching tax invoice",
            })

        # Returns claiming more input tax than verified in registers
        cursor.execute("SELECT * FROM vat_returns WHERE business_id = ?;", (biz_id,))
        returns = cursor.fetchall()
        for ret in returns:
            p_start, p_end = ret["period_start"], ret["period_end"]
            cursor.execute(
                """
                SELECT COALESCE(SUM(vat_amount), 0.0) as verified_tax
                FROM invoices
                WHERE business_id = ? AND direction = 'purchase'
                  AND eligibility_status = 'VALID'
                  AND date >= ? AND date <= ?;
                """,
                (biz_id, p_start, p_end),
            )
            v_tax = round(float(cursor.fetchone()["verified_tax"]), 2)
            ret_input = round(float(ret["input_tax"]), 2)
            if ret_input > v_tax + 0.05:
                diff = round(ret_input - v_tax, 2)
                discrepancies.append({
                    "cause": "missing_invoice",
                    "source_pair": "VAT Return <-> Invoices Register",
                    "reference": ret["period"],
                    "period_code": ret["period"],
                    "amount_aed": diff,
                    "description": f"Form 201 return claims {ret_input} AED input tax, exceeding verified register by {diff} AED (missing supporting invoices)",
                })

        # ------------------------------------------------------------------
        # 3. CLASSIFICATION DIFFERENCE CHECK
        # ------------------------------------------------------------------
        cursor.execute(
            """
            SELECT id, invoice_number, description_lines, vat_amount, eligibility_status, eligibility_reason
            FROM invoices
            WHERE business_id = ? AND eligibility_status IN ('INELIGIBLE', 'NEEDS_REVIEW');
            """,
            (biz_id,),
        )
        for blocked in cursor.fetchall():
            discrepancies.append({
                "cause": "classification_difference",
                "source_pair": "Invoices Register <-> Eligibility Rules",
                "reference": blocked["invoice_number"],
                "period_code": None,
                "amount_aed": float(blocked["vat_amount"]),
                "description": f"Invoice {blocked['invoice_number']} ({blocked['description_lines']}): {blocked['eligibility_reason']}",
            })

        # ------------------------------------------------------------------
        # 4. TIMING DIFFERENCE CHECK
        # ------------------------------------------------------------------
        # Invoices dated outside any filed tax return period
        cursor.execute(
            """
            SELECT id, invoice_number, date, vat_amount
            FROM invoices
            WHERE business_id = ?
              AND date NOT IN (
                  SELECT inv.date FROM invoices inv
                  JOIN vat_returns vr ON inv.business_id = vr.business_id
                  WHERE inv.date >= vr.period_start AND inv.date <= vr.period_end
              );
            """,
            (biz_id,),
        )
        for timing in cursor.fetchall():
            discrepancies.append({
                "cause": "timing_difference",
                "source_pair": "Invoices Register <-> Tax Periods",
                "reference": timing["invoice_number"],
                "period_code": None,
                "amount_aed": float(timing["vat_amount"]),
                "description": f"Invoice {timing['invoice_number']} dated {timing['date']} falls outside all filed tax periods (timing mismatch)",
            })

        # ------------------------------------------------------------------
        # 5. UNRECORDED CREDIT NOTE CHECK
        # ------------------------------------------------------------------
        cursor.execute(
            """
            SELECT id, entry_date, account_code, description, credit_aed
            FROM ledger_entries
            WHERE business_id = ? AND credit_aed > 0
              AND (description LIKE '%credit note%' OR description LIKE '%cr note%' OR description LIKE '%cn-%');
            """,
            (biz_id,),
        )
        for gl_cn in cursor.fetchall():
            # Check if matching credit note exists in invoices
            cursor.execute(
                """
                SELECT COUNT(*) as cnt FROM invoices
                WHERE business_id = ?
                  AND (description_lines LIKE '%credit note%' OR description_lines LIKE '%cr note%' OR taxable_amount < 0);
                """,
                (biz_id,),
            )
            if cursor.fetchone()["cnt"] == 0:
                discrepancies.append({
                    "cause": "unrecorded_credit_note",
                    "source_pair": "GL <-> Invoices Register",
                    "reference": gl_cn["account_code"],
                    "period_code": None,
                    "amount_aed": float(gl_cn["credit_aed"]),
                    "description": f"General Ledger records VAT credit '{gl_cn['description']}' ({gl_cn['credit_aed']} AED), but no corresponding credit note in purchase register",
                })

        # ------------------------------------------------------------------
        # TOTALS COMPUTATION
        # ------------------------------------------------------------------
        cursor.execute("SELECT COALESCE(SUM(vat_amount), 0.0) as s FROM invoices WHERE business_id = ? AND direction = 'purchase';", (biz_id,))
        inv_purch = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(vat_amount), 0.0) as s FROM invoices WHERE business_id = ? AND direction = 'sales';", (biz_id,))
        inv_sales = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(debit_aed), 0.0) as s FROM ledger_entries WHERE business_id = ?;", (biz_id,))
        gl_input = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(credit_aed), 0.0) as s FROM ledger_entries WHERE business_id = ?;", (biz_id,))
        gl_output = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(input_tax), 0.0) as s FROM vat_returns WHERE business_id = ?;", (biz_id,))
        ret_input_tot = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(output_tax), 0.0) as s FROM vat_returns WHERE business_id = ?;", (biz_id,))
        ret_output_tot = round(float(cursor.fetchone()["s"]), 2)

        cursor.execute("SELECT COALESCE(SUM(net_excess_aed), 0.0) as s FROM credit_ledger WHERE business_id = ?;", (biz_id,))
        credit_excess_tot = round(float(cursor.fetchone()["s"]), 2)

        counts: Dict[str, int] = {
            "missing_invoice": sum(1 for d in discrepancies if d["cause"] == "missing_invoice"),
            "duplicate": sum(1 for d in discrepancies if d["cause"] == "duplicate"),
            "classification_difference": sum(1 for d in discrepancies if d["cause"] == "classification_difference"),
            "timing_difference": sum(1 for d in discrepancies if d["cause"] == "timing_difference"),
            "unrecorded_credit_note": sum(1 for d in discrepancies if d["cause"] == "unrecorded_credit_note"),
        }

        return {
            "business_id": biz_id,
            "reconciled": len(discrepancies) == 0,
            "discrepancies_count": len(discrepancies),
            "discrepancy_counts_by_cause": counts,
            "discrepancies": discrepancies,
            "totals": {
                "invoices_purchase_vat_aed": inv_purch,
                "invoices_sales_vat_aed": inv_sales,
                "gl_input_vat_debit_aed": gl_input,
                "gl_output_vat_credit_aed": gl_output,
                "returns_input_vat_aed": ret_input_tot,
                "returns_output_tax_aed": ret_output_tot,
                "credit_ledger_excess_aed": credit_excess_tot,
            },
        }
    finally:
        conn.close()


def compute_refund_claim(
    business_id: int,
    requested_periods: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Code-calculated refund claim: which periods can be claimed and for how much.

    A period is included when it has a positive excess credit and is not EXPIRED
    or already CLAIMED. If periods are requested, only those valid ones are used
    (falling back to all valid periods when none of the requested ones qualify).
    The amount is always the sum of the ledger's net excess, never an AI figure.
    """
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT period_code, net_excess_aed, status, expiry_date, days_remaining FROM credit_ledger "
            "WHERE business_id = ? ORDER BY period_end_date ASC;",
            (business_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) AS c FROM invoices WHERE business_id = ? AND direction = 'purchase' "
            "AND (eligibility_status = 'NEEDS_REVIEW' OR (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') NOT LIKE 'Also in %'));",
            (business_id,),
        )
        unresolved = int(cur.fetchone()["c"])
    finally:
        conn.close()

    claimable = [r for r in rows if float(r["net_excess_aed"] or 0) > 0 and r["status"] not in ("EXPIRED", "CLAIMED")]
    excluded = [
        {"period_code": r["period_code"],
         "reason": "expired" if r["status"] == "EXPIRED" else ("already claimed" if r["status"] == "CLAIMED" else "no excess credit")}
        for r in rows if r not in claimable
    ]
    if requested_periods:
        wanted = [r for r in claimable if r["period_code"] in set(requested_periods)]
        if wanted:
            claimable = wanted
    return {
        "period_codes": [r["period_code"] for r in claimable],
        "claimed_amount": round(sum(float(r["net_excess_aed"]) for r in claimable), 2),
        "periods": claimable,
        "excluded": excluded,
        "unresolved_invoices": unresolved,
    }


def _num_str(v: float) -> str:
    """9400.0 -> '9400', 12.5 -> '12.5' (how numbers usually appear in CSV/Excel cells)."""
    return str(int(v)) if float(v).is_integer() else f"{v}"


def document_problems(business_id: int, db_path: Optional[Path] = None) -> Dict[int, Dict[str, Any]]:
    """Code-found problems grouped by the uploaded document they come from.

    Returns {document_id: {"file_name", "problems": [str, ...]}} for documents with at
    least one problem. Only documents used in the analysis are checked.
    """
    import re as _re
    conn = get_db_connection(db_path=db_path, readonly=True)
    out: Dict[int, Dict[str, Any]] = {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, original_filename, document_type, COALESCE(included_in_analysis, 1) AS inc, "
            "extracted_text FROM uploaded_documents WHERE business_id = ?;",
            (business_id,),
        )
        docs = {int(r["id"]): dict(r) for r in cur.fetchall()}

        def add(doc_id: Any, text: str, find: Optional[List[str]] = None) -> None:
            """Record a problem; `find` = strings that locate it inside the file (row / text to box in red)."""
            if doc_id is None or int(doc_id) not in docs:
                return
            entry = out.setdefault(int(doc_id), {"file_name": docs[int(doc_id)]["original_filename"],
                                                 "document_type": docs[int(doc_id)]["document_type"],
                                                 "problems": [], "items": []})
            if text not in entry["problems"]:
                entry["problems"].append(text)
                entry["items"].append({"message": text, "find": [f for f in (find or []) if f]})

        cur.execute("SELECT period, period_start, period_end, input_tax, document_id FROM vat_returns WHERE business_id = ?;",
                    (business_id,))
        returns = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT id, document_id, invoice_number, supplier_name, supplier_trn, date, taxable_amount, vat_amount, total, "
            "eligibility_status, eligibility_reason FROM invoices WHERE business_id = ? AND direction = 'purchase' "
            "AND NOT (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') LIKE 'Also in %');",
            (business_id,),
        )
        invoices = [dict(r) for r in cur.fetchall()]
        seen: Dict[tuple, Any] = {}
        for inv in invoices:
            num = inv["invoice_number"]
            if inv["eligibility_status"] in ("NEEDS_REVIEW", "INELIGIBLE"):
                add(inv["document_id"], f"Invoice {num}: {inv['eligibility_reason'] or inv['eligibility_status'].lower().replace('_', ' ')}", [num])
            trn = str(inv["supplier_trn"] or "")
            is_customs = "customs" in str(inv.get("supplier_name") or "").lower()
            if not is_customs and not _re.fullmatch(r"100\d{12}", trn):
                add(inv["document_id"], f"Invoice {num}: supplier TRN missing or not 15 digits", [num, trn])
            tax = abs(float(inv["taxable_amount"] or 0)); vat = abs(float(inv["vat_amount"] or 0))
            if tax > 0 and abs(round(tax * 0.05, 2) - vat) > 0.05:
                add(inv["document_id"], f"Invoice {num}: VAT AED {vat:,.2f} is not 5% of AED {tax:,.2f}", [num, f"{vat:,.2f}", _num_str(vat)])
            tot = abs(float(inv.get("total") or 0))
            if tot > 0 and tax > 0 and abs(round(tax + vat, 2) - tot) > 0.05:
                add(inv["document_id"], f"Invoice {num}: total AED {tot:,.2f} is not net AED {tax:,.2f} + VAT AED {vat:,.2f}",
                    [num, f"{tot:,.2f}", _num_str(tot)])
            _d = str(inv["date"] or "")[:10]
            if _d and _d > date.today().isoformat():
                add(inv["document_id"], f"Invoice {num} is dated in the future ({_d})", [num, _d])
            key = (trn, str(num))
            if key in seen:
                add(inv["document_id"], f"Invoice {num}: duplicate of an invoice already uploaded", [num])
                add(seen[key], f"Invoice {num}: appears twice", [num])
            seen[key] = inv["document_id"]
            d = str(inv["date"] or "")[:10]
            if returns and d and not any(r["period_start"] <= d <= r["period_end"] for r in returns):
                add(inv["document_id"], f"Invoice {num} dated {d} is outside every filed VAT return period", [num, d])

        for r in returns:
            verified = sum(float(i["vat_amount"] or 0) for i in invoices
                           if i["eligibility_status"] == "VALID" and r["period_start"] <= str(i["date"])[:10] <= r["period_end"])
            gap = round(float(r["input_tax"] or 0) - verified, 2)
            if gap > 0.05:
                _it = float(r["input_tax"] or 0)
                add(r["document_id"], f"{r['period']}: return claims AED {gap:,.2f} more input VAT than the invoices support",
                    [r["period"], f"{_it:,.2f}", _num_str(_it)])

        cur.execute(
            "SELECT document_id, entry_date, account_code, account_name, description, debit_aed, credit_aed, "
            "reconciled_invoice_id FROM ledger_entries WHERE business_id = ?;",
            (business_id,),
        )
        ledger_rows = [dict(r) for r in cur.fetchall()]

        # Ledger vs VAT return: input VAT booked in the ledger for each period must match the return.
        def _is_input_vat(l: Dict[str, Any]) -> bool:
            name = f"{l.get('account_name') or ''} {l.get('description') or ''}".lower()
            return ("input" in name and "output" not in name) or str(l.get("account_code") or "").strip() in ("2210", "1410", "1420")
        for r in returns:
            in_period = [l for l in ledger_rows if _is_input_vat(l)
                         and r["period_start"] <= str(l["entry_date"] or "")[:10] <= r["period_end"]]
            if not in_period:
                continue
            booked = round(sum(float(l["debit_aed"] or 0) - float(l["credit_aed"] or 0) for l in in_period), 2)
            declared = round(float(r["input_tax"] or 0), 2)
            if abs(booked - declared) > 0.05:
                docs_in = {}
                for l in in_period:
                    docs_in[l["document_id"]] = docs_in.get(l["document_id"], 0) + 1
                ledger_doc = max(docs_in, key=docs_in.get)
                diff = round(declared - booked, 2)
                add(ledger_doc,
                    f"{r['period']}: ledger shows input VAT AED {booked:,.2f} but the VAT return declares AED {declared:,.2f} "
                    f"(difference AED {abs(diff):,.2f})")
                add(r["document_id"],
                    f"{r['period']}: declared input VAT AED {declared:,.2f} differs from the ledger (AED {booked:,.2f})",
                    [r["period"], f"{declared:,.2f}", _num_str(declared)])

        for l in ledger_rows:
            if float(l["debit_aed"] or 0) > 0 and l["reconciled_invoice_id"] is None:
                add(l["document_id"], f"Ledger line '{l['description']}' (AED {float(l['debit_aed']):,.2f}) has no matching invoice",
                    [l["description"], _num_str(float(l["debit_aed"])), f"{float(l['debit_aed']):,.2f}"])
            if float(l["credit_aed"] or 0) > 0 and "credit note" in str(l["description"]).lower():
                add(l["document_id"], f"Credit note in ledger '{l['description']}' (AED {float(l['credit_aed']):,.2f}) is not in the invoices",
                    [l["description"], _num_str(float(l["credit_aed"])), f"{float(l['credit_aed']):,.2f}"])

        for doc_id, d in docs.items():
            if d["document_type"] == "other":
                add(doc_id, "This is not a VAT document (for example a photo or notes). It is not used in your numbers; you can delete it.")
                continue
            if d["inc"] and d["document_type"] not in ("fta_guide", "evidence"):
                cur.execute(
                    "SELECT (SELECT COUNT(*) FROM invoices WHERE document_id = ?) + "
                    "(SELECT COUNT(*) FROM vat_returns WHERE document_id = ?) + "
                    "(SELECT COUNT(*) FROM ledger_entries WHERE document_id = ?) AS n;",
                    (doc_id, doc_id, doc_id),
                )
                if int(cur.fetchone()["n"]) == 0:
                    # A second copy of the same returns (e.g. PDF + Excel summary) is fine: its periods are already in.
                    txt = str(d.get("extracted_text") or "")
                    from ingest.returns_ledger_parser import _period_dates
                    import re as _r2
                    toks = _r2.findall(r"(?i)q[1-4][\s/-]*20\d{2}|20\d{2}[\s/-]*q[1-4]", txt)
                    codes = {(_period_dates(t) or ("",))[0] for t in toks}
                    have = {r["period"] for r in returns}
                    if d["document_type"] == "vat_return" and codes & have:
                        continue
                    if d["document_type"] == "invoice":
                        cur.execute("SELECT COUNT(*) AS n FROM invoices WHERE document_id = ? AND eligibility_status = 'DUPLICATE';", (doc_id,))
                        if int(cur.fetchone()["n"]):
                            continue
                    add(doc_id, "Nothing could be read from this file")
    finally:
        conn.close()
    return out



def declared_vs_proven(business_id: int, db_path=None) -> Dict[str, Any]:
    """Refund your filed returns declare vs. the part backed by valid tax invoices (per period)."""
    from database.connection import get_db_connection
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT period, output_tax, input_tax FROM vat_returns WHERE business_id = ?;", (business_id,))
        rets = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT period_code, net_excess_aed FROM credit_ledger WHERE business_id = ?;", (business_id,))
        proven = {r["period_code"]: float(r["net_excess_aed"] or 0) for r in cur.fetchall()}
    finally:
        conn.close()
    periods = []
    for r in rets:
        declared = max(float(r["input_tax"] or 0) - float(r["output_tax"] or 0), 0.0)
        if declared > 0:
            periods.append({"period": r["period"], "declared": round(declared, 2),
                            "proven": round(proven.get(r["period"], 0.0), 2)})
    tot_d = round(sum(p["declared"] for p in periods), 2)
    tot_p = round(sum(min(p["proven"], p["declared"]) for p in periods), 2)
    unproven = [p for p in periods if p["proven"] + 0.01 < p["declared"]]
    return {"declared": tot_d, "proven": tot_p, "gap": round(tot_d - tot_p, 2), "unproven_periods": unproven}
