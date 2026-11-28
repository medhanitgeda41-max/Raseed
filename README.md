# Raseed (رصيد) — Autonomous UAE VAT Recovery Copilot

> **UAE Federal Tax Authority (FTA) Hackathon 2026**
> *Chat-first autonomous VAT recovery built for UAE SME owners who don't have an in-house tax team.*

---

## 1. The Problem
Small and medium business owners in the UAE leave millions of dirhams in eligible VAT credits unclaimed every year:
- **Statutory Expiry (5-Year Deadline)**: Under Federal Decree-Law No. (8) of 2017 as amended by Decree-Laws No. (16) of 2024 and (17) of 2025, excess recoverable input VAT expires after **5 years**, with a strict transitional cutoff on **31 December 2026** for pre-2022 credits.
- **Complex Rules & Risk**: Business owners fear penalties from mistakes (e.g. accidentally claiming blocked entertainment or personal motor vehicle expenses under Article 53).
- **No Tax Department**: Most SMEs cannot afford dedicated tax consultants or full-time VAT accountants to perform complex multi-period reconciliations.

---

## 2. The Solution in Plain Words
**Raseed** is your autonomous VAT recovery partner. It connects conversationally with the business owner to find unclaimed money, verify invoices, calculate statutory expiry deadlines, and prepare ready-to-submit FTA refund applications.

**The Golden Safety Rule**: 
> *The AI reads, explains, and prepares. Strict deterministic Python code does all VAT maths and checks every tax rule. The business owner approves every consequential action. The AI never submits or files on its own.*

---

## 3. How Raseed Works
A simple 4-step workflow visible right across the top of the interface:

```text
📤 Upload your documents ──▶ 🔍 Raseed checks them ──▶ 💰 Finds money you're owed ──▶ ✅ You approve the refund
```

1. **Upload**: Drop in your purchase registers (Excel/CSV), tax invoices (PDF), and filed Form 201 VAT returns.
2. **Check**: Pure Python guards verify 15-digit TRNs, validate 5% VAT calculations (±0.05 AED tolerance), block ineligible expenses (entertainment/cars), and reconcile books across 4 accounting sources.
3. **Find**: Raseed builds a transaction-level VAT credit ledger, maps excess input tax per tax period, and prioritizes claims based on statutory expiry.
4. **Approve**: An interactive approval card appears in the chat. You review the exact amounts and click **Authorize & Commit** to generate official EmaraTax refund packages (.xlsx and .pdf).

---

## 4. Setup & Running

### Local Quickstart
```bash
# 1. Clone repository & create virtual environment
git clone https://github.com/your-org/raseed.git
cd raseed
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your keys (or leave default for local Ollama / offline deterministic engine)

# 4. Run tests (115 automated tests)
pytest tests/ -v

# 5. Launch the Streamlit application
streamlit run app.py
```
Open **`http://localhost:8501`** in your browser.

### Deploying on Render (Production Multi-Tenant Online Demo)

Raseed is fully configured for automated cloud deployment on Render using Cloudflare R2 object storage and Neon Serverless PostgreSQL. Render's free instances sleep and reset local disks, but Raseed's state remains 100% persistent in the cloud.

#### 1. One-Click Blueprint Deployment via `render.yaml`
1. Fork or push this repository to your GitHub account.
2. In the [Render Dashboard](https://dashboard.render.com/), click **New +** → **Blueprint**.
3. Select your repository. Render automatically reads `render.yaml`.
4. Fill in the required environment variables:

| Environment Variable | Description | Example / Default |
|---|---|---|
| `PYTHON_VERSION` | Python runtime version | `3.12.7` |
| `LLM_MODE` | Primary LLM mode | `demo` |
| `STORAGE_BACKEND` | Storage provider | `r2` |
| `DATABASE_URL` | Neon Postgres connection string | `postgresql://user:pass@ep-xyz.aws.neon.tech/neondb?sslmode=require` |
| `R2_ENDPOINT` | Cloudflare R2 S3 endpoint | `https://<account_id>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | Cloudflare R2 Access Key ID | `your_r2_access_key` |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 Secret Access Key | `your_r2_secret_key` |
| `R2_BUCKET` | Private R2 bucket name | `raseed-documents` |
| `GROQ_API_KEY` | Groq cloud LLM API key | `gsk_...` |
| `GEMINI_API_KEY` | Google Gemini API key (fallback) | `AIza...` |
| `SIGNUP_CODE` | Optional registration passcode | *(leave blank for open registration)* |

#### 2. Multi-Tenant Company Workflow
- **Different Companies**: Each SME business owner creates their own company account on the `/` login screen with their company name, 15-digit TRN, owner name, email, and password.
- **Strict Isolation**: Company A and Company B are completely isolated at the database, storage, and agent level. Files in R2 are partitioned by `<business_id>/`. Direct cross-tenant access attempts trigger a strict `PermissionError`.
- **Persistent Memory Across Restarts**: Documents, OCR extracted text, conversational messages, confirmed business rules, and staged refund claims are restored from Neon Postgres and Cloudflare R2 even after Render restarts.
- **Fast-Skip Fallback**: When running online without local Ollama, Raseed immediately fast-skips local models (timeout <= 1.5s) and routes to Groq → Gemini → Deterministic Rule Engine.

---

## 5. 3-Minute Hackathon Demo Script: "Sara, Owner of Al Noor Trading"

### Minute 1: Onboarding from a Clean Slate
1. Open Raseed on a clean workspace. Notice the **status bar shows no numbers** and the panel states *"No VAT credits yet"*.
2. In the chat, type:
   > *"Hi, I'm Sara. My company is Al Noor Trading LLC, TRN 100123456789001, registered 2020-01-01, quarterly returns."*
3. Raseed responds warmly, automatically registers **Al Noor Trading LLC**, and guides Sara to Step 2 (Document Ingestion).
4. Use the in-chat uploader to attach purchase registers and Form 201 VAT returns.

### Minute 2: Analysis, Reconciliation & Time Travel
1. Raseed analyzes all tax periods, builds the credit ledger, and updates the left **VAT Credit Panel**.
2. Sara asks: *"What is about to expire?"*
3. The chat automatically commands the visual panel to switch to **Expiring soon** (`at_risk` view). Raseed shows her 2021 excess credits.
4. **Demo Time Acceleration**: In the sidebar, click **+90 Days** or **+365 Days**. 
   - Observe the 2021 credits immediately transition from safe green (`OK`) to orange (`DUE_SOON`) and urgent red (`URGENT`) as they approach the Decree-Law 17/2025 transitional deadline of 31 December 2026.
   - An automatic alert triggers in the chat stream warning Sara of impending forfeiture.

### Minute 3: In-Chat Approval & EmaraTax Package Export
1. Sara clicks the quick-question button: **"Claim my refund"**.
2. Raseed runs pre-submission safety checks and presents a clean **Refund Approval Card** directly inside the chat:
   - *Money you will receive: AED 12,400.00*
   - *Tax periods included: 2021-Q1, 2021-Q2*
   - *Status: Waiting for your OK*
3. Sara enters an approver note and clicks **✅ Authorize & Commit**.
4. Raseed locks the transaction, logs an immutable audit event, and provides one-click download buttons:
   - 📥 **Download Excel Dossier (.xlsx)**: Full FTA-compliant transaction schedule.
   - 📄 **Download PDF Dossier (.pdf)**: Formal audit summary ready for EmaraTax upload.

---

## 6. Privacy & Safety Guarantees

| Safety Layer | Implementation |
|---|---|
| **Human Authorization** | The AI **never** submits refund applications or writes off credits. Only the owner can authorize actions. |
| **Pure Python Guards** | Tax math, 5% calculation checks, and Article 53/54 exclusions are executed by deterministic code in `guards/`, never an LLM. |
| **Write Gateway with Rollback** | All state mutations pass through `tools/write_gateway.py`. Any failure triggers an automatic SQLite `ROLLBACK`. |
| **Privacy Masker** | All 15-digit TRNs, IBANs, and personal emails are masked before any payload leaves the machine for cloud LLMs. |
| **Zero Data Tracking** | `.env`, `data/`, and uploaded customer files are strictly excluded from git tracking via `.gitignore`. |

---

## 7. TechX Architecture Checklist (Manual Section 11)

- [x] **Local Execution**: Fully functional on local Ollama models (`LLM_MODE=local`) and offline deterministic rule fallback.
- [x] **Input Validation**: Pydantic v2 schemas (`models/schemas.py`) strictly validate TRNs, currencies, amounts, and dates.
- [x] **Hardcoded Safety Guards**: Code-level guards for prompt injections, arithmetic, deadlines, and eligibility.
- [x] **Human-in-the-Loop**: Consequential actions halt in `staged_actions` pending human approval.
- [x] **Transactional Gateway**: Atomic transactions with rollback guarantee zero partial writes.
- [x] **LLM Fallback Chain**: 5-tier fallback: Groq (120b) → Groq (20b) → Gemini 2.5 Flash → Ollama (qwen3) → Offline Deterministic Engine.
- [x] **Loop Caps & Timeouts**: Chat tool loops capped at 3 iterations; LangGraph graph capped at 5 steps; 30s network timeouts.
- [x] **Audit Trail**: Every user, agent, and system action recorded in `audit_events`.
- [x] **Knowledge Retrieval (RAG)**: Version-aware `rank_bm25` index over official FTA PDF guidance with page-level citations.

---

## 8. Current Limitations
- **No Direct FTA API Submission**: The UAE FTA EmaraTax platform does not offer an open public submission API for direct machine-to-machine filing. Raseed prepares the complete, pre-verified Excel and PDF dossiers for the owner to upload directly to EmaraTax.
- **Guidance Verification Status**: Rules in `config/tax_rules.yaml` marked `To verify` require the owner to upload the matching official FTA guidance PDF into the knowledge base to achieve `🟢 Verified` status. Conservative statutory safety defaults are enforced in the interim.
