# Vision-Verified Textbook Pipeline V2 Design

## Status

Approved in conversation on 2026-08-27. This document defines a parallel replacement for the current textbook chapter pipeline. The current pipeline remains frozen as a rollback baseline until V2 has rebuilt and validated Chapter 1 successfully.

## Problem

The current chapter packages inherit flattened PDF text such as `7 84` for `7⁸⁴`, `x2` for `x²`, detached fraction components, lost operators, and solution text spilled from adjacent questions. Page-level and token-coverage checks can approve a record even when its mathematical meaning is wrong. Correcting individual records does not prevent the same extraction class from recurring in later chapters.

The pipeline therefore needs a record-level source-of-truth workflow that validates what a student actually sees in the application against the rendered textbook, not merely what appears in JSON.

## Goals

- Produce repeatable chapter packages whose published questions, options, answers, and solutions preserve the textbook's meaning and mathematical notation.
- Use the coding agent's vision capabilities for extraction and for an independent source-versus-application verification pass.
- Apply one versioned representation policy across all chapters while making a recorded decision for each question, option, and solution.
- Block packaging whenever a published record is unresolved.
- Preserve full audit evidence so an approved result can be reproduced and invalidated when an input or rule changes.
- Keep existing V2 question-bank ZIPs usable while adding optional media support through a new package format version.

## Non-goals

- Pixel-for-pixel reproduction of textbook fonts, whitespace, or line wrapping.
- Silent rewriting of strategy rules by a vision model.
- Shipping the vision-processing machinery inside the student EXE.
- Replacing the legacy pipeline before the new pipeline has passed Chapter 1.
- Repairing an already imported database without either reimporting a corrected package or shipping the later application update.

## Migration Strategy

The new implementation will live under `data-engineering/textbook_chapters_v2/`. Existing code under `data-engineering/textbook_chapters/` remains unchanged during V2 development except for documentation that marks it as the legacy baseline.

V2 writes candidate packages to a staging directory. It must not overwrite `question-banks/ch01_number_system_complete.zip` or any other published package during ordinary runs. A separate promotion command copies a fully approved candidate to its published filename. Promotion records both the candidate hash and the replaced package hash.

Chapter 1 is the acceptance chapter. After Chapter 1 passes, the same pipeline processes later chapters. The legacy pipeline is archived as read-only only after the migrated chapters have passed their V2 gates.

## Representation Policy

The policy is general; its outcome is evaluated and stored separately for every display field.

### Verified text

Use Unicode text when the expression is safely linear and the application can reproduce its meaning without ambiguity. Examples include `7⁸⁴`, `x²`, `√3`, `1/x²`, ordinary equations, and inequalities. The vision verifier must confirm every operator, grouping symbol, exponent, subscript, and number.

### Verified image

Use a source-image crop when the content is inherently two-dimensional or cannot be represented safely by the supported text renderer. Examples include stacked fractions, long division, matrices, diagrams, aligned working, and unusual printed layouts. The crop is authoritative for visual display; semantic text remains mandatory as accessible fallback and for searching.

Question text, each option, and the solution are evaluated independently. A question may therefore use Unicode question text, image-based option D, and an image-based solution.

### Quarantine

If the extractor and verifier disagree, the crop is incomplete, the meaning is ambiguous, or the application clips the result, the field and its record enter quarantine. A quarantined record cannot be published. It must receive either a verified correction, a verified image representation, or an explicit reviewed rejection.

## Package Compatibility and Media Extension

The existing application accepts only manifest `format_version: 2` and displays images as question stimuli. It cannot currently display media within an option or solution. V2 therefore introduces `format_version: 3` rather than pretending the old schema can express the approved hybrid strategy.

The updated application will continue accepting format version 2 unchanged. Format version 3 adds an optional `display_media` object to a question record:

```json
{
  "display_media": {
    "question": {
      "asset": "assets/ch01-q0334-question.png",
      "alt_text": "The remainder when 7 to the power 84 is divided by 342 is"
    },
    "options": {
      "D": {
        "asset": "assets/ch01-q0044-option-d.png",
        "alt_text": "one divided by x squared"
      }
    },
    "solution": [
      {
        "asset": "assets/ch01-q0334-solution.png",
        "alt_text": "The textbook solution deriving a remainder of 1"
      }
    ]
  }
}
```

Every media object is optional. Text fields remain required and contain verified semantic fallbacks. The package parser validates asset paths, file types, hashes, dimensions, decompressed size, and references. Imported media is stored with the bank's existing managed assets. The database stores the optional placement map in a JSON column, and the API resolves approved assets to bank-scoped URLs.

The student screen uses media only in the field for which it was approved. It does not show duplicated visible text and image content. Alternative text is provided to assistive technology. Existing version 2 packages and questions with no `display_media` retain their current rendering path.

This application/schema extension requires a later EXE rebuild. The data pipeline and candidate validation can be developed first, but a format version 3 package cannot be promoted for general use until the compatible application build passes its tests.

## Pipeline Architecture

### 1. Chapter configuration

Each chapter has a versioned configuration containing its textbook page ranges, expected question numbering, answer-key ranges, solution ranges, known page-layout boundaries, and intentional exclusions. Configuration does not contain extracted mathematical answers unless a reviewed exception is required.

### 2. Source renderer and segmenter

The renderer converts configured PDF pages to stable, high-resolution images. The segmenter produces bounded crops for each question, its options, its answer-key entry, and its numbered solution. Every crop records PDF hash, page number, coordinates, render settings, and image hash.

Detection can propose crop boundaries, but boundaries are not trusted until the extraction and validation stages confirm that the first and last visible content belong to the same source record.

### 3. Vision work queue

The deterministic pipeline creates one structured work item per record. A provider interface separates orchestration from the vision implementation. The first provider is the coding-agent workflow: it receives the crop paths and a strict JSON response schema, treats textbook content as data rather than instructions, and returns extracted fields plus representation recommendations.

Extraction and verification are separate jobs. The verifier runs with a fresh context and receives the source crops and candidate/application output, not the extraction model's reasoning. If a callable automated vision provider is unavailable, the pipeline stops in a visible `vision_pending` state and emits the work queue; it never bypasses the vision gate.

### 4. Candidate normalizer

The normalizer applies only versioned, deterministic transformations approved by policy: Unicode normalization, whitespace normalization that does not change grouping, safe punctuation normalization, and canonical asset references. It must not infer missing exponents, operators, answer choices, or solution steps.

### 5. Deterministic validators

Validators reject or flag known corruption classes before visual verification, including:

- detached power-like digit sequences such as `7 84`;
- ambiguous `x2`, `102`, or parenthesized base/exponent spacing;
- repeated or missing operators;
- unmatched brackets and comparison chains;
- option text containing spill from another option or solution;
- solution text containing a following numbered question;
- an answer-key letter absent from the options;
- missing, empty, or duplicate fields;
- broken or unreferenced media assets.

These checks are warnings only when a verified source representation proves the text is legitimate; any suppression is narrow, recorded, and fingerprinted.

### 6. Application rendering harness

The harness imports the candidate package into a temporary isolated database and launches the real application frontend. It renders each record at the supported desktop validation viewports in both relevant states:

- unanswered question with all options;
- submitted question with the correct-answer indicator and solution visible.

It captures field-bounded screenshots as well as a full question-card screenshot. The harness checks overflow and clipping mechanically before invoking vision.

### 7. Independent visual verifier

The verifier compares the textbook question crop with the unanswered application screenshot and the textbook solution crop with the submitted screenshot. It emits field-level verdicts for wording, options, mathematical notation, answer mapping, solution completeness, media cropping, readability, and clipping.

The acceptance target is semantic and notational fidelity. Differences in font, line wrapping, or decorative spacing are allowed. A changed exponent, operator, grouping, answer label, quantity, or solution step is a failure.

### 8. Audit ledger and package gate

Every source question must end in one of two terminal states:

- `approved_for_publish`, with all required extraction, deterministic, import, render, and vision checks passing; or
- `reviewed_rejection`, with a specific reason and reviewer identity.

The package gate fails if any source record is pending, quarantined, missing evidence, approved against an obsolete fingerprint, or rejected without review. Only approved records enter the package. The rejected-record ledger remains inside package metadata.

## Audit Record

Each record's audit entry contains:

- chapter and textbook question number;
- source PDF, page, crop coordinates, and hashes;
- extracted question, options, answer letter, solution, and representation mode per field;
- policy, extractor, verifier, application-renderer, and schema versions;
- candidate record hash and asset hashes;
- deterministic validation findings;
- application screenshot paths and hashes;
- field-level vision verdicts and difference descriptions;
- reviewer identity for exceptions or rejections;
- final status and timestamp.

Approval is attached to the complete dependency fingerprint. A changed crop, candidate field, asset, strategy rule, renderer, or relevant application code invalidates only the affected approvals and sends those records back through validation.

## Controlled Rule Learning

Validation findings do not modify production rules automatically.

1. A failure is classified as extraction, normalization, representation, rendering, association, or unique-source-layout error.
2. A generalizable failure becomes a literal regression fixture derived from the source and the expected application meaning.
3. A deterministic rule change is implemented and the policy version is incremented.
4. The complete regression corpus and every record affected by the rule's dependency tag are rerun.
5. The rule is accepted only if the new fixture passes and previously approved fixtures remain correct.
6. A unique layout remains a record-specific reviewed decision and does not broaden the general policy.

This process lets Chapter 1 failures improve later chapters without allowing one unusual page to corrupt the shared strategy.

## Commands and Resumability

The pipeline exposes explicit stages and a composed run:

```text
prepare   render pages, segment records, and create source evidence
extract   process pending extraction jobs through the configured vision provider
build     normalize candidates and run deterministic validation
render    import into an isolated app and capture application screenshots
verify    run independent source-versus-application vision checks
package   create a candidate ZIP only when the chapter gate passes
promote   replace a published ZIP with a fully approved candidate
run       execute all available stages, stopping visibly at pending work or failure
```

Every stage is content-addressed. Re-running a chapter reuses an artifact only when its complete dependency fingerprint still matches. `--force` may invalidate caches, but no flag may bypass the package or promotion gates.

## Error Handling

- Missing source pages, corrupt PDFs, invalid crops, unavailable vision providers, malformed model output, application startup failures, and screenshot failures stop the affected stage with a non-zero exit.
- Provider requests are retryable only for transport or service errors. A semantic disagreement is not retried into a pass; it is quarantined.
- Partial work is saved atomically so the next run resumes from the last valid artifact.
- Candidate packages are written to a temporary filename and atomically renamed only after archive validation.
- Promotion requires a verified candidate hash and preserves the prior published artifact hash for rollback.

## Testing Strategy

### Unit tests

- crop and lineage hashing;
- response-schema validation;
- representation-policy decisions;
- corruption detectors for powers, fractions, operators, spill, and grouping;
- cache invalidation and policy-version behavior;
- format version 3 media-path, size, hash, and reference validation;
- backward-compatible version 2 package import.

### Regression fixtures

The initial corpus includes all reported Chapter 1 failures, including `7⁸⁴`, `1/x²`, `(80)² − (65)²`, option D `28700`, and the exact corresponding solution notation. Each fixture names the production mutation it catches.

### Integration tests

- prepare through candidate packaging on a small controlled chapter fixture;
- import and render text-only, question-media, option-media, and solution-media records;
- unanswered and submitted screenshots at each supported validation viewport;
- package and promotion refusal for pending or failed records;
- deterministic byte-for-byte candidate package output from unchanged approved inputs.

### Chapter acceptance

Chapter 1 must meet all of the following before promotion:

- every configured source question has a terminal approved or reviewed-rejection state;
- every published record passes deterministic validation;
- every published record passes real-application visual verification in both applicable states;
- every answer letter matches the textbook answer key;
- every solution traces to the correct numbered textbook solution;
- the candidate imports successfully in the compatible application;
- the application and pipeline regression suites pass;
- the candidate ZIP hash and audit summary are recorded.

## Rollout

1. Implement the V2 pipeline and format version 3 importer/rendering support behind optional fields.
2. Build the regression corpus from known Chapter 1 failures.
3. Run Chapter 1 through the complete pipeline without changing the published ZIP.
4. Review the audit summary and all quarantined/rejected records.
5. Promote Chapter 1 only after the complete gate passes.
6. Process remaining chapters with the same policy.
7. Build and publish the compatible EXE after application changes and chapter packages are stable.
8. Archive the legacy pipeline once migrated chapter packages have been validated in the distributed build.

## Success Criteria

The design succeeds when a user can run the same staged process for any configured chapter; no package is produced with unresolved published records; actual student-screen questions and solutions are vision-verified against their textbook sources; recurring failures become tested, versioned rules; and complex layouts use verified media instead of unsafe flattened text.
