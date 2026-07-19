# Design QA — Option 1 Production Workbench

## Sources

- Visual source of truth: `docs/design/reference-workbench.png` (1487 × 1058)
- Future v1.1 source, deliberately excluded from v1: `docs/design/reference-narrative-atlas.png`
- Implementation: `apps/web/src/Workbench.tsx`, `apps/web/src/components/*`, `apps/web/src/styles.css`
- Final browser capture: `docs/design/implementation-final-1280x720.jpg`
- Combined final comparison: `docs/design/comparison-workbench-final.jpg`

## Viewport and state

- Browser QA viewport: 1280 × 720 at device pixel ratio 2 (the in-app browser's fixed desktop viewport).
- State: Emperor-Simulator demo ledger, 2D media category, first portrait candidate selected, side-by-side comparison, prompt tab.
- The layout keeps the planned 1024 px minimum width, responsive reductions below 1180 px, high-contrast focus rings, and reduced-motion handling. Production build validates the same responsive CSS used at the 1440 × 1024 acceptance target.

## Interaction and integration evidence

- Search for `宫廷夜宴` reduced the result set to exactly one row; clearing restored the ledger.
- Asset-type filtering returned the expected CG subset.
- Selecting `portrait.han-lie.resolute` updated the inspector.
- Side-by-side and overlay comparison both rendered; overlay exposed the 50% opacity slider.
- Prompt, hard-QA, and revision-history tabs worked; tabs support Left/Right, Home, and End.
- Demo approval and rejection are explicitly labelled as non-persistent. Real review is disabled until the true candidate ID, rendition, and QA evidence are loaded.
- Provider settings opened with focus containment and displayed the IndexedDB/WebCrypto/XSS boundary warning.
- Emperor import dialog performed a real read-only API dry-run against `/Users/0x10/Documents/Github/Emperor-Simulator`: 3 anchors, 186 media assets, 186 QA records, 0 QA errors, 0 missing media, and 882 parseable V2/V3 records.
- Import preview remains bound to the exact scanned source path; changing the path invalidates it. Formal import additionally requires a separate managed-project destination.
- A fresh browser session completed the search, selection, overlay, QA tab, settings, and real import-preview flow with zero console warnings or errors.

## Comparison history

1. Round 1: `docs/design/comparison-workbench-round-1.jpg`. The three-column anatomy, charcoal hierarchy, table density, inspector, and task strip matched the selected mock. Review-contract and accessibility audit findings remained.
2. Final: `docs/design/comparison-workbench-final.jpg`. Preserved the visual hierarchy while adding honest disabled states for unconnected actions, keyboard-operable asset buttons, modal focus management, stronger contrast, real candidate/approved revision separation, and safe review gating.

## Automated evidence

- Frontend: 16/16 Vitest tests passed.
- Frontend production build passed; the remaining 558.56 kB entry-chunk warning is a non-blocking code-splitting optimization.
- API: 20/20 pytest tests passed.
- Alembic upgraded a fresh SQLite data directory from the repository root successfully.
- Emperor adapter: 6/6 pytest tests passed, including real-project read-only acceptance.
- Real managed-project import wrote 638 metadata files and copied 0 media files; the second run wrote 0 and reported all 638 unchanged. Export returned the expected 186 stable runtime entries.
- API health and OpenAPI smoke tests passed; the live Emperor preview returned HTTP 200.
- The production build served by FastAPI at `http://127.0.0.1:8787/` loaded and completed the real dry-run flow with zero browser console warnings or errors.

final result: passed
