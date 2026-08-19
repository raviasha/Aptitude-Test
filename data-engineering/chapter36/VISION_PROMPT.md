# Vision analysis contract

Analyze exactly one source page at a time. Do not use a nearby page merely
because it is close.

For the supplied page image and candidate question records:

1. Read every printed question on the page before selecting any visual.
2. Match candidate records to printed questions by meaning and printed number.
   Reject records that are not present on the page.
3. Find the smallest complete visual region needed to answer each matched
   question. Include table/chart titles, units, axes, legends, notes, and every
   data row or column required by the question.
4. Exclude question bodies, answer choices, answer keys, solutions, headers,
   footers, and unrelated visuals from the crop.
5. Complete same-page associations first. If a required graph/table is on
   another page, mark the question `cross_page_visual` without guessing the
   other page.
6. Reuse one visual ID for multiple same-page questions only when they require
   the same complete visual.

After all pages are analyzed, run a cross-page resolution pass. It may resolve
`cross_page_visual` only when a printed question-set continuation, matching set
number, and matching visual title prove the relationship. Record every source
page. If the complete stimulus spans pages, return separate crop regions that
the pipeline can combine. Never attach a page based only on proximity.

Return JSON compatible with `page-analysis.json`. Every visual must contain:

- a stable `id`;
- `source_page` and `source_file`, or `source_regions` for a multi-page visual;
- `exercise` and `question_numbers`;
- `crop_box` as `[left, top, right, bottom]` in source-image pixels;
- `question_content_starts_at_y`, proving the crop stops before question text;
- a human-readable `title` and `alt_text`.

Never infer association from nearest-page distance, reading-order proximity,
or a generic `image_reference` string. Page identity plus question identity is
the join key, and every cross-page join requires explicit continuation evidence.

## Answer and solution extraction contract

The candidate record's answer, explanation, solution steps, and option
explanations are untrusted. Do not calculate a replacement or choose an answer
from the visual.

1. Read the textbook's printed `ANSWERS` panel for the same exercise and record
   the letter for the exact printed question number.
2. Read the matching numbered entry under the textbook's printed `SOLUTIONS`
   heading. Preserve its values, operations, units, approximation, and result.
3. Record the answer-key source page and every solution source page. If a
   solution continues on the next page, include both pages.
4. Reject a question when either the printed answer or the complete printed
   solution cannot be found. Never fill the gap with model reasoning.
5. Do not publish model-generated option explanations. The textbook answer key
   and numbered solution are the only accepted answer/solution sources.

Return records compatible with `textbook-solutions.json`. Exercise plus printed
question number is the join key; page proximity is never sufficient.
