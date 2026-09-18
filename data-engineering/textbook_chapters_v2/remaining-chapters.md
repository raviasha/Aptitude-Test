# Remaining all-vision text-only chapter inventory

Source: the PDF configured by `configs/chapter-015.json`, SHA-256
`0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf`.
Inventory checked on 2026-09-09. Physical PDF page numbers are printed page
numbers plus 9. The source has 961 physical pages.

Continue the established extraction, independent source/application vision
verification, and format-v3 text-only packaging workflow. Existing Chapters
1-15 are read-only. Save each new chapter separately after its own release
gates pass. An inventory count is not a record-level fidelity approval.

## Source inventory

| Chapter | Title | Physical pages | Objective count | Separate Data Sufficiency count | Objective key pages |
| --- | --- | --- | ---: | ---: | --- |
| 16 | Pipes and Cisterns | 519-534 | 54 | 7 | 527 |
| 17 | Time and Work | 535-570 | 133 | 21 | 550 |
| 18 | Time and Distance | 571-608 | 191 | 12 | 591 |
| 19 | Boats and Streams | 609-620 | 36 | Embedded Q37; separate 11 | 614 |
| 20 | Problems on Trains | 621-641 | 74 | 26 | 629 |
| 21 | Alligation or Mixture | 642-649 | 26 | None found | 646 |
| 22 | Simple Interest | 650-671 | 106 | 12 | 661 |
| 23 | Compound Interest | 672-696 | 69 | 27 | 682 |
| 24 | Area | 697-774 | 416 | 41 | 735-736 |
| 25 | Volume and Surface Area | 775-822 | 304 | 16 | 800-801 |
| 26 | Races and Games of Skill | 823-827 | 25 | None found | 826 |
| 27 | Calendar | 828-831 | 18 | None found | 830 |
| 28 | Clocks | 832-842 | 54 | None found | 838 |
| 29 | Stocks and Shares | 843-849 | 30 | None found | 848 |
| 30 | Permutations and Combinations | 850-858 | 48 | None found | 855 |
| 31 | Probability | 859-869 | 50 | None found | 865 |
| 32 | True Discount | 870-874 | 21 | None found | 872 |
| 33 | Banker's Discount | 875-878 | 13 | None found | 877 |
| 34 | Heights and Distances | 879-885 | 18 | None found | 883 |
| 35 | Odd Man Out and Series | 886-892 | 96 | None found | 889 |
| 36 | Tabulation | 896-913 | I:25; II:35; III:25 | Separate exercise structure | 899; 905; 912 |
| 37 | Bar Graphs | 914-931 | I:31; II:25; III:25 | Separate exercise structure | 918; 924; 930 |
| 38 | Pie Chart | 932-945 | I:29; II:24; III:23 | Separate exercise structure | 935; 939; 944 |
| 39 | Line Graphs | 946-961 | I:28; II:15; III:25 | Separate exercise structure | 949; 955; 959 |

Pages 893-895 contain the section transition, not a missing chapter.

## Scope and source issues

- Separate Data Sufficiency exercises remain outside the objective bank scope.
  When their numbering restarts, record them as excluded sections with their
  actual printed numbers, not invented continuation question IDs.
- Chapter 29 Q30 and its solution exist, but the answer-key entry is blank.
  Keep this unresolved until source adjudication; do not invent key evidence.
- Chapter 37 Exercise I Q26-27 and their solutions exist, but the answer key
  skips them. These also need explicit source adjudication before packaging.
- Chapter 22 prints key entry `105 (b)` without a period. Marker discovery must
  not mistake that formatting difference for a missing source entry.
- Chapter 23 Q69 has four printed choices labelled (a), (b), (c), (e), with
  answer key (a). The application's bank contract uses ABCD or ABCDE. Preserve
  this source-label issue explicitly; do not invent a missing choice or silently
  change the printed labels. Root visually confirmed both question crops and
  key682. Planned representation: four internal application choices A-D, with
  the original `(e)` retained literally in the fourth choice text, `(e) None of
  these`. The choice values/order and correct answer A remain unchanged. This
  preserves the printed label without fabricating a fifth option; the final
  candidate and its renders must still pass independent verification.
- Chapters 24-25 have two-page objective keys. Legacy-bank totals are not
  authoritative; for example, the source Chapter 36 contains 85 questions,
  not the legacy extraction's 105.
- Chapters 24, 25, 28, 34, and 36-39 need special checks for diagrams, shading,
  clock faces, geometry, and shared chart/table data. Text-only representations
  must preserve all information required to answer the question; otherwise
  record a reviewed exclusion, never silently remove the visual information.
- Chapter 21 solutions include alligation diagrams that need a faithful
  software-renderable text representation.

## Execution order

Start with Chapters 16-20, save each passing ZIP immediately, then continue in
chapter order. Preserve every source, extraction, render, and verification
fingerprint. Repair only affected records; never bypass the audit gates.

## Progress and restart notes (2026-09-09)

- Chapter 16: 54 prepared objective records and 54 unique extraction jobs.
  Source bindings checked independently (179 crop bindings). All 54 vision
  transcriptions completed and were ingested; all 54 application renders passed.
  All 54 verification records accepted; eight individual visual adjudications
  document six helper-screenshot artifacts, decorative step numbering, and one
  source-punctuation false positive. ZIP packaged and independently imported:
  `ch16_pipes_cisterns_all_vision_text_only.zip`, 54 questions, no images.
  SHA-256: `1a63961783ff851b6d4c7bbe04c1ffd55c56d52a383af0522195859962860668`.
- Chapter 17: 133 prepared objective records and 133 unique extraction jobs.
  Source bindings checked independently (435 crop bindings). All 133 vision
  transcriptions ingested, rendered, independently reviewed, and approved.
  Source-backed readability repairs for Q28 fraction grouping and Q46 mixed
  number passed fresh renders and independent verification. Eight individual
  helper-capture adjudications retain original findings and evidence in tmp017.
  ZIP packaged and independently imported: 133 questions, no images/rejections.
  `ch17_time_work_all_vision_text_only.zip`
  SHA-256: `a66f9616eee0c3486b61b20881947e071332455b2c09c004f8b2882e7f02cd70`.
- Chapter 18: 191 records and unique jobs ready; root checked all 631 crop hashes.
  All 191 vision transcriptions ingested, built, and rendered. Independent
  verification started with prior tool approval, then was stopped after the
  Chapter22 transfer-approval rejection. Seven results were saved; all later
  external jobs were interrupted. Await explicit user transfer approval before
  resuming; no ZIP is published for this chapter.
- Chapter 19: 36 prepared objective records; delegated full extraction,
  independent verification, and package gates. All 36 transcriptions completed;
  all 36 rendered and independently verified, with no content repairs or
  adjudications. ZIP packaged and independently imported:
  `ch19_boats_streams_all_vision_text_only.zip`, 36 questions, no images.
  SHA-256: `56dd3c04cb9b46487374cc56de0c4dc18a82e2f1657d67ca659213b3ad99bdf1`.
  Embedded Data Sufficiency Q37
  and the separate restarted DS exercise 1-11 are explicitly outside scope.
- Chapter 20: 74 records/jobs ready, all 236 crop bindings root-checked;
  all 74 transcribed, ingested, rendered, independently reviewed, and approved.
  Three individual adjudications document decorative numbering/helper captures.
  ZIP packaged and independently imported: 74 questions, no images or rejections.
  `ch20_problems_on_trains_all_vision_text_only.zip`
  SHA-256: `bf2b459738815f510d5b0f783843ad56d059ed26a6f71b0d4b8263340b5dfec4`.
- Chapter 21: 26 records/jobs ready, all 86 crop bindings root-checked;
  all 26 transcriptions ingested, rendered, and verified. Two individual visual
  adjudications document a key-row misread and a helper-screenshot artifact.
  ZIP packaged and independently imported:
  `ch21_alligation_mixture_all_vision_text_only.zip`, 26 questions, no images.
  SHA-256: `9f6612586557443ccf631327dda01de1dc315e1adb56d00b6570e6f32c46470d`.
- Chapter 22: 106 source records/jobs prepared, all 336 crop bindings root-checked,
  all 106 transcribed, rendered, independently reviewed, and approved after the
  user authorized external vision transfer. Ten individual source/full-card
  adjudications preserve two printed inconsistencies and eight capture artifacts.
  ZIP independently imported: 106 questions, no images/media/rejections.
  `ch22_simple_interest_all_vision_text_only.zip`
  SHA-256: `b1434f02f49d5507d0fe12371d38f65e4b1040e6b411e5ff5a16732dacdfb97b`.
- Chapter 23: 69 records/jobs prepared, all 219 crop bindings root-checked;
  all 69 transcribed, rendered, independently reviewed, and approved. The initial
  browser timeout was diagnosed and the full normal render subsequently passed.
  Q66's genuine principal-grouping error was corrected and freshly verified;
  printed later punctuation was retained. Q69's printed fourth `(e)` label remains
  visible in internal slot D and passed independent review. ZIP imported through
  the actual app parser: 69 questions, no images/media/rejections.
  `ch23_compound_interest_all_vision_text_only.zip`
  SHA-256: `35d7f712e661e960d04e07c2c51c9a74497b3f2aa6cfeff63ca1894ad8830f3a`.
- Chapter 24: source/config preparation underway, including diagrams and the
  two-page objective answer key.
- Source preparation must check all first-line fractions: the generic marker
  generator can clip numerators above the printed question/solution number.
  Also review mixed-column starts and final-record continuations. Store specific
  boundary decisions in the chapter config or its `boundary-review/` folder.
- Runtime check: all 118 V2 pipeline tests passed, including actual application
  browser rendering, with the original Python 3.14 installation and Playwright
  1.62.0. The bundled Python 3.12 can prepare crops and run vision queues but
  lacks Python Playwright; do not use it for application renders.
  A second complete 118-test regression run also exited successfully after the
  five new ZIPs were built; no application/pipeline code was changed by this task.

Render/ingest/package runtime:

```powershell
$env:PYTHONPATH = 'data-engineering;tmp/app-contract-deps'
& 'C:/Users/ravis/AppData/Local/Python/pythoncore-3.14-64/python.exe' -m textbook_chapters_v2 <stage> --config data-engineering/textbook_chapters_v2/configs/chapter-016.json
```

The existing extraction/verification queue scripts use authenticated Codex CLI
in independent, read-only model contexts. Run with two workers and resume by
fingerprint. Do not edit a handed-off source config while its queue is running.
After extraction: ingest, build, render, generate the separate verification
queue, ingest actual verdicts, then package only if all release gates pass.
Prepared records and extracted text are not fidelity approvals.

## External verification authorization pause (2026-09-09)

Auto-review rejected Chapter22's next authenticated vision runner because it
requires explicit authorization to send textbook source crops and application
screenshots to the external Codex service. Do not retry through another agent,
runtime, or wrapper. The user must approve this transfer before further external
vision extraction/verification. Chapter18's already-approved runner was stopped;
Chapter23 local render diagnosis and Chapter24 source prep may finish without
external transmission. Completed ZIPs16,17,19,20,21 remain available and validated.
