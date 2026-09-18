from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
import json
import copy
import hashlib

from PIL import Image


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


class MarkerReviewTests(unittest.TestCase):
    def test_explicit_page_continuation_is_preserved_in_source_order(self) -> None:
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pairs = []
            for page, block in ((22, {"printed_number": 1, "top": 2100, "bottom": 2200}),
                                (23, {"printed_number": 1, "top": 270, "bottom": 350,
                                      "continuation": True})):
                grid = root / f"page-{page}.png"
                Image.new("RGB", (1530, 2340), "white").save(grid)
                review = root / f"page-{page}.json"
                review.write_text(json.dumps({
                    "exercise_id": "1A", "page": page,
                    "question_blocks": [block],
                    "answer_solution_blocks": ([{"printed_number": 1, "top": 500, "bottom": 550}]
                                               if page == 23 else []),
                    "reviewer_notes": "Explicitly reviewed continuation with remaining options."
                }), encoding="utf-8")
                pairs.append((review, grid))
            result = reviewed_exercise_markers(tuple(reversed(pairs)))
            self.assertEqual(result.marker_overrides["question"]["1"]["segments"], [
                {"page": 22, "left": 95, "top": 2100, "right": 1435, "bottom": 2200},
                {"page": 23, "left": 95, "top": 270, "right": 1435, "bottom": 350},
            ])

    def test_orphan_continuation_is_not_treated_as_a_complete_question(self) -> None:
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid, review = root / "page.png", root / "review.json"
            Image.new("RGB", (1530, 2340), "white").save(grid)
            review.write_text(json.dumps({
                "exercise_id": "1A", "page": 23,
                "question_blocks": [{"printed_number": 1, "top": 270, "bottom": 350,
                                     "continuation": True}],
                "answer_solution_blocks": [{"printed_number": 1, "top": 500, "bottom": 550}],
                "reviewer_notes": "Missing first page."
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "continuation"):
                reviewed_exercise_markers(((review, grid),))

    def test_duplicate_starts_on_different_pages_are_still_rejected(self) -> None:
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pairs = []
            for page in (22, 23):
                grid, review = root / f"{page}.png", root / f"{page}.json"
                Image.new("RGB", (1530, 2340), "white").save(grid)
                review.write_text(json.dumps({
                    "exercise_id": "1A", "page": page,
                    "question_blocks": [{"printed_number": 1, "top": 300, "bottom": 400}],
                    "answer_solution_blocks": [{"printed_number": 1, "top": 500, "bottom": 550}],
                    "reviewer_notes": "Two starts cannot be silently combined."
                }), encoding="utf-8")
                pairs.append((review, grid))
            with self.assertRaisesRegex(ValueError, "Duplicate question"):
                reviewed_exercise_markers(pairs)

    def test_coordinate_grid_preserves_image_dimensions_and_writes_hashable_png(self) -> None:
        from logical_reasoning_v2.marker_review import write_coordinate_grid

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            output = root / "grid.png"
            Image.new("RGB", (300, 240), "white").save(source)

            digest = write_coordinate_grid(source, output, interval=50)

            self.assertTrue(output.is_file())
            self.assertEqual(len(digest), 64)
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (300, 240))
                self.assertNotEqual(rendered.getpixel((50, 100)), (255, 255, 255))

    def test_reviewed_exercise_markers_create_matched_v2_segments(self) -> None:
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid_one = root / "page-022-grid.png"
            grid_two = root / "page-026-grid.png"
            Image.new("RGB", (1530, 2340), "white").save(grid_one)
            Image.new("RGB", (1530, 2340), "white").save(grid_two)
            one = root / "page-022-review.json"
            two = root / "page-026-review.json"
            one.write_text(
                json.dumps(
                    {
                        "exercise_id": "Exercise 1A",
                        "page": 22,
                        "question_blocks": [
                            {"printed_number": 1, "top": 1400, "bottom": 1470},
                            {"printed_number": 2, "top": 1480, "bottom": 1550},
                        ],
                        "answer_solution_blocks": [],
                        "reviewer_notes": "question rows",
                    }
                ),
                encoding="utf-8",
            )
            two.write_text(
                json.dumps(
                    {
                        "exercise_id": "1A",
                        "page": 26,
                        "question_blocks": [],
                        "answer_solution_blocks": [
                            {"printed_number": 1, "top": 400, "bottom": 460},
                            {"printed_number": 2, "top": 470, "bottom": 530},
                        ],
                        "reviewer_notes": "answer rows",
                    }
                ),
                encoding="utf-8",
            )

            result = reviewed_exercise_markers(
                ((one, grid_one), (two, grid_two)),
                internal_number_start=41,
            )

            self.assertEqual(result.exercise_id, "Exercise 1A")
            self.assertEqual(result.internal_numbers, (41, 42))
            self.assertEqual(result.printed_numbers, (1, 2))
            self.assertEqual(
                result.marker_overrides["question"]["41"]["segments"],
                [{"page": 22, "left": 95, "top": 1400, "right": 1435, "bottom": 1470}],
            )
            self.assertEqual(
                result.marker_overrides["answer_key"]["42"]["segments"],
                [{"page": 26, "left": 95, "top": 470, "right": 1435, "bottom": 530}],
            )
            self.assertEqual(
                result.marker_overrides["solution"]["41"],
                result.marker_overrides["answer_key"]["41"],
            )
            self.assertEqual(len(result.review_provenance), 2)

    def test_reviewed_exercise_markers_reject_unmatched_question_and_answer_sets(self) -> None:
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = root / "page-022-grid.png"
            Image.new("RGB", (1530, 2340), "white").save(grid)
            review = root / "page-022-review.json"
            review.write_text(
                json.dumps(
                    {
                        "exercise_id": "Exercise 1A",
                        "page": 22,
                        "question_blocks": [{"printed_number": 1, "top": 1400, "bottom": 1470}],
                        "answer_solution_blocks": [{"printed_number": 2, "top": 400, "bottom": 460}],
                        "reviewer_notes": "mismatch",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "do not match"):
                reviewed_exercise_markers(((review, grid),))


class RecoveryMarkerTests(unittest.TestCase):
    def region(self, page=29, left=100, top=400, right=700, bottom=450):
        return dict(page=page, left=left, top=top, right=right, bottom=bottom)

    def block(self, number=22, regions=None, continuation=False):
        return dict(printed_number=number, regions=regions or [self.region()],
                    continuation=continuation, start_question=not continuation)

    def result(self, page=29, questions=None, answers=None, contexts=None):
        ex = dict(exercise_id="1B", question_blocks=questions if questions is not None else [self.block()],
                  answer_solution_blocks=answers if answers is not None else [self.block(regions=[self.region(top=900, bottom=950)])],
                  shared_context_blocks=contexts or [])
        ex["answer_solution_blocks"] = [dict(block, start_question=False) for block in ex["answer_solution_blocks"]]
        return dict(job_id=f"page-{page}", job_fingerprint="reviewed-fingerprint", verdict="approved", notes="Visual review",
                    pages=[dict(page=page, coverage_complete=True, exercises=[ex],
                                observed_counts={key: len(ex[key]) for key in ("question_blocks", "answer_solution_blocks", "shared_context_blocks")})])

    def compile(self, results, **kwargs):
        from logical_reasoning_v2 import marker_review
        self.assertTrue(hasattr(marker_review, "reviewed_recovery_markers"), "Recovery adapter is missing")
        return marker_review.reviewed_recovery_markers(
            results, exercise_id="1B", page_sizes={29: (1530, 2340), 30: (1530, 2340),
                226: (1530, 2340), 227: (1530, 2340), 228: (1530, 2340)}, **kwargs)

    def test_page29_two_regions_keep_exact_coordinates_and_list_order(self):
        regions = [self.region(left=800, top=437, right=1400, bottom=500),
                   self.region(left=100, top=520, right=600, bottom=539)]
        markers, contexts = self.compile([self.result(questions=[self.block(regions=regions)])], internal_number_start=41)
        self.assertEqual(markers.marker_overrides["question"]["41"]["segments"], regions)
        self.assertEqual(markers.printed_numbers, (22,))
        self.assertEqual(contexts, {})

    def test_explicit_continuation_preserves_both_pages_without_filling_gap(self):
        first = self.result(questions=[self.block()], answers=[])
        second = self.result(page=30, questions=[self.block(regions=[self.region(page=30, top=100, bottom=140)], continuation=True)],
                             answers=[self.block(regions=[self.region(page=30, top=900, bottom=950)])])
        markers, _ = self.compile([first, second])
        self.assertEqual(markers.marker_overrides["question"]["1"]["segments"],
                         [self.region(), self.region(page=30, top=100, bottom=140)])

    def test_nonphysical_continuations_require_explicit_source_order(self):
        results = [self.result(page=p, questions=[self.block(regions=[self.region(page=p)], continuation=p != 226)],
                    answers=[self.block(regions=[self.region(page=p, top=900, bottom=950)])] if p == 227 else [])
                   for p in (226, 228, 227)]
        with self.assertRaises(ValueError):
            self.compile(results)
        markers, _ = self.compile(results, source_order=[226, 228, 227])
        self.assertEqual([s["page"] for s in markers.marker_overrides["question"]["1"]["segments"]], [226, 228, 227])

    def test_regions_can_explicitly_span_pages_without_continuation_inference(self):
        regions = [self.region(page=226), self.region(page=228), self.region(page=227)]
        result = self.result(page=226, questions=[self.block(regions=regions)],
                             answers=[self.block(regions=[self.region(page=226, top=900, bottom=950)])])
        with self.assertRaises(ValueError):
            self.compile([result])
        markers, _ = self.compile([result], source_order=[226, 228, 227])
        self.assertEqual(markers.marker_overrides["question"]["1"]["segments"], regions)

    def test_native_recovery_rectangles_use_containing_page(self):
        result = self.result()
        del result["pages"][0]["exercises"][0]["question_blocks"][0]["regions"][0]["page"]
        markers, _ = self.compile([result])
        self.assertEqual(markers.marker_overrides["question"]["1"]["segments"], [self.region()])

    def test_duplicate_and_incomplete_source_order_rejected(self):
        for order in ([29, 29], [30], [True], []):
            with self.subTest(order=order), self.assertRaises(ValueError):
                self.compile([self.result()], source_order=order)

    def test_missing_flags_and_conflicting_role_flags_are_incomplete(self):
        for role, field, value in (("question_blocks", "continuation", None),
                                   ("question_blocks", "start_question", False),
                                   ("answer_solution_blocks", "start_question", True),
                                   ("answer_solution_blocks", "continuation", None)):
            result = self.result()
            block = result["pages"][0]["exercises"][0][role][0]
            if value is None:
                del block[field]
            else:
                block[field] = value
            with self.subTest(role=role, field=field), self.assertRaises(ValueError):
                self.compile([result])

    def test_expected_roster_detects_records_missing_from_both_roles(self):
        with self.assertRaises(ValueError):
            self.compile([self.result()], expected_printed_numbers=[22, 23])
        markers, _ = self.compile([self.result()], expected_printed_numbers=[22])
        self.assertEqual(markers.printed_numbers, (22,))

    def test_same_page_continuation_and_overlap_validation(self):
        pieces = [self.block(), self.block(regions=[self.region(top=500, bottom=550)], continuation=True)]
        markers, _ = self.compile([self.result(questions=pieces)])
        self.assertEqual(len(markers.marker_overrides["question"]["1"]["segments"]), 2)
        pieces[1]["regions"][0]["top"] = 440
        with self.assertRaises(ValueError):
            self.compile([self.result(questions=pieces)])

    def test_shared_duplicate_starts_and_orphans_rejected(self):
        context = dict(regions=[self.region(top=100, bottom=150)], printed_number=None,
                       continuation=False, start_question=False,
                       target_printed_number_start=22, target_printed_number_end=22)
        with self.assertRaises(ValueError):
            self.compile([self.result(contexts=[context, copy.deepcopy(context)])])
        context["continuation"] = True
        with self.assertRaises(ValueError):
            self.compile([self.result(contexts=[context])])

    def test_bad_regions_fail_closed(self):
        bad = [[], [self.region(bottom=400)], [self.region(left=-1)], [self.region(right=1600)],
               [self.region(page=31)], [self.region(top=True)], [self.region(), self.region(top=440, bottom=480)]]
        for regions in bad:
            with self.subTest(regions=regions):
                result = self.result()
                result["pages"][0]["exercises"][0]["question_blocks"][0]["regions"] = regions
                with self.assertRaises(ValueError):
                    self.compile([result])

    def test_blocked_unknown_incomplete_and_ambiguous_results_fail(self):
        base = self.result()
        variants = []
        for key, value in (("verdict", "blocked"), ("verdict", "unknown")):
            item = copy.deepcopy(base); item[key] = value; variants.append(item)
        item = copy.deepcopy(base); item["pages"][0]["coverage_complete"] = False; variants.append(item)
        item = copy.deepcopy(base); item["pages"][0]["observed_counts"]["question_blocks"] = 2; variants.append(item)
        item = copy.deepcopy(base); item["pages"][0]["exercises"][0]["exercise_id"] = "Unknown"; variants.append(item)
        item = copy.deepcopy(base); item["pages"][0]["exercises"][0]["question_blocks"][0]["printed_number"] = None; variants.append(item)
        item = self.result(answers=[]); variants.append(item)
        item = self.result(questions=[self.block(), self.block(regions=[self.region(top=600, bottom=650)])]); variants.append(item)
        item = self.result(questions=[self.block(continuation=True)]); variants.append(item)
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.compile([item])
        with self.assertRaises(ValueError):
            self.compile([base, copy.deepcopy(base)])

    def test_shared_visible_target_range_maps_to_internal_ids(self):
        shared = dict(top=100, bottom=150, printed_number=None, continuation=False, start_question=False,
                      target_printed_number_start=22, target_printed_number_end=23)
        result = self.result(questions=[self.block(), self.block(23, [self.region(top=600, bottom=650)])],
            answers=[self.block(regions=[self.region(top=900, bottom=950)]), self.block(23, [self.region(top=1000, bottom=1050)])],
            contexts=[shared])
        markers, contexts = self.compile([result], internal_number_start=41)
        self.assertEqual(markers.internal_numbers, (41, 42))
        self.assertEqual(contexts, {"question": {"exercise-1b-22-23": {
            "question_numbers": [41, 42], "segments": [dict(page=29, left=95, top=100, right=1435, bottom=150)]}}})
        result["pages"][0]["exercises"][0]["shared_context_blocks"][0]["target_printed_number_end"] = 24
        with self.assertRaises(ValueError):
            self.compile([result])

    def test_legacy_file_entrypoint_accepts_regions_and_keeps_image_provenance(self):
        from logical_reasoning_v2.marker_review import reviewed_exercise_markers
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); grid = root / "grid.png"; review = root / "review.json"
            Image.new("RGB", (1530, 2340), "white").save(grid)
            raw = self.result()["pages"][0]["exercises"][0]
            raw.update(page=29, reviewer_notes="Approved exact rectangles")
            review.write_text(json.dumps(raw), encoding="utf-8")
            markers = reviewed_exercise_markers([(review, grid)])
            self.assertEqual(markers.marker_overrides["question"]["1"]["segments"], [self.region()])
            self.assertEqual(len(markers.review_provenance[0]["grid_sha256"]), 64)

    def test_overlapping_context_targets_partition_into_merged_v2_groups(self):
        a, b = self.region(top=100, bottom=150), self.region(top=200, bottom=250)
        def context(start, end, regions):
            return dict(printed_number=None, regions=regions, continuation=False, start_question=False,
                        target_printed_number_start=start, target_printed_number_end=end)
        result = self.result(questions=[self.block(n, [self.region(top=400+n*100, bottom=450+n*100)]) for n in (1, 2, 3)],
            answers=[self.block(n, [self.region(top=900+n*100, bottom=950+n*100)]) for n in (1, 2, 3)],
            contexts=[context(1, 3, [a]), context(2, 3, [b])])
        _, contexts = self.compile([result], internal_number_start=41)
        self.assertEqual(list(contexts["question"].values()), [
            {"question_numbers": [41], "segments": [a]},
            {"question_numbers": [42, 43], "segments": [a, b]}])
        # Identical evidence in overlapping contexts is included exactly once.
        result["pages"][0]["exercises"][0]["shared_context_blocks"][1]["regions"] = [a, b]
        _, deduplicated = self.compile([result], internal_number_start=41)
        self.assertEqual(deduplicated, contexts)

    def test_merged_contexts_follow_explicit_nonphysical_page_order(self):
        a, b, c = self.region(page=226), self.region(page=228), self.region(page=227)
        shared = dict(printed_number=None, continuation=False, start_question=False,
                      target_printed_number_start=22, target_printed_number_end=23)
        result = self.result(page=226,
            questions=[self.block(22, [self.region(page=226, top=600, bottom=650)]), self.block(23, [self.region(page=226, top=700, bottom=750)])],
            answers=[self.block(22, [self.region(page=226, top=900, bottom=950)]), self.block(23, [self.region(page=226, top=1000, bottom=1050)])],
            contexts=[dict(shared, regions=[a, c]), dict(shared, target_printed_number_end=22, regions=[b])])
        _, contexts = self.compile([result], source_order=[226, 228, 227])
        selected = next(g for g in contexts["question"].values() if g["question_numbers"] == [1])
        self.assertEqual(selected["segments"], [a, b, c])

    def test_context_continuation_keeps_intervening_subgroup_in_source_order(self):
        a, b, c = self.region(top=100, bottom=150), self.region(top=200, bottom=250), self.region(page=30, top=100, bottom=150)
        shared = dict(printed_number=None, continuation=False, start_question=False,
                      target_printed_number_start=22, target_printed_number_end=23)
        first = self.result(
            questions=[self.block(), self.block(23, [self.region(top=600, bottom=650)])],
            answers=[self.block(regions=[self.region(top=900, bottom=950)]), self.block(23, [self.region(top=1000, bottom=1050)])],
            contexts=[dict(shared, regions=[a]), dict(shared, target_printed_number_end=22, regions=[b])])
        second = self.result(page=30, questions=[], answers=[], contexts=[dict(shared, regions=[c], continuation=True)])
        _, contexts = self.compile([first, second])
        selected = next(g for g in contexts["question"].values() if g["question_numbers"] == [1])
        self.assertEqual(selected["segments"], [a, b, c])

    def damaged_number_result(self):
        result = self.result(questions=[self.block(n, [self.region(top=400+i*100, bottom=450+i*100)])
                                       for i, n in enumerate((33, None, 35))],
            answers=[self.block(n, [self.region(top=900+i*100, bottom=950+i*100)]) for i, n in enumerate((33, 34, 35))])
        result.update(verdict="blocked", notes="One printed number is unreadable; all other evidence is complete.")
        return result

    def adjudication(self):
        return {(29, "1B", "question_blocks", 1): dict(printed_number=34, reviewer="visual-reviewer", reason="Reviewed the damaged source label.")}

    def clearance(self, result):
        remaining = []
        evidence = {}
        for page in result["pages"]:
            evidence[page["page"]] = dict(sha256="a" * 64, width=1530, height=2340,
                                        review_reference="review://pixel-evidence/29")
            for exercise in page["exercises"]:
                for role in ("question_blocks", "answer_solution_blocks"):
                    for index, block in enumerate(exercise[role]):
                        if block["printed_number"] is None:
                            remaining.append(dict(page=page["page"], exercise_id=exercise["exercise_id"],
                                role=role, block_index=index, field="printed_number"))
                for index, block in enumerate(exercise["shared_context_blocks"]):
                    for field in ("target_printed_number_start", "target_printed_number_end"):
                        if field in block and block[field] is None:
                            remaining.append(dict(page=page["page"], exercise_id=exercise["exercise_id"],
                                role="shared_context_blocks", block_index=index, field=field))
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return {result["job_fingerprint"]: dict(result_sha256=digest, reviewer="boundary-reviewer",
            reason="Reviewed every note and source region; only the listed null labels remain.",
            cleared_checks=["geometry", "coverage", "exercise_identity", "content_completeness", "other_notes"],
            remaining_nulls=remaining, other_blockers=[], source_evidence=evidence)}

    def test_reviewed_null_number_adjudication_retains_provenance_and_input(self):
        result = self.damaged_number_result()
        before = copy.deepcopy(result)
        with self.assertRaises(ValueError):
            self.compile([result])
        markers, _ = self.compile([result], adjudications=self.adjudication(), cleared_blockers=self.clearance(result))
        self.assertEqual(markers.printed_numbers, (33, 34, 35))
        self.assertEqual(result, before)
        proof = markers.review_provenance[0]
        self.assertEqual(proof["original_verdict"], "blocked")
        self.assertEqual(proof["notes"], result["notes"])
        self.assertEqual(proof["geometry_evidence_mode"], "external_pixel_review")
        self.assertEqual(proof["adjudications"], [dict(page=29, exercise_id="Exercise 1B", role="question_blocks",
            block_index=1, original_printed_number=None, printed_number=34,
            reviewer="visual-reviewer", reason="Reviewed the damaged source label.")])

    def test_adjudication_cannot_change_visible_numbers_or_skip_nulls(self):
        result = self.damaged_number_result()
        for key in ((29, "1B", "question_blocks", 0), (29, "1B", "question_blocks", 9),
                    (29, "1C", "question_blocks", 1), (29, "1B", "shared_context_blocks", 0)):
            adjudications = self.adjudication()
            adjudications[key] = dict(printed_number=34, reviewer="reviewer", reason="reviewed")
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.compile([result], adjudications=adjudications, cleared_blockers=self.clearance(result))
        result["pages"][0]["exercises"][0]["answer_solution_blocks"][1]["printed_number"] = None
        with self.assertRaises(ValueError):
            self.compile([result], adjudications=self.adjudication(), cleared_blockers=self.clearance(result))
        decisions = self.adjudication()
        decisions[(29, "Exercise 1B", "answer_solution_blocks", 1)] = dict(printed_number=34, reviewer="answer-reviewer", reason="Reviewed the answer label.")
        markers, _ = self.compile([result], adjudications=decisions, cleared_blockers=self.clearance(result))
        self.assertEqual(markers.printed_numbers, (33, 34, 35))
        self.assertEqual(len(markers.review_provenance[0]["adjudications"]), 2)
        result["verdict"] = "approved"
        with self.assertRaises(ValueError):
            self.compile([result], adjudications=decisions)

    def test_adjudication_requires_review_details_and_cannot_bypass_other_blockers(self):
        for field, value in (("reviewer", " "), ("reason", ""), ("printed_number", True)):
            adjudications = self.adjudication()
            adjudications[(29, "1B", "question_blocks", 1)][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.compile([self.damaged_number_result()], adjudications=adjudications)
        for fault in ("notes", "coverage", "counts", "identity", "regions", "no_null"):
            result = self.damaged_number_result()
            page = result["pages"][0]; exercise = page["exercises"][0]
            if fault == "notes": result.pop("notes")
            elif fault == "coverage": page["coverage_complete"] = False
            elif fault == "counts": page.pop("observed_counts")
            elif fault == "identity": exercise["exercise_id"] = "Unknown"
            elif fault == "regions": exercise["question_blocks"][0].pop("regions")
            else: exercise["question_blocks"][1]["printed_number"] = 34
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=self.clearance(result))

    def test_number_decision_alone_cannot_clear_unrelated_note_blockers(self):
        result = self.damaged_number_result()
        result["notes"] += " Also the option pixels are clipped."
        with self.assertRaises(ValueError):
            self.compile([result], adjudications=self.adjudication())
        for fault in ("other_blockers", "checks", "digest", "remaining", "reference"):
            clearance = self.clearance(result)
            entry = clearance[result["job_fingerprint"]]
            if fault == "other_blockers": entry["other_blockers"] = ["clipped options"]
            elif fault == "checks": entry["cleared_checks"].remove("geometry")
            elif fault == "digest": entry["result_sha256"] = "0" * 64
            elif fault == "remaining": entry["remaining_nulls"] = []
            else: entry["source_evidence"][29]["review_reference"] = ""
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=clearance)

    def test_pixel_binding_checks_hash_dimensions_and_records_clearance(self):
        result = self.damaged_number_result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            Image.new("RGB", (1530, 2340), "white").save(path)
            clearance = self.clearance(result)
            evidence = clearance[result["job_fingerprint"]]["source_evidence"][29]
            evidence["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            markers, _ = self.compile([result], adjudications=self.adjudication(),
                cleared_blockers=clearance, source_images={29: path})
            self.assertEqual(markers.review_provenance[0]["blocker_clearance"]["result_sha256"],
                             clearance[result["job_fingerprint"]]["result_sha256"])
            self.assertEqual(markers.review_provenance[0]["geometry_evidence_mode"], "bound_source_images")
            with self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=clearance, source_images={})
            Image.new("RGB", (100, 100), "white").save(path)
            with self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=clearance, source_images={29: path})
            evidence["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=clearance, source_images={29: path})

    def test_clearance_cannot_be_reused_after_notes_or_geometry_change(self):
        for fault in ("notes", "geometry"):
            result = self.damaged_number_result()
            clearance = self.clearance(result)
            if fault == "notes":
                result["notes"] += " Unresolved clipping remains."
            else:
                result["pages"][0]["exercises"][0]["question_blocks"][0]["regions"][0]["left"] = 101
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.compile([result], adjudications=self.adjudication(), cleared_blockers=clearance)

    def test_context_partial_orders_merge_topologically_and_cycles_fail(self):
        a, b, c = [self.region(top=t, bottom=t+40) for t in (100, 200, 300)]
        shared = dict(printed_number=None, continuation=False, start_question=False,
                      target_printed_number_start=22, target_printed_number_end=23)
        result = self.result(questions=[self.block(), self.block(23, [self.region(top=600, bottom=650)])],
            answers=[self.block(regions=[self.region(top=900, bottom=950)]), self.block(23, [self.region(top=1000, bottom=1050)])],
            contexts=[dict(shared, regions=[a, c]), dict(shared, target_printed_number_end=22, regions=[b, c])])
        _, contexts = self.compile([result])
        selected = next(g for g in contexts["question"].values() if g["question_numbers"] == [1])
        self.assertEqual(selected["segments"], [a, b, c])
        result["pages"][0]["exercises"][0]["shared_context_blocks"][1]["regions"] = [c, a]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.compile([result])


    def unnumbered_context_result(self):
        shared = dict(printed_number=None, regions=[self.region(top=100, bottom=150)],
            continuation=False, start_question=False, target_printed_number_start=None, target_printed_number_end=None)
        result = self.result(questions=[self.block(n, [self.region(top=400+n*100, bottom=450+n*100)]) for n in (1, 2, 3)],
            answers=[self.block(n, [self.region(top=900+n*100, bottom=950+n*100)]) for n in (1, 2, 3)], contexts=[shared])
        result.update(verdict="blocked", notes="Unnumbered directions have unresolved target endpoints only.")
        return result

    def context_decision(self, start=1, end=3, role="question"):
        return {(29, "1B", 0): dict(target_printed_number_start=start, target_printed_number_end=end,
            role=role, reviewer="context-reviewer", reason="Visually reviewed the scope of these directions.")}

    def compile_context(self, result, decisions=None, **kwargs):
        return self.compile([result], context_adjudications=self.context_decision() if decisions is None else decisions,
            expected_printed_numbers=kwargs.pop("expected_printed_numbers", [1, 2, 3]),
            cleared_blockers=kwargs.pop("cleared_blockers", self.clearance(result)), **kwargs)

    def test_context_adjudication_exercise_wide_and_subgroup_preserve_provenance(self):
        for start, end, role, targets in ((1, 3, "question", [41, 42, 43]), (2, 3, "solution", [42, 43])):
            result = self.unnumbered_context_result(); original = copy.deepcopy(result)
            markers, contexts = self.compile_context(result, self.context_decision(start, end, role), internal_number_start=41)
            self.assertEqual(list(contexts[role].values()), [dict(question_numbers=targets, segments=[self.region(top=100, bottom=150)])])
            self.assertEqual(result, original)
            self.assertEqual(markers.review_provenance[0]["context_adjudications"], [dict(page=29, exercise_id="Exercise 1B",
                block_index=0, original_target_printed_number_start=None, original_target_printed_number_end=None,
                **self.context_decision(start, end, role)[(29, "1B", 0)])])

    def test_context_adjudication_cannot_alter_visible_or_missing_endpoints(self):
        for field in ("target_printed_number_start", "target_printed_number_end"):
            for value in (1, "missing"):
                result = self.unnumbered_context_result()
                block = result["pages"][0]["exercises"][0]["shared_context_blocks"][0]
                if value == "missing": del block[field]
                else: block[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.compile_context(result)

    def test_context_adjudication_requires_valid_roster_range_role_and_reviewer(self):
        for start, end, roster in ((0, 3, [1, 2, 3]), (3, 2, [1, 2, 3]), (1, 4, [1, 2, 3]),
                                   (True, 3, [1, 2, 3]), (1, 3, [1, 3]), (1, 3, None)):
            with self.subTest(start=start, end=end, roster=roster), self.assertRaises(ValueError):
                self.compile_context(self.unnumbered_context_result(), self.context_decision(start, end), expected_printed_numbers=roster)
        for field, value in (("role", "unknown"), ("reviewer", " "), ("reason", "")):
            decisions = self.context_decision(); decisions[(29, "1B", 0)][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.compile_context(self.unnumbered_context_result(), decisions)

    def test_context_adjudication_missing_extra_or_wrong_block_decisions_fail(self):
        result = self.unnumbered_context_result()
        with self.assertRaises(ValueError): self.compile_context(result, {})
        for key in ((29, "1B", 1), (30, "1B", 0), (29, "1C", 0), (29, "1B", "shared_context_blocks", 0)):
            decisions = self.context_decision(); decisions[key] = copy.deepcopy(decisions[(29, "1B", 0)])
            with self.subTest(key=key), self.assertRaises(ValueError): self.compile_context(result, decisions)

    def test_context_adjudication_requires_exact_clearance_inventory_and_blocked_result(self):
        result = self.unnumbered_context_result()
        for fault in ("absent", "stale", "one_field", "extra_field", "other_blocker", "coverage", "approved"):
            value = copy.deepcopy(result); clearance = self.clearance(value)
            entry = clearance[value["job_fingerprint"]]
            if fault == "absent": clearance = {}
            elif fault == "stale": value["notes"] += " Changed evidence."
            elif fault == "one_field": entry["remaining_nulls"].pop()
            elif fault == "extra_field": entry["remaining_nulls"].append(dict(entry["remaining_nulls"][0], block_index=1))
            elif fault == "other_blocker": entry["other_blockers"] = ["missing options"]
            elif fault == "coverage": value["pages"][0]["coverage_complete"] = False; clearance = self.clearance(value)
            else: value["verdict"] = "approved"; clearance = self.clearance(value)
            with self.subTest(fault=fault), self.assertRaises(ValueError): self.compile_context(value, cleared_blockers=clearance)

    def test_number_and_context_adjudications_must_cover_every_null_together(self):
        result = self.unnumbered_context_result()
        result["pages"][0]["exercises"][0]["question_blocks"][1]["printed_number"] = None
        with self.assertRaises(ValueError): self.compile_context(result)
        numbers = {(29, "1B", "question_blocks", 1): dict(printed_number=2, reviewer="number-reviewer", reason="Reviewed label.")}
        markers, contexts = self.compile_context(result, adjudications=numbers)
        self.assertEqual(markers.printed_numbers, (1, 2, 3))
        self.assertEqual(len(markers.review_provenance[0]["adjudications"]), 1)
        self.assertEqual(len(markers.review_provenance[0]["context_adjudications"]), 1)
        self.assertEqual(next(iter(contexts["question"].values()))["question_numbers"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
