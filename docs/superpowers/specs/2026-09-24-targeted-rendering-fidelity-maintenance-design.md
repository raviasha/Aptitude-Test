# Targeted Rendering Fidelity Maintenance Design

## Status

Approved in conversation on 2026-09-24. This design replaces the proposed exhaustive revalidation of all 5,166 questions with a baseline-preserving maintenance process. The existing 39 `*_all_vision_text_only.zip` archives are accepted as the current baseline. Only reported defects, records affected by a generalized fix, and the visual-heavy Chapters 36–39 enter the new validation loop.

The focused-fidelity branch contains checkpointed work that predates this design. That work is not considered validated or approved for release. The implementation plan must compare it with this design and retain, revise, or remove each part accordingly.

## Purpose

The question banks are broadly usable, but a small number of defects can appear at different stages: textbook extraction, mathematical representation, source-asset cropping and association, packaging, import or replacement, and browser rendering. Fixing a visible question by hand can hide the underlying defect and allow it to recur.

The maintenance process will diagnose each reported issue at its actual stage, reconsider the question's rendering category when necessary, improve the shared pipeline rule, locate other records affected by that rule, and regenerate only archives whose visible or packaged output changes.

## Goals

- Preserve the good work already present in the 39 baseline ZIPs.
- Classify display fields by the rendering strategy they require without rewriting every package merely to attach metadata.
- Treat each reported fidelity issue as evidence about both the assigned category and the category's shared strategy.
- Use the 2017 RS Aggarwal PDF as the source ground truth.
- Use source crops for genuine spatial content such as tables and graphs; do not create AI redraws.
- Validate changed records through the actual KSAT import and browser-rendering path.
- Keep fixes general and versioned, then rerun only records whose dependencies changed.
- Package and promote chapters independently as they pass.
- Preserve and document textbook inconsistencies rather than silently correcting them.

## Non-goals

- Re-extracting or visually revalidating every existing question before the banks remain usable.
- Achieving pixel-for-pixel similarity with the textbook.
- Proactively converting the clocks or Area chapters to source images.
- Treating all mathematical notation as an image.
- Maintaining runtime question-specific repair maps for new pipeline defects.
- Delaying usable chapters while pursuing certainty for a small number of difficult questions.

## Accepted Baseline and Scope

The 39 current `*_all_vision_text_only.zip` files are the accepted baseline. Their hashes and question inventories will be recorded before further work. Unchanged baseline records remain accepted and do not require a new source-versus-render vision pass.

Work proceeds in two streams:

1. **Reactive fidelity maintenance.** Reported issues are investigated, generalized, fixed, and used as regression cases. Chapter 1 question 124 and Chapter 3 question 113 are the first cases.
2. **Planned source-visual work.** Chapters 36–39 are processed after the earlier reported issues are resolved:
   - Chapter 36: Tabulation
   - Chapter 37: Bar Graphs
   - Chapter 38: Pie Chart
   - Chapter 39: Line Graphs

Clocks remain outside the proactive cropping exercise because there are few affected questions. Area is not treated as a visual chapter by default. A future reported issue in either area follows the reactive process.

## Rendering Classification Registry

Classification metadata lives in a versioned pipeline-side registry rather than being inserted into every baseline archive. This prevents a metadata-only change from forcing regeneration of all 39 ZIPs.

The registry uses a stable question identifier such as `ch03-q0113` and stores decisions per display field. The question stem, each option, solution, and display media may require different strategies.

Initial categories are:

- `plain_text`: ordinary prose, integers, punctuation, and other content that renders reliably as text.
- `unicode_math`: linear mathematical content that the supported fonts and browser render unambiguously, including verified superscripts and subscripts.
- `structured_math`: mathematical content requiring controlled markup or a dedicated renderer, including recurring decimals, fractions, roots, and expressions that are ambiguous in plain Unicode.
- `structured_table`: information whose row and column relationships must be preserved by a software-rendered table.
- `source_visual`: inherently spatial textbook content displayed from a verified crop, including graphs, charts, and diagrams.
- `review_needed`: content whose correct strategy cannot yet be selected confidently.

Classification is a versioned decision, not permanent truth. Automatic rules propose categories from existing package content and chapter configuration. Ambiguous baseline fields retain their existing output and are flagged without blocking an unchanged chapter. A changed record cannot be promoted while a required field remains `review_needed`.

For Chapters 36–39, a missing or uncertain crop for a question that depends on a table or graph blocks that question from promotion or causes an explicit documented omission. It must not silently fall back to flattened text when that loses spatial meaning.

## Category Evolution

Every fidelity report triggers two questions:

1. Was the field assigned to the correct category?
2. Is the strategy for that category adequate?

A resolution may reclassify the field, refine the deterministic strategy for an existing category, or introduce a new category when multiple questions have a materially different need. New categories require a clear rendering contract and regression coverage; a one-question peculiarity remains a reviewed source exception unless it exposes a general rule.

Category rules and registry entries carry versions and dependency tags. Changing either invalidates downstream fingerprints only for fields and records that depend on the changed decision.

## Issue-to-Fix Lifecycle

For each reported issue, the pipeline records the textbook question identity and gathers four artifacts where applicable:

1. the source PDF crop;
2. the current candidate or packaged record;
3. the imported KSAT record;
4. the actual rendered KSAT screenshot.

The failure is assigned to one or more stages:

- source extraction;
- math representation or rendering-category selection;
- source-asset crop or association;
- packaging;
- import or update of an existing bank;
- frontend font, CSS, or renderer behavior.

The investigation then:

1. identifies the earliest stage at which the visible meaning diverges from the source;
2. checks and, if needed, revises the field's category;
3. adds a literal regression fixture for the reported behavior;
4. changes the generalized pipeline or application rule;
5. increments the relevant rule or category version;
6. finds every field carrying the affected dependency tag;
7. invalidates only those downstream fingerprints;
8. rebuilds and renders the affected records;
9. regenerates only archives whose packaged bytes or visible behavior changes; and
10. records the result in the audit manifest.

A question-specific runtime repair map is not an acceptable primary fix. Such a map may exist only as a narrowly documented compatibility bridge for stale banks that were already imported and cannot yet be replaced. Newly generated packages must be correct without it.

## Source Visuals

`source_visual` fields use crops from the supplied textbook PDF. Crop metadata records the PDF hash, page number, coordinates, render settings, and asset hash. Existing reviewed shared-context mappings for Chapters 36–39 are used to associate one table or graph with the correct question group.

Cropping may remove unrelated page furniture, but it must retain every label, axis, legend, unit, scale, and data value required to answer the associated questions. The application receives semantic alternative text in addition to the source crop. No generative model redraws or reconstructs textbook visuals.

When one source visual serves several questions, the pipeline stores one content-addressed asset and associates it with each applicable record. A crop or association change invalidates every dependent record.

## Validation Policy

The process is fail-closed for changed records and changed archives, not for the untouched baseline as a whole.

Every changed question must pass:

- schema and package validation;
- answer-letter and option-integrity checks;
- required-asset existence, hash, crop, and association checks;
- import into an isolated KSAT bank using the supported create or explicit replacement path;
- capture of the actual KSAT page at a supported desktop viewport;
- mechanical clipping and overflow checks; and
- source-versus-render vision comparison for the changed or reported display fields.

Vision comparison evaluates semantic and notational fidelity. Font family, line wrapping, and decorative spacing may differ. Missing or changed numbers, operators, superscripts, recurrence marks, grouping, labels, data relationships, answer mapping, or required visual content fail validation.

Unchanged baseline questions are not subjected to an exhaustive vision sweep. Automated rule checks may scan all records cheaply, but visual review is concentrated on reported issues, fields affected by a rule change, and the planned source-visual work in Chapters 36–39.

## Failure Handling and Explicit Omissions

If a changed record fails any required check, that record and its regenerated chapter ZIP are withheld while the failure is classified and corrected. A semantic disagreement cannot be retried until a model happens to approve it.

For the planned visual chapters, up to approximately five percent of questions may be omitted when their source crop, association, or meaning cannot be established with reasonable confidence. Each omission must be explicit in the audit manifest and package metadata, with its textbook question number and reason. The pipeline must never drop a question silently.

If the textbook itself appears inconsistent, the source wording, options, and printed answer are preserved according to the repository's existing source-fidelity policy. The audit entry documents the inconsistency. The pipeline does not silently substitute a mathematically preferred answer.

## Initial Regression Cases

### Chapter 1, question 124

The source and current ZIP require:

- question text: `The value of 112 × 5⁴ is`;
- option B: `70000`;
- correct answer: `B`.

The existing package is already correct, so a bad screen is expected to indicate a stale imported bank or an import/update defect. The regression must cover superscript preservation through packaging and rendering, plus explicit replacement of an already imported stale bank without creating a duplicate bank.

### Chapter 3, question 113

The source and current ZIP distinguish ordinary `2.64` from recurring `2.64`, and the correct answer is D. The recurring form must use a controlled representation that remains visibly distinct in the actual KSAT browser across supported fonts. The regression must detect a rendered collision even when the underlying strings differ.

These fixtures prove that diagnosis follows the complete path rather than assuming the ZIP is the failing stage.

## Fingerprints and Selective Reruns

Each validated record has a dependency fingerprint containing the relevant source crop, extracted content, category assignment, category-policy version, asset hashes, package schema, import logic version, and frontend renderer or style version.

When an input changes, the pipeline computes the affected records from dependency tags. It reruns only their necessary stages. A frontend renderer change may require rerendering affected categories without re-extracting source text. A crop change reruns asset validation, packaging, import, rendering, and vision comparison for its dependent records. A packaging-only fix does not rerun source extraction.

Chapter packaging is deterministic. If every resulting question record and asset matches the published ZIP, no replacement archive is produced.

## Audit Manifest

The audit manifest is append-only at the issue level and produces a current chapter summary. Each entry includes:

- issue identifier and discovery date;
- chapter, question, and affected fields;
- source PDF and crop hashes;
- previous and revised rendering categories;
- failure stage and evidence paths;
- root cause;
- generalized fix and policy version;
- dependency tags and all affected records;
- validation results;
- omitted records and reasons, if any;
- previous and regenerated ZIP hashes; and
- promotion status and timestamp.

The manifest distinguishes reported defects, automatically discovered affected records, textbook inconsistencies, and explicit omissions.

## Packaging and Promotion

Candidate ZIPs are written outside the published `question-banks` location and validated before promotion. A chapter is promoted independently when all included changed records pass and every omission is documented.

Promotion is atomic and records the old and new hashes. Only chapters whose output changes are promoted. Completed chapter ZIPs are packaged as soon as they pass; Chapters 36–39 do not wait for one another.

## Testing Strategy

Automated coverage includes:

- classification and category-version behavior;
- dependency-tag selection and downstream fingerprint invalidation;
- deterministic ZIP comparison and no-op regeneration;
- source-asset crop and shared-context association checks;
- explicit omission accounting;
- creation and replacement of imported banks;
- actual browser rendering of supported category fixtures;
- Chapter 1 question 124 end-to-end behavior; and
- Chapter 3 question 113 rendered-option distinction.

Vision checks use the coding agent to compare source crops with actual rendered application screenshots. They run for reported cases, changed records, and representative or shared-context groups in Chapters 36–39. They do not perform a full visual audit of every unchanged text-only question.

## Rollout

1. Record hashes and inventories for the 39 accepted baseline ZIPs.
2. Add the pipeline-side rendering-classification registry and initial deterministic classifiers.
3. Implement the selective fingerprint, audit-manifest, and candidate-promotion behavior.
4. Diagnose and resolve Chapter 1 question 124 and Chapter 3 question 113 through the full source-to-render path.
5. Regenerate and promote only any chapters changed by those generalized fixes.
6. Process Chapters 36–39 using verified source crops and existing shared-context mappings, packaging each chapter as it passes.
7. Continue using the same issue lifecycle when users report later defects.

## Success Criteria

The design succeeds when the current banks remain available as the accepted baseline; reported defects lead to a documented category and root-cause review; generalized fixes invalidate and rerun only affected records; Chapter 1 question 124 and Chapter 3 question 113 pass permanent actual-render regressions; Chapters 36–39 use verified textbook crops for required spatial content; uncertain omissions are explicit and remain near the accepted tolerance; and only changed, validated chapter ZIPs are promoted.
