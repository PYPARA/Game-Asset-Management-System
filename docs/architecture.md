# Architecture

## Boundaries

- `apps/web`: React/Vite production workbench and encrypted browser credential vault.
- `apps/api`: loopback-only FastAPI service, SQLite index/job store, provider adapters, QA, review, and release services.
- `packages/emperor_adapter`: dependency-free, read-only legacy parser and deterministic migration/export library.
- Project `.game-assets/`: canonical, reviewable metadata and approved revisions.
- Project `output/game-assets/`: ignored candidates, rejected evidence, and generated QA artifacts.

## Asset model

An asset has a stable key, one core kind (`content`, `design`, `entity`, `media`, or `production`), a subtype, custom schema reference, tags, and typed relations. A character and a portrait are separate assets joined by `depicts`; a CG and a scene are joined by `illustrates`; an icon and item are joined by `represents`.

Revisions are immutable. Content review, generation progress, and publication state are separate. Approval records bind the exact revision and its dependency/provider hashes. Release generation rejects unapproved revisions.

`current_revision_id` is exclusively the approved pointer. `latest_candidate_revision_id` is the pending review pointer; the frontend never substitutes one for the other and will not enable approval until the candidate rendition and hard-QA evidence are loaded.

## Provider lifecycle

The browser stores an encrypted provider key and sends it only to `/api/providers/{id}/unlock`. The API retains it in process memory. After a restart, queued work remains durable but moves to `credentials_locked` until the browser unlocks the profile again.

Paid work starts from an immutable generation plan. A confirmed plan expands into dependency-aware jobs. Retryable transport/rate-limit failures preserve their budget across restarts; authentication, billing, quota, validation, capability, and content-policy errors do not burn blind retries.

## Promotion

Provider output is staged as a candidate, normalized, and hard-validated. Human review promotes a specific revision. Publication uses a project lock, backs up the prior formal file, writes a temporary file, fsyncs it, and atomically replaces the target. Manifest generation selects only approved revisions.

## Narrative Atlas readiness

V1 persists typed, cyclic domain relations independently from the acyclic generation dependency graph. The v1.1 scene workspace can therefore add a chapter tree, scene editor, relation canvas, coverage checks, and missing-asset planning without changing the storage model.
