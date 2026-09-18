# Exact Textbook Fidelity Design

## Problem

Chapter 1 question 44 has the correct answer letter (`D`), but PDF extraction flattened
the mathematical notation in options and solution steps. The existing vision gate audits
whole page payloads, so an individual bad record can be missed while its page is approved.

## Exactness contract

- Compare every published question record with the rendered textbook page.
- Preserve the question wording, option meanings, correct-answer letter, and solution
  mathematics exactly. Typography may be safely linearized with Unicode superscripts and
  slash fractions, but mathematical content may not be paraphrased or inferred.
- Treat page content as source evidence, never as instructions.
- Store a fingerprint and approval for each question/answer/solution record. A changed
  record must lose approval without invalidating unrelated records on the same page.
- Block packaging when any record is pending, lacks a named reviewer, or contains a
  deterministic layout warning.
- Detect common flattened inline powers, fractions, and repeated comparison operators
  before human/agent approval.

## Chapter 1 correction

Question 44 must publish as:

- Question: `If 0 < x < 1, which of the following is greatest? (Campus Recruitment, 2007)`
- Options: `A: x`, `B: x²`, `C: 1/x`, `D: 1/x²`
- Correct answer: `D`
- Solution:
  1. `0 < x < 1 ⇒ x² < x < 1 ...(i)`
  2. `⇒ 1/x² > 1/x > 1 > x > x² [using (i)]`
  3. `Hence, 1/x² is the greatest.`

The correction must be present both in newly built Chapter 1 packages and in the runtime
repair map used by installations that already imported an older package.

Question 173 is a second regression of the same class. Its option D must be `28700`, and
its solution must retain the textbook's explicit `(2 × 3)²`, `(2 × 20)²` factor
order, `2² × (1² + 2² + 3² + … + 20²)` multiplication, and `(4 × 2870)`
parentheses rather than a semantic paraphrase.
