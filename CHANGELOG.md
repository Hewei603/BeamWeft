# Changelog

## 1.9.1 — 2026-11-22

- Use BeamWeft for the application, launch and update scripts, documentation, and Windows distribution.
- Save new portable projects as `.beamweft`. Earlier project files can be opened and saved as a new BeamWeft file.
- New desktop libraries use `BeamWeft-Data`. The launcher discovers an existing library when upgrading.

## 1.9.0 — 2026-11-22

The Windows launcher is `Launch-BeamWeft.bat`. Existing projects and laboratory libraries can be opened.

- Continue editing an Agent-built optical layout directly on the canvas. A project with one saved alternative and no main revision opens that design. Projects with parallel alternatives offer a choice. Archived replies open the exact revision used at the time, and a new Agent question can use the experimenter's unsaved canvas.

- Case originals retain their identities across deletion, import and concurrent uploads. Researchers can verify the bytes behind a result, restore a matching backup in place, and resume interrupted folder uploads. Agent reports link unsuccessful readings to their diagnostic records.

- The API accepts `X-BeamWeft-API-Key` and the original header for existing clients.
- The optical editor supports undo and redo across saves on the current branch. An entire pointer gesture or focused parameter edit is reversible in one step. Adopting a trial or replacing a stock component preserves subsequent local edits, including when the server responds after the user has left the workbench.
- Manual and Agent workflows can propagate registered incoherent source bounds or weighted rays through single-pass layouts. Ray tables retain their original columns, incident radiant power and row correspondence. Spatial and angular acceptance can be evaluated together at a receiver. Owned lenses with unknown glass keep their focal length unresolved, and a missing fiber core size or NA no longer silently supplies a beam envelope.
- Users and the Agent can compare camera measurements with saved predictions or other acquisitions on calibrated coordinates. The comparison integrates the reference over detector cells and reports common coverage, centroid shifts and signed residuals. Camera extraction preserves instrument codes and the original sensor axes.
- A stored optical project can supply the path from a known source to a named observation plane even when a later component lacks data. The bench and Agent use the same local prescription. Comparing candidates retains the full experimental layout and identifies changes in where a parameter or reference plane comes from.
- Reopening an interrupted task shows its saved tool calls and identifies the source of any final answer. Local processes sharing a database can check whether the original executor still owns a task before marking it interrupted.

- Users and the Agent can continue a paraxial beam from an interior material plane through homogeneous media and coaxial refracting surfaces. A measured caustic can supply one registered transverse axis.
- Scalar Fresnel propagation v2 publishes the field after an aperture, including its transverse phase and cumulative clipping losses. A new calculation can reuse a compatible historical method's frozen inputs and cite the original run.
- Complex results load individual published quantities on demand. Simple structured plugin parameters can be edited with ordinary form controls. Missing material indices now appear as unknown throughout the project overview.

## 1.8.0 — 2026-11-03 maintenance baseline

The owner reserved 2.0 for a later third review and major optimization. The runtime and package identifiers at this maintenance baseline were 1.8.0. Earlier 2.0/v3.x development records remain available.

- Preserve the latest generic component editing/replacement, experiment context, project revision and saved analysis workflows. Add the general-purpose product constraint to project guidance.
- Preserve signed mirror curvature; reject malformed/non-finite measurement rows with line numbers; normalize explicitly configured coating percentages and power units; reject nonphysical passive-loss fits as inapplicable.
- Require a reviewer/admin to clear an existing damage state, including during return. Returning without changing condition preserves that state.
- Preserve composer and project drafts across case/workbench tabs; append contextual prompts without overwriting text or mixing project versions. Warn before closing a page with edited text/model data.
- Keep successful saves/replacements visible when a subsequent read fails. Isolate optional case-resource failures, expose project retry, and distinguish unknown inventory loan state from an empty list.
- Add an explicit return dialog, direct evidence navigation, keyboard-accessible workbench upload, graph reload retry, responsive navigation, consistent case fields and higher-contrast primary controls.
- Validate 643 backend tests (2 PostgreSQL tests skipped), 3 frontend draft-contract tests, ESLint, production build and isolated browser workflows. The browser Agent provider is scripted; numerical kernels are real.

## Historical development milestones

Historical development label, superseded by the current 1.8.0 maintenance baseline; retained without rewriting its original evidence.

The release consolidates the task-led Agent, editable optical engineering, Codex subscription transport, scientific visualization and daily-workflow repairs. Backend, frontend handshake and frontend package versions are 2.0.0. Earlier v3.x labels below describe unpublished development milestones, not released Git tags.

- Added a homepage API Key / Codex exec connection selector. Codex exec reuses the backend host's ChatGPT CLI sign-in, defaults to GPT-6.1 Sol, and supports existing chat, image intake, plans, reports, memory summaries and the iterative workbench tool loop. Structured CLI actions are validated and executed by the workbench tools. Added sign-in status, a real connection test, timeouts, readable subscription errors and subscription-aware usage accounting. Saved API profiles remain available when switching back. See docs/CODEX_EXEC.md.
- Added physical-position field-radius/envelope plots, selectable Gaussian cross sections, radius/diameter and unit controls, medium/active-region shading, and CSV/SVG/PNG exports. Workbench scenarios and two-parameter stability maps have descriptive axes, units and explicit calculation boundaries; engineering results can use the full page width.
- Current optical projects now take precedence over obsolete specialized designs in plans/reports and the case overview, while historical records remain available. Agent completion refreshes case artifacts without discarding an in-progress conversation.

- Published the real-case acceptance report, optical figures, editable model and raw numerical evidence in the historical source tree. Maximum-power optimization remains explicitly incomplete because the current ABCD tool lacks the required gain/power solver and measured constraints.
- Fixed clean-environment release checks: CI fetches a pinned upstream Rezonator calculation oracle without bundling it in the product; local cross-checks explicitly skip when the optional checkout is absent. Bare PostgreSQL URLs now select the installed psycopg2 driver consistently in the app and Alembic.

## Development milestones included in 2.0

- v3.4 adds versioned, editable optical projects with owned-part locks, physical element tables, T/S radius and wavefront plots, JSON/SVG export, and saved two-parameter stability maps. Agent tools read, create, revise and import these projects; saves retain earlier revisions and detect stale edits.
- General optics now supports normal-incidence thick lenses, focusing quadratic GRIN, real symplectic custom T/S ABCD matrices, and single-pass equivalent M². Measured-caustic fitting returns waist, divergence and equivalent M². Standing-wave return propagation handles asymmetric thick optics.
- Fixed mirror roles: concave input M1 and flat output M2 are modeled without swapping. Owned mirrors stay fixed; unknown transmission stays unknown. Curved-input refraction remains excluded from pump placement until substrate data is supplied. The runtime is Python/NumPy, not a full Rezonator C++ port or power/thermal-field solver.

- v3.3 makes the primary experiment workspace a task-led, iterative Agent: native model tool calls, typed execution feedback, error repair, preserved partial results and actionable questions that can be answered in the same conversation. The configured GPT-6.1 Sol uses Responses tool calling; Rezonator is an interchangeable capability, not a hard-coded application workflow.
- Added a general ordered optical-system adapter for single-pass, standing-wave and coplanar ring models, T/S beam profiles, analytic waist sampling and bounded parameter scans. Added Agent access to actual inventory, attachments, saved calculations, literature, phase matching, coating evaluation and measured-power fitting. The specialized plane-concave designer remains an optional tool with its existing detailed visualization.
- Added a task/results/tool-trace UI, readable Markdown reports, structured measurement guidance, exports, stop/continue behavior and native provider accounting. Old workflows remain explicitly labeled for compatibility. Runtime optical calculations are Python/NumPy ABCD; no Rezonator C++ executable is invoked.

- v3.2 replaces the overview envelopes with a scientific optical analysis bench: zoom/pan, physical axes, radius/diameter and unit selection, caustic and wavefront plots, crystal boundaries, analytic waists, Rayleigh lengths, precise ABCD cursor values, and raw CSV / SVG / PNG exports. Plotting is lazy-loaded.
- M2 spacing and thermal-lens previews now recompute the saved design directly through read-only physics endpoints without invoking the LLM. Piecewise q propagation retains both sides of interfaces/lenses and adaptively resolves narrow pump waists. Unstable cavities return no fabricated eigenmode; stability scans preserve unclipped numerical values. The current model remains scalar and axisymmetric (T=S), not complete Rezonator feature parity.

- v3.1 connects title/description, owned optics, inventory bindings and fixed positions to the numerical design search. R-only lenses use an explicit N-BK7 assumption; unknown core/NA no longer blocks geometrical placement. Missing parts become proposed specifications rather than fabricated user facts.
- Added 1 mm M2 rear-face distance scans, cold-cavity and thermal-scenario stability curves, direct placement cards, an editable constraint panel, whole-path component sourcing, and deterministic Rezonator 2 cold-cavity export. Cross-checked exported matrices and mode sizes against the bundled Rezonator reference calculations. The runtime remains the Python ABCD implementation.
- Search now scores all admissible grid points before pruning, including the exact thermal-lens trace. Fixed constraints are enforced, incompatible placements are explained, and nominal fiber markings cannot silently become measured beam sizes or an output-power optimum.
- Rebuilt experiment planning as the v3.0 conversational design workspace: title/notes case creation, image-backed requirement intake, persisted background runs, editable follow-up requirements, version history, candidate comparison, beam envelopes, component coordinates and CSV exports.
- Added deterministic end-pumped plane-concave cavity design with composite-crystal optical length, active-section threshold gain, bounded thermal-lens scenarios, fiber imaging, and inventory matching by coating, curvature, quantity and condition. R50 is not f50 and AR880 is not AR808. Output power and optimal output coupling are not invented.
- Grounded subsequent reports in the latest engineering design and retained previous versions when a provider call fails. Existing cases and generated artifacts remain compatible; no schema migration is required.

- Added homepage Agent configuration (v2.3.0): choose a provider/model, enter a hidden-by-default API key, and optionally customize compatible API endpoints. Settings apply to subsequent AI requests immediately and persist across restarts; saved keys are never returned to the browser, and blank keys preserve existing credentials.
- Fixed the v2.2.1 daily-workflow defects: same-file inventory refresh preserves item IDs, manual condition and returned-loan history; removed rows with history remain at zero stock, and ambiguous identity changes roll back with a clear conflict message.
- Plans, troubleshooting and reports now retrieve evidence before model generation and persist the same citation set. Model context includes recorded measurements, conclusions and completed module results, and two-tier retrieval respects the requested result limit.
- Legacy upload migration now repairs persisted attachment, module-file and knowledge-source paths, including already-migrated databases launched from a new checkout. Repairs retain a pre-change SQLite backup and only point to existing matching files.
- Agent literature and physics tools yield the event loop while keeping database writes on the request thread. Restoring a chat from another case now preserves its history and continues the same session; case/session selectors are locked while a reply is running.

- Added literature search (v2.2.0) with three entries — a standalone 文献检索 page (`POST /api/literature/search`), a `literature_search` case module, and an agent chat mode — backed by one deterministic service over keyless OpenAlex + arXiv: DOI/title dedup, honest statuses (per-source failure notes, `failed` instead of silently-empty, Chinese `needs_input` naming the missing field), BibTeX export, and an explicit "metadata + abstracts only, no full texts were read" disclaimer. Archived module results index into case RAG so plans/reports can cite them. Chat queries derive the search terms deterministically from the sentence (asking words stripped) or the case's physics parameters.
- Fixed `generated_content_id` never persisting into any module's stored `result_json`: the run endpoint mutated the same dict object it had already assigned, which SQLAlchemy's JSON column change detection cannot see.

- Fixed the stale-backend deployment gap: the launcher now takes over ports held by earlier workbench processes, `/health` reports the app version, and the frontend shows a Chinese "restart the launcher" banner on version mismatch (`tests/test_version_handshake.py` pins the three version strings together).
- Integrated crystal cut-angle matching into the L1 inventory evaluator (`phase_match` requirement: per-material phase-match angle vs labelled cut, three-state with 180°−θ equivalence) and added a "nonlinear crystal (cut angle)" mode to the inventory match form.
- Added a full-BOM export (`/api/cases/{id}/components/bom.csv`, Chinese headers, owned parts included) for the teardown 泵表 deliverable; both component CSVs now carry a UTF-8 BOM so Excel renders Chinese correctly.
- Closed real-workbook geometry parsing gaps (lens focal lengths `F=300mm/f=250 mm/F100` into a new `focal_length_mm` field + migration 0010, `Φ25*5mm` diameter×thickness, `D>18mm` lower bounds); geometry cells that still parse to nothing now downgrade the row to the review queue with a "几何列未解析" note instead of vanishing.
- Replaced the raw-JSON module run config with typed per-module Chinese forms (cavity design / phase match / coating / power curve / stability); the JSON textarea remains as an advanced override.

- Added an MIT `LICENSE` file for open-source distribution.
- Added a public synthetic RAG eval JSONL example and wired its format check into the local acceptance script.
- Added configurable score/margin abstention settings for ambiguous retrieval results.
- Added strict JSONL eval row validation, private eval path containment checks, and CI backend acceptance extras.
- Added CLI acceptance thresholds for authorized RAG eval holdout runs.
- Added a final acceptance audit script for local, hosted CI, and private eval gates.
- Updated local acceptance evidence to `161 passed, 2 skipped`.
- Added project-level ACL helpers and enforced case access across case, knowledge, attachment, generation, and Agent APIs.
- Added pre-retrieval RAG filtering so case-specific chunks are not returned to unauthorized users.
- Aligned pgvector defaults with the example dense embedding model dimension.
- Added explicit pgvector embedding dimension validation.
- Added CI for backend tests and frontend builds.
- Added contribution, security, and code of conduct documentation.
- Added JSONL RAG eval loading, positive/negative retrieval metrics, private dataset eval endpoint, and threshold tuning scripts.
- Added case module ACL coverage so module files, analysis runs, and component/procurement items follow case permissions.
- Added authorized lab document import workflow, deployment/RAG operations documentation, and Docker health checks.
- Made the pgvector migration dialect-aware so local SQLite Alembic upgrades remain valid while PostgreSQL uses pgvector.
- Added an enterprise acceptance matrix, CI pgvector integration job, Chroma dense retrieval coverage, and enabled-reranker coverage.
- Split optional sentence-transformers dependencies into `requirements-ml.txt` and added a backend `.dockerignore` for lean Docker builds.
- Added issue/PR templates and a local `acceptance_check.py` script.
- Verified Docker Compose startup and live pgvector integration locally.
