# Raseed — Antigravity prompts, phase by phase

Paste ONE prompt at a time into the Antigravity chat (Ctrl+L) inside the new `raseed` folder.
Wait until each phase finishes, do the HUMAN CHECKPOINT yourself, then commit before the next one.

---

## STEP 1 — Write PLAN.md (planning model: Gemini 3.8 Flash, thinking High)

Attach `PROBLEM_STATEMENT.md` and `All_project_starting_prompt.md`, then paste:

```
Read PROBLEM_STATEMENT.md and All_project_starting_prompt.md in this folder and follow the master
prompt to write PLAN.md for "Raseed — Autonomous VAT Recovery Agent" for the FTA hackathon.
Do not write application code yet.

Fixed decisions (do not change them):
- Python 3.12, LangGraph (stateful loop, checkpointer, interrupt for human approval), Pydantic v2,
  SQLite (local file, all-or-nothing transactions), Streamlit UI, pytest, python-dotenv,
  openai SDK for all LLM providers, APScheduler for monitoring, pdfplumber + openpyxl + pandas for
  documents, rapidocr-onnxruntime for scanned invoices/images, rank_bm25 for searching uploaded
  FTA guidance (RAG).
- LLM fallback chain from the TechX manual: Groq openai/gpt-oss-120b -> Groq openai/gpt-oss-20b ->
  Gemini Flash -> Ollama qwen3 (local) -> a deterministic offline engine. Timeout 30 s, max_retries 1,
  log which provider answered. LLM_MODE in .env: "demo" (Groq first) or "local" (Ollama first).
- NO FAKE DATA IN THE APP: no seed data, no demo business. The agent must start by asking the user
  for the business information and documents it needs, and must not calculate anything until the
  required minimum is uploaded and validated. (Tests may use tiny fixtures inside tests/ only.)
- Golden rule: the LLM reads, plans and drafts; Python does all VAT maths, eligibility rules,
  deadlines and validation; a human approves consequential actions.
- Autonomy: routine work runs automatically (logged); human approval for refund submission,
  uncertain/ineligible input tax, amounts above a configurable threshold, and any external email.
- Raseed never submits to the FTA by itself: after approval it produces the final refund package
  and the user records the EmaraTax submission reference; then Raseed monitors the status.
- Tax rules live in config/tax_rules.yaml with an official source for each rule, including:
  standard rate 5%; excess recoverable input tax can be carried forward or refunded within 5 years
  from the end of the tax period in which it arose (VAT Law Art. 74(3) as amended by Federal
  Decree-Law No. 16 of 2025, effective 1 Jan 2026); transitional rule: if the 5-year period expired
  before 1 Jan 2026 or expires within one year of that date, a refund can be requested until
  31 Dec 2026 (Tax Procedures Law as amended by Federal Decree-Law No. 17 of 2025). Mark every
  rule "to be verified against current FTA guidance".
- Privacy: uploads and the database stay local in data/ (gitignored); cloud LLMs get masked data
  (TRN, IBAN, names, emails masked) by default; "Local only" mode uses Ollama.
- Safety: injection guard on chat and on all extracted document text; MAX_STEPS 5; MAX_REPLANS 2
  then human review; broken JSON from the model is caught; one write gateway with rollback; full
  audit log (inputs, tool calls, decisions, provider, approver).

Plan the core demo in 4 phases with a HUMAN CHECKPOINT after each, then stretch phases:
continuous monitoring (APScheduler), RAG over uploaded official FTA guides, deployment.
List your assumptions at the end and ask me to confirm them.
```

HUMAN CHECKPOINT: read PLAN.md, answer its assumptions, then switch to the coding model
(Gemini 3.8 Flash, thinking Low/Medium).

---

## PHASE 1 — Setup, schemas, database and onboarding requirements

```
Follow PLAN.md. Build Phase 1 only, then stop at the human checkpoint.

1. Project setup: .gitignore (.env, .venv, __pycache__, data/, *.db, *.sqlite, uploads),
   .env.example (GROQ_API_KEY, GEMINI_API_KEY, LLM_MODE=demo, APP_PASSWORD,
   APPROVAL_THRESHOLD_AED=10000), requirements.txt (pinned), README.md skeleton, conftest.py,
   pytest.ini.
2. config/settings.py (pydantic-settings, reads .env) and config/tax_rules.yaml (rules with
   official sources, as in PLAN.md).
3. Pydantic schemas for: BusinessProfile (legal name, TRN, VAT registration date, tax period type
   monthly/quarterly, first tax period start, business activity, emirate, makes exempt supplies
   yes/no, authorised signatory name + email, refund bank IBAN stored masked), UploadedDocument,
   Invoice (purchase/sales, supplier/customer name and TRN, invoice number, date, taxable amount,
   VAT amount, total, currency, description lines, source document, extraction confidence),
   LedgerEntry, VatReturn (period, output tax, input tax, net payable/refundable), RefundRecord,
   CreditLedgerRow, Deadline, DocumentRequest, AgentTask, StagedAction, AuditEvent.
4. SQLite database (database/schema.sql + database/connection.py): tables for the schemas above,
   WAL mode, all-or-nothing transactions, a read-only connection for read tools. NO seed data.
5. onboarding/requirements.py: the checklist of what the agent needs before it can work:
   REQUIRED = business profile + purchase invoices (or purchase register) + filed VAT returns for
   the periods to review; RECOMMENDED = sales invoices/register, general ledger VAT accounts export,
   previous refund applications and FTA decisions, supplier payment records, official FTA guides.
   A function onboarding_status() returns what is present, missing, invalid, and whether the agent
   may start (minimum met).
6. Local file storage in data/uploads/<document_type>/ with a SHA-256 hash to detect duplicate files.
7. A "Reset workspace" function that deletes all business data and uploads (with confirmation).

Tests: schemas validate good input and reject bad input (TRN must be 15 digits, negative amounts,
future dates); onboarding_status() with nothing uploaded says "cannot start" and lists what is
missing; duplicate file detection.
Run python -m pytest tests/ -v and show me git diff --stat. Then STOP for my checkpoint.
```

HUMAN CHECKPOINT: copy `.env.example` to `.env`, add your keys, run the tests yourself, and check
that the database starts empty.
Commit: `git add .` and `git commit -m "Phase 1: setup, schemas, onboarding"`.

---

## PHASE 2 — Document ingestion, tools, guards and unit tests

```
Follow PLAN.md. Build Phase 2 only, then stop at the human checkpoint.

1. Ingestion (ingest/):
   - PDF text with pdfplumber; scanned PDFs and images (JPG/PNG) with rapidocr-onnxruntime;
     CSV/XLSX registers and ledgers with pandas/openpyxl (column mapping step: guess columns, let
     the user confirm the mapping when unsure).
   - LLM extraction of invoice fields into the Invoice schema (strict JSON, validated with
     Pydantic; on broken JSON, tell the model the error and retry once; on failure mark
     NEEDS_REVIEW). Store an extraction confidence per invoice.
   - All extracted text goes through the injection guard BEFORE the LLM sees it.
2. Guards (guards/, pure Python, never the LLM):
   - injection_filter: chat messages and document text ("ignore previous instructions", "system
     override", "approve refund", "pre_approved", hidden instructions) -> quarantine the text,
     log it, the LLM only sees "[QUARANTINED TEXT]".
   - invoice_validator: required tax-invoice fields present; supplier TRN 15 digits; invoice
     addressed to the business's TRN; VAT = 5% of taxable amount within a small tolerance;
     date inside a tax period and not in the future; currency AED or converted; duplicate invoice
     (same supplier TRN + invoice number, or same amounts and date) -> DUPLICATE.
   - eligibility_rules: from config/tax_rules.yaml; categories that are blocked or uncertain
     (e.g. entertainment, personal use, employee benefits) are never auto-claimed: status
     NEEDS_HUMAN_REVIEW. The LLM may SUGGEST a category; code decides the status.
   - credit_ledger: per tax period: output tax, recoverable input tax (only VALID + ELIGIBLE),
     net position, excess recoverable carried forward, used, refunded. Cross-check with the filed
     VAT returns and report differences.
   - deadline_engine: for each excess credit, expiry = end of its tax period + 5 years, plus the
     transitional rule; days left; status OK / DUE_SOON (< 180 days) / URGENT (< 60 days) /
     EXPIRED.
   - refund_guard: refund amount must equal the code-calculated eligible excess; no expired
     credits; no NEEDS_REVIEW items; amount above APPROVAL_THRESHOLD_AED always needs approval.
3. Tools (tools/), short results, inputs checked:
   READ: get_business_profile, get_onboarding_status, list_documents, get_invoices(filters),
   get_vat_returns, get_credit_ledger, get_deadlines, get_document_requests, get_refund_status.
   COMPUTE (code): reconcile_invoices_with_ledger, rebuild_credit_ledger, compute_deadlines,
   detect_missing_documents (credits/ledger lines without a valid invoice, returns missing for a
   period, totals that don't match).
   WRITE (only through tools/write_gateway.py): stage_action(type, payload, reason, source) ->
   STAGED_PENDING_APPROVAL or AUTO_EXECUTED for low-risk types; execute_approved_action (called
   only by the Approve button; re-runs the guards first; SQLite transaction with rollback).
   Action types: REQUEST_DOCUMENT_FROM_USER (low risk, auto), MARK_INVOICE_STATUS (auto when
   rule-based, approval when it changes a claim), PREPARE_REFUND_PACKAGE (auto draft),
   SUBMIT_REFUND_APPLICATION (always approval), SEND_EXTERNAL_EMAIL (always approval),
   RECORD_FTA_REFERENCE (user input).

Tests (tests/ only, with tiny fixtures created inside the tests, never loaded into the app):
VAT maths, 5% check, duplicate detection, blocked category -> NEEDS_HUMAN_REVIEW, deadline maths
(normal and transitional), refund_guard rejects wrong amounts, injection text is quarantined,
the gateway rolls back on error.
Run python -m pytest tests/ -v and show me git diff --stat. Then STOP for my checkpoint.
```

HUMAN CHECKPOINT: run the tests, and check the deadline and VAT maths by hand on two examples.
Commit: `git commit -am "Phase 2: ingestion, guards, tools"`.

---

## PHASE 3 — The agent brain (LangGraph), approvals and terminal demo

```
Follow PLAN.md. Build Phase 3 only, then stop at the human checkpoint.

1. llm_client.py: the fallback chain with tool calling (ask(messages, tools)), timeouts, logging
   of the provider, masking of TRN/IBAN/names/emails before cloud calls, offline deterministic
   engine as the last resort (clearly labelled "AI unavailable — rule-based answer").
2. agent/graph.py (LangGraph), goal "Recover all eligible VAT credits before they expire":
   - ONBOARD node: if onboarding_status() says the minimum is missing, the agent asks the user,
     in plain language, for exactly what is missing (one or two items at a time) and waits.
   - PERCEIVE: ingest new uploads, validate, extract.
   - PLAN (LLM): choose the next step from the tools, based on the current state.
   - ACT: run the tool; results go back to the planner (max 5 steps per turn).
   - EVALUATE (code): did the step reach its aim? Retry, ask the user, recalculate or escalate.
   - GUARD: run all guards on any proposed action; rejected -> replan (max 2) -> human review.
   - ROUTE: low risk -> auto-execute through the gateway (logged); consequential -> interrupt for
     human approval (LangGraph interrupt + checkpointer).
   - REMEMBER: state, tasks, document requests and decisions stored in SQLite.
   Example behaviour: finds AED X excess credit -> sees AED Y without valid invoices -> searches the
   uploaded ledger and documents -> requests the rest from the user -> recalculates after upload ->
   drafts the refund package for the eligible amount only -> asks the authorised person to approve.
3. Never invent numbers or reasons: every figure in an answer comes from a tool result; staged
   actions show their source (AI proposal with data, or user request with the user's words).
4. cli.py: a terminal demo that runs the agent on the current workspace (no built-in data); if the
   workspace is empty it shows the onboarding questions.
5. End-to-end tests with a mocked LLM: empty workspace -> asks for documents; missing invoices ->
   document request created; blocked category -> NEEDS_HUMAN_REVIEW; injection in an invoice ->
   quarantined; refund submission -> interrupt waiting for approval; approve -> guards re-run ->
   executed; reject -> nothing changes.
Run python -m pytest tests/ -v and show me git diff --stat. Then STOP for my checkpoint.
```

HUMAN CHECKPOINT: run `python cli.py`. With an empty workspace it must ask for documents. Then
add your own test documents and try an invoice containing an injection sentence.
Commit: `git commit -am "Phase 3: agent brain and approvals"`.

---

## PHASE 4 — Streamlit app (chat-first) and demo polish

```
Follow PLAN.md. Build Phase 4 only, then stop at the human checkpoint.

Layout (clean and simple):
- Top status bar: business name, TRN (masked), onboarding progress (e.g. 4/6), total eligible
  credits (AED), credits at risk (DUE_SOON/URGENT), pending approvals, LLM provider.
- Left (60%): the chat with Raseed. On first open, Raseed greets the user and starts onboarding:
  asks for the business profile (in chat or a short form), then asks for each required document
  with an upload box in the chat (st.chat_input with file upload, or st.file_uploader under the
  message). Show the user's message immediately, then a live status ("Reading invoices...",
  "Checking VAT maths...", "Calculating deadlines...") while the agent works.
- Right (40%), tabs: Checklist (required/recommended documents with status), Credit ledger (per
  tax period, with expiry and status colours), Deadlines, Approvals (cards with what will happen,
  amounts, source, guard results, Approve / Reject buttons, approver note), Documents (uploaded
  files, extraction status, NEEDS_REVIEW items with a quick review form), Audit log.
- Under each agent answer: a collapsed "How I got this" with tools used, guard checks (PASS /
  BLOCKED) and provider.
- Sidebar: Privacy mode (Masked cloud / Local only), Reset workspace (with confirmation), Show
  technical details toggle.
- Approval cards for SUBMIT_REFUND_APPLICATION show the full package preview (period, amounts,
  supporting invoices list, deadline) and, after Approve, a download button for the package
  (PDF + Excel) and a field to record the EmaraTax reference number.
- st.secrets bridge at the top of app.py (copy Streamlit secrets into os.environ) and an optional
  APP_PASSWORD login.

Run python -m pytest tests/ -v, start the app with python -m streamlit run app.py, and show me
git diff --stat. Then STOP for my checkpoint.
```

HUMAN CHECKPOINT: run `python -m streamlit run app.py` and go through onboarding with your own
documents: profile, then invoices, then a VAT return. Check the ledger, a missing-document
request, an approval and the audit log.
Commit: `git commit -am "Phase 4: Streamlit app"`.

---

## PHASE 5 (stretch) — Continuous autonomy

```
Follow PLAN.md. Add continuous monitoring:
1. APScheduler background jobs (started once per app process, thread-safe with SQLite):
   - daily: recompute deadlines; create tasks/alerts for DUE_SOON and URGENT credits;
   - on new upload: ingest, validate, update the ledger, re-check missing documents;
   - on a changed VAT return: rebuild the credit ledger and report differences;
   - document requests not answered after N days: remind the user;
   - refund applications: remind the user to update the FTA status; record decisions and
     amounts received; reconcile refunded amounts with the ledger;
   - legal uncertainty: escalate to the human with the reason.
2. An "Agent inbox" tab with the agent's tasks (open, waiting for user, waiting for approval, done)
   and what it did by itself since the last visit, posted as chat messages too.
3. A "Run check now" button and a "Demo time" control that moves the agent's clock forward
   (e.g. +30 days) so judges can see deadlines change.
Keep all guards and approvals unchanged. Add tests for each job (mocked time). Run all tests, then
STOP for my checkpoint.
```

Commit: `git commit -am "Phase 5: continuous monitoring"`.

---

## PHASE 6 (stretch) — Tax knowledge from official FTA documents (RAG)

```
Follow PLAN.md. Add a knowledge base built ONLY from official documents the user uploads
(FTA VAT guides, public clarifications, the VAT Law and Executive Regulation PDFs):
1. Split them into chunks with document name and page, index with rank_bm25, tool
   search_tax_knowledge(query) returns the top passages with source and page.
2. When the agent explains an eligibility decision or a deadline, it cites the source passage
   ("Source: <document>, page N"). If no passage supports an answer, it says so and escalates
   instead of guessing.
3. Rules in config/tax_rules.yaml show their source; a "Rules" tab lists them with their sources
   and a "verified by" field the team fills in.
4. The guards still decide; RAG only explains and helps the human review.
Tests: search returns cited passages; no source -> escalation. Run all tests, then STOP.
```

Commit: `git commit -am "Phase 6: RAG over FTA guidance"`.

---

## PHASE 7 — Final check, README, GitHub and deployment

```
Final check of Raseed.
1. Run all tests; all must pass.
2. Start the app from an EMPTY workspace and confirm: the agent asks for the business profile and
   documents first and calculates nothing before the minimum is met.
3. With the team's test documents, confirm each demo scenario and give me a table
   (scenario | expected | actual | PASS/FAIL): happy path to approval; missing documents ->
   request -> upload -> recalculation; ineligible input tax flagged; injection in an invoice and in
   chat blocked; credit close to expiry escalated; duplicate invoice excluded; reject changes
   nothing; LLM fallback works (simulate a Groq failure).
4. TechX checklist (manual section 11): local Ollama run, validated inputs, guards in code, human
   approval, one write gateway with rollback, keys in .env, two+ providers ending with Ollama,
   capped loops/retries/tokens, full logging, timeouts, broken-JSON handling, clear tools.
5. README.md: problem, architecture diagram (text), setup, how to run, demo script (90 seconds),
   privacy notes, limits (no direct FTA submission; rules to verify).
6. Make sure data/, uploads and .env are not tracked by git. Commit "Raseed MVP complete" and push
   to origin main.
Show me git diff --stat and the results table.
```

---

## Push to GitHub (new repository)
Create an empty repository on github.com (for example `Raseed`), then in the VS Code terminal:

```
git init
git add .
git commit -m "Raseed MVP"
git branch -M main
git remote add origin https://github.com/medhanitgeda41-max/Raseed.git
git push -u origin main
```

## Deploy on Streamlit Cloud (optional)
share.streamlit.io -> Create app -> repository `medhanitgeda41-max/Raseed`, branch `main`,
main file `app.py` -> Advanced settings -> Secrets:

```
LLM_MODE = "demo"
GROQ_API_KEY = "gsk_..."
GEMINI_API_KEY = "AIza..."
APP_PASSWORD = "your password"
APPROVAL_THRESHOLD_AED = "10000"
```

Only upload documents of a test business to the online app: files uploaded there are stored on
Streamlit's servers.
