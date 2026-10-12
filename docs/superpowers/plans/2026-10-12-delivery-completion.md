# Delivery Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Track the checkboxes below.

**Goal:** Complete editable fictional intake, seven-day execution persistence, ten real integration cases and interview deliverables.

**Architecture:** Extend the existing request with an optional bounded draft and persist its snapshot beside the Feishu analysis. Scope read/write access to each completed request with a server HMAC token; store each day's execution in a separate text field.

**Tech Stack:** FastAPI, Pydantic, HTTPX, vanilla HTML/CSS/JS, Feishu Bitable, Dify, Vercel.

**Spec:** ../specs/2026-10-12-delivery-completion-design.md

## Global Constraints

- Use fictional data only; no credentials in artifacts.
- Maintain scenario_id-only backward compatibility and explicit offline preview labeling.
- Do not disable production WAF for acceptance.
- Do not call simulated tests real AI evidence or simulated metrics actual business results.

## Review Focus

- Retried requests with edited input must not silently return an old analysis.
- Validation responses must not echo rejected personal details.
- A request UUID alone must not authorize execution writes.
- Different day updates must preserve each other.
- Browser refresh and shared form selection must preserve the correct saved request.

### Task 1: Editable intake and immutable request content

Files: app/schemas.py, app/main.py, app/services/pipeline.py, app/services/feishu.py, tests/fakes.py, tests/test_intake.py.
- [x] Write and run failing API tests for edited draft propagation, field limits, fictional confirmation, PII rejection and UUID/content conflict.
- [x] Implement draft validation and input snapshot creation/reading; forward draft to pipeline and map safe errors.
- [x] Run affected tests and original pipeline/API/client tests.

### Task 2: Scoped execution tracking

Files: app/services/tracking.py, app/schemas.py, app/main.py, app/services/feishu.py, tests/test_tracking.py.
- [x] Write failing tests for forged tokens, not-found/not-complete records and seven independent day updates.
- [x] Derive HMAC token from server secret; expose POST /api/record and /api/actions, add seven execution text fields and safe server timestamps.
- [x] Run tracking tests; verify completed retry returns the same analysis and token.

### Task 3: Browser form and tracker

Files: static/index.html, static/app.js, static/styles.css, static/tracking.js, tests/test_static_contract.py.
- [x] Fill editable labeled fields from template selection; freeze pending draft and preserve retry input.
- [x] Render server validation/429 messages; completed result exposes request ID and saves token locally.
- [x] Add recovery/report view and seven-day metric/conclusion forms, saved status and progress count.
- [x] Verify desktop/mobile, reload and error behavior with a real browser.

### Task 4: Live acceptance and delivery evidence

Files: scripts/prepare_live_table.py, scripts/verify_live_flow.py, docs/live-acceptance.json, README.md, docs/case-study.md, docs/user-guide.md, docs/resume-project.md, docs/failure-cases.md.
- [x] Create missing Feishu fields idempotently and maintain WAF limits for record/action routes.
- [x] Run ten varied fictional drafts through local API and real services; read back snapshots/retries/logs, export sanitized inputs and outputs.
- [x] Document input validation, content conflicts and invalid access tokens; retain mock upstream failure evidence.
- [x] Update user-facing docs, actual screenshots, personal responsibility/AI-assistance disclosure and resume-ready wording.
- [ ] Full appropriate tests, credential/diff review, commit/push/deploy and anonymous public verification.
