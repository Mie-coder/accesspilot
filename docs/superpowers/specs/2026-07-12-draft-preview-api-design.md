# Draft Preview API Design

## Goal

Expose the first visible AccessPilot behavior in FastAPI: receive a partial access-request draft and return the fields the Agent still needs to collect.

## Scope

- Add `GET /health` returning `{"status": "ok"}`.
- Add `POST /api/drafts/preview`, accepting `RequestDraft` and returning the received draft, its ordered `missing_fields`, and `is_complete`.
- Keep this endpoint deterministic: no database, model API, LangGraph, authentication, or frontend changes.
- Reuse the existing `RequestDraft` domain model. Missing fields are program rules, never an LLM judgment.

## Error Handling

FastAPI/Pydantic returns HTTP 422 for malformed JSON or wrong field types. A valid incomplete draft returns HTTP 200; it is not an error because multi-turn collection is expected.

## Acceptance

Opening `/docs` shows both endpoints. Posting only `system_name` and `entitlement_name` returns the five missing business fields in the existing deterministic order.
