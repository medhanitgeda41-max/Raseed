Act as a Principal Software Architect and Hackathon Technical Lead. Using the project idea,
features, and context we just discussed in this chat, create a comprehensive, execution-ready
engineering roadmap and project blueprint.

Do not write the full application code yet. Instead, structure your response so that I can use
it as a master specification (`PLAN.md`) to feed directly into an AI coding agent (such as
Cursor, Claude Code, Windsurf, or Cline) to build the project.

Please organize the blueprint into the following sections:

### 1. System Architecture & Hackathon Tech Stack
- Summarize the end-to-end data flow (how the frontend, backend, database, external APIs, and
AI components interact).
- Recommend the fastest, most reliable tech stack for a hackathon MVP based on our idea,
listing all required third-party APIs or SDKs.

### 2. Complete File-by-File Project Structure
- Provide a clean visual directory tree of the entire repository.
- Directly below the tree, list **every single file** and explain:
  - Its exact purpose and responsibility.
  - Key functions, components, or endpoints it will contain.
  - How it connects to other files (inputs, outputs, and dependencies).

### 3. Step-by-Step Build Order (What to Build First)
- Break development into logical, sequential phases (e.g., Phase 1: Setup & Data Schema, Phase
2: Core Backend/AI Pipeline, Phase 3: Frontend UI & Wiring, Phase 4: Testing & Demo Polish).
- Prioritize getting a working end-to-end "happy path" demo running as early as possible,
clearly separating **Must-Have MVP tasks** from **Nice-to-Have stretch features**.

### 4. Human-in-the-Loop (HITL) Checkpoints
- Embed explicit **"Human Checkpoint"** pauses at critical stages where the AI agent must stop
and collaborate with us before proceeding.
- Define exactly what our team needs to provide, review, or decide at each checkpoint (such as
supplying `.env` API keys, approving UI wireframes/layouts, tuning core logic, testing user
flows, or cutting scope if time runs short).

### 5. AI Coding Agent Handoff Prompts
- Provide ready-to-use, phase-by-phase starter prompts tailored for an AI coding agent so we
can instruct it to build the repository incrementally without losing context or breaking
earlier files.

If any technical details (such as preferred frameworks, authentication, or database choices)
were not specified earlier in our chat, choose sensible hackathon-friendly defaults, list your
assumptions clearly, and ask me to confirm them after presenting the roadmap.
