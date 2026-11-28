-- ==============================================================================
-- Raseed SQLite Schema (Multi-Business Isolation, Strict Foreign Keys, WAL Mode)
-- NO SEED DATA: User data tables start completely empty.
-- ==============================================================================

PRAGMA foreign_keys = ON;

-- 1. Businesses (Tenant Isolation Root)
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    trn TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Users and Roles (RBAC & Authentication per business)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    username TEXT,
    email TEXT,
    full_name TEXT,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'OWNER' CHECK(role IN ('OWNER', 'ACCOUNTANT', 'APPROVER')),
    failed_login_attempts INTEGER DEFAULT 0,
    locked_until TIMESTAMP,
    is_verified INTEGER DEFAULT 1,
    totp_secret TEXT,
    is_totp_enabled INTEGER DEFAULT 0,
    backup_codes_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id, username)
);

-- 2b. Verification Codes (Email Verification & Password Reset)
CREATE TABLE IF NOT EXISTS verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('EMAIL_VERIFICATION', 'PASSWORD_RESET')),
    failed_attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_verification_codes_email_purpose ON verification_codes(email, purpose);

-- 3. Business Profile
CREATE TABLE IF NOT EXISTS business_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    legal_name TEXT NOT NULL,
    trn TEXT NOT NULL,
    vat_registration_date TEXT NOT NULL,
    tax_period_type TEXT NOT NULL CHECK(tax_period_type IN ('monthly', 'quarterly')),
    first_tax_period_start TEXT NOT NULL,
    business_activity TEXT NOT NULL,
    emirate TEXT NOT NULL,
    makes_exempt_supplies INTEGER NOT NULL DEFAULT 0,
    apportionment_percentage REAL NOT NULL DEFAULT 100.0 CHECK(apportionment_percentage >= 0.0 AND apportionment_percentage <= 100.0),
    signatory_name TEXT NOT NULL,
    signatory_email TEXT NOT NULL,
    masked_iban TEXT NOT NULL,
    local_ai_only INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id)
);

-- 4. Uploaded Documents
CREATE TABLE IF NOT EXISTS uploaded_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    file_hash TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK(document_type IN ('invoice', 'register', 'vat_return', 'ledger', 'fta_guide', 'evidence', 'other')),
    file_size_bytes INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    extracted_text TEXT,
    included_in_analysis INTEGER NOT NULL DEFAULT 1,
    reader_version INTEGER NOT NULL DEFAULT 0,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id, file_hash)
);

-- 4b. Rows set aside while their document is excluded from the analysis
CREATE TABLE IF NOT EXISTS parked_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_parked_rows_doc ON parked_rows(business_id, document_id);

-- 5. Invoices
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES uploaded_documents(id) ON DELETE SET NULL,
    direction TEXT NOT NULL DEFAULT 'purchase' CHECK(direction IN ('purchase', 'sales')),
    supplier_name TEXT NOT NULL,
    supplier_trn TEXT,
    customer_name TEXT,
    customer_trn TEXT,
    invoice_number TEXT NOT NULL,
    date TEXT NOT NULL,
    taxable_amount REAL NOT NULL CHECK(taxable_amount >= 0),
    vat_amount REAL NOT NULL CHECK(vat_amount >= 0),
    total REAL NOT NULL CHECK(total >= 0),
    currency TEXT NOT NULL DEFAULT 'AED',
    description_lines TEXT, -- JSON array of strings
    source_document TEXT,
    extraction_confidence REAL NOT NULL DEFAULT 1.0,
    eligibility_status TEXT NOT NULL DEFAULT 'VALID' CHECK(eligibility_status IN ('VALID', 'INELIGIBLE', 'NEEDS_REVIEW', 'DUPLICATE')),
    eligibility_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Transactions (Detailed classification & apportionment)
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN (
        'standard_rated', 'zero_rated', 'exempt', 'import',
        'reverse_charge', 'credit_note_purchase', 'credit_note_sales',
        'out_of_scope', 'special_review'
    )),
    tax_period TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    taxable_amount REAL NOT NULL,
    vat_rate REAL NOT NULL DEFAULT 0.05,
    vat_amount REAL NOT NULL,
    is_recoverable INTEGER NOT NULL DEFAULT 1,
    recovery_percentage REAL NOT NULL DEFAULT 100.0 CHECK(recovery_percentage >= 0.0 AND recovery_percentage <= 100.0),
    recoverable_vat_amount REAL NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 7. Transaction-Level Credit Ledger Entries
CREATE TABLE IF NOT EXISTS credit_ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    original_tax_period TEXT NOT NULL,
    source_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    source_invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    original_input_vat REAL NOT NULL CHECK(original_input_vat >= 0),
    eligible_recoverable_vat REAL NOT NULL CHECK(eligible_recoverable_vat >= 0),
    adjustments REAL NOT NULL DEFAULT 0.0,
    credit_used REAL NOT NULL DEFAULT 0.0 CHECK(credit_used >= 0),
    refunds_requested REAL NOT NULL DEFAULT 0.0 CHECK(refunds_requested >= 0),
    refunds_received REAL NOT NULL DEFAULT 0.0 CHECK(refunds_received >= 0),
    remaining_balance REAL NOT NULL CHECK(remaining_balance >= 0),
    expiry_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'USED', 'REFUND_REQUESTED', 'REFUNDED', 'EXPIRED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 8. General Ledger Entries
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES uploaded_documents(id) ON DELETE SET NULL,
    entry_date TEXT NOT NULL,
    account_code TEXT NOT NULL,
    account_name TEXT NOT NULL,
    description TEXT NOT NULL,
    debit_aed REAL NOT NULL DEFAULT 0.0 CHECK(debit_aed >= 0),
    credit_aed REAL NOT NULL DEFAULT 0.0 CHECK(credit_aed >= 0),
    vat_code TEXT,
    reconciled_invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL
);

-- 9. Filed VAT Returns (Form 201)
CREATE TABLE IF NOT EXISTS vat_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES uploaded_documents(id) ON DELETE SET NULL,
    period TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    output_tax REAL NOT NULL DEFAULT 0.0 CHECK(output_tax >= 0),
    input_tax REAL NOT NULL DEFAULT 0.0 CHECK(input_tax >= 0),
    net_payable_refundable REAL NOT NULL,
    filing_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id, period)
);

-- 10. Period-Level Credit Ledger
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    period_code TEXT NOT NULL,
    period_end_date TEXT NOT NULL,
    output_tax_aed REAL NOT NULL DEFAULT 0.0,
    verified_input_tax_aed REAL NOT NULL DEFAULT 0.0,
    net_excess_aed REAL NOT NULL CHECK(net_excess_aed >= 0),
    expiry_date TEXT NOT NULL,
    transitional_deadline_applicable INTEGER NOT NULL DEFAULT 0,
    days_remaining INTEGER,
    status TEXT NOT NULL DEFAULT 'OK' CHECK(status IN ('OK', 'DUE_SOON', 'URGENT', 'EXPIRED', 'CLAIMED')),
    claimed_refund_amount_aed REAL NOT NULL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id, period_code)
);

-- 11. Refund Records
CREATE TABLE IF NOT EXISTS refund_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    period_codes TEXT NOT NULL, -- JSON array of strings
    total_refund_claimed_aed REAL NOT NULL CHECK(total_refund_claimed_aed >= 0),
    emartax_application_ref TEXT,
    submission_date TEXT,
    fta_status TEXT NOT NULL DEFAULT 'DRAFTED' CHECK(fta_status IN ('DRAFTED', 'SUBMITTED', 'IN_REVIEW', 'ADDITIONAL_INFO_REQUESTED', 'APPROVED', 'REJECTED')),
    fta_decision_date TEXT,
    amount_approved_aed REAL
);

-- 12. Credit Allocations (Anti-Double Counting Mechanism)
CREATE TABLE IF NOT EXISTS credit_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    credit_ledger_entry_id INTEGER NOT NULL REFERENCES credit_ledger_entries(id) ON DELETE CASCADE,
    allocation_type TEXT NOT NULL CHECK(allocation_type IN ('CARRY_FORWARD_USE', 'REFUND_CLAIM', 'REFUND_SETTLEMENT', 'EXPIRY_WRITE_OFF')),
    allocated_amount REAL NOT NULL CHECK(allocated_amount > 0),
    target_period_code TEXT,
    refund_record_id INTEGER REFERENCES refund_records(id) ON DELETE SET NULL,
    allocation_ref TEXT NOT NULL UNIQUE,
    allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 13. Document Requests
CREATE TABLE IF NOT EXISTS document_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    period_code TEXT NOT NULL,
    invoice_reference TEXT,
    supplier_name TEXT,
    amount_aed REAL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'FULFILLED', 'CANCELLED')),
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fulfilled_at TIMESTAMP
);

-- 14. Agent Tasks
CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'IN_PROGRESS', 'WAITING_USER', 'WAITING_APPROVAL', 'DONE', 'CANCELLED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 15. Staged Actions
CREATE TABLE IF NOT EXISTS staged_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    staged_by_user_id INTEGER REFERENCES users(id),
    action_type TEXT NOT NULL CHECK(action_type IN (
        'REQUEST_DOCUMENT_FROM_USER',
        'MARK_INVOICE_STATUS',
        'PREPARE_REFUND_PACKAGE',
        'SUBMIT_REFUND_APPLICATION',
        'SEND_EXTERNAL_EMAIL',
        'RECORD_FTA_REFERENCE'
    )),
    payload_json TEXT NOT NULL DEFAULT '{}',
    rationale TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('LOW', 'CONSEQUENTIAL')),
    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL' CHECK(status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'AUTO_EXECUTED')),
    guard_results_json TEXT NOT NULL DEFAULT '{}',
    approver_note TEXT,
    staged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP
);

-- 16. Immutable Audit Events
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER REFERENCES businesses(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor TEXT NOT NULL CHECK(actor IN ('AGENT', 'USER', 'SYSTEM')),
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    llm_provider TEXT,
    execution_time_ms REAL
);

-- 17. Scheduler Runs (Execution Log & Catch-Up State)
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER REFERENCES businesses(id) ON DELETE CASCADE,
    job_name TEXT NOT NULL,
    run_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED', 'RETRYING')),
    details_json TEXT NOT NULL DEFAULT '{}'
);

-- 18. Scheduler Autonomous Messages (Chat Stream Notifications)
CREATE TABLE IF NOT EXISTS scheduler_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    message_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    period_code TEXT,
    priority TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')),
    is_delivered INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 19. Chat Messages (Per-business persistent conversation history)
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    how_i_got_this_json TEXT,
    approval_card_json TEXT,
    staged_action_id INTEGER REFERENCES staged_actions(id) ON DELETE SET NULL,
    timestamp TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 20. Memory Notes (Owner confirmed facts & operational parameters)
CREATE TABLE IF NOT EXISTS memory_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL DEFAULT 1 REFERENCES businesses(id) ON DELETE CASCADE,
    fact TEXT NOT NULL,
    source TEXT NOT NULL,
    confirmed_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 21. Rate Limits Tracker
CREATE TABLE IF NOT EXISTS rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 22. Cached Column Mappings (Learned Column Patterns per Company)
CREATE TABLE IF NOT EXISTS cached_column_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    file_signature TEXT,
    mapping_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(business_id, file_signature)
);

-- Multi-Tenant Indexes
CREATE INDEX IF NOT EXISTS idx_invoices_biz_supp_num ON invoices(business_id, supplier_trn, invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_biz_date ON invoices(business_id, date);
CREATE INDEX IF NOT EXISTS idx_transactions_biz_period ON transactions(business_id, tax_period);
CREATE INDEX IF NOT EXISTS idx_credit_entries_biz_status ON credit_ledger_entries(business_id, status);
CREATE INDEX IF NOT EXISTS idx_credit_allocations_entry ON credit_allocations(credit_ledger_entry_id);
CREATE INDEX IF NOT EXISTS idx_vat_returns_biz_period ON vat_returns(business_id, period);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_biz_status ON credit_ledger(business_id, status);
CREATE INDEX IF NOT EXISTS idx_staged_actions_biz_status ON staged_actions(business_id, status);
CREATE INDEX IF NOT EXISTS idx_uploaded_docs_biz_hash ON uploaded_documents(business_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_scheduler_msgs_biz_delivered ON scheduler_messages(business_id, is_delivered);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_biz_job ON scheduler_runs(business_id, job_name, run_time);
CREATE INDEX IF NOT EXISTS idx_chat_messages_biz ON chat_messages(business_id, id);
CREATE INDEX IF NOT EXISTS idx_memory_notes_biz ON memory_notes(business_id, id);
CREATE INDEX IF NOT EXISTS idx_rate_limits_key_event ON rate_limits(key, event_type, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
