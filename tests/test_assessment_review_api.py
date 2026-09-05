import json
import sqlite3
import unittest

from ksat.coordinator.releases import wrap_release_content_key, wrap_release_review_key
from ksat.coordinator.reviews import ReviewProblem, issue_review_grant, list_completed_assessments
from ksat.coordinator.schema import migrate_distributed_schema


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
RELEASE_ID = "22222222-2222-4222-8222-222222222222"


class AssessmentReviewAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE tests (
              test_id INTEGER PRIMARY KEY, test_name TEXT NOT NULL,
              launched INTEGER NOT NULL DEFAULT 0, release_id TEXT
            );
            CREATE TABLE attempts (
              attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, test_id INTEGER NOT NULL,
              status TEXT NOT NULL, total_questions INTEGER NOT NULL, score INTEGER NOT NULL,
              percentage REAL NOT NULL
            );
            CREATE TABLE responses (
              response_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
              question_id INTEGER NOT NULL, selected_answer TEXT, correct INTEGER,
              category TEXT NOT NULL, chapter TEXT NOT NULL, question_order INTEGER NOT NULL,
              UNIQUE(attempt_id, question_id)
            );
        """)
        migrate_distributed_schema(self.connection)
        self.master_key = b"m" * 32
        content_key = b"c" * 32
        review_key = b"r" * 32
        self.connection.execute(
            "INSERT INTO tests (test_id,test_name,launched,release_id) VALUES (7, 'Aptitude', 1, ?)",
            (RELEASE_ID,),
        )
        self.connection.execute(
            """INSERT INTO assessment_releases
               (release_id,test_id,state,duration_seconds,manifest_json,content_pack_filename,
                content_hash,content_signature_b64,wrapped_content_key_b64,
                wrapped_review_key_b64,created_at)
               VALUES (?,?, 'launched',120,'{}',?,?,?, ?,?,?)""",
            (
                RELEASE_ID, 7, f"{RELEASE_ID}.ksatpack", "a" * 64, "c2ln",
                wrap_release_content_key(self.master_key, RELEASE_ID, content_key),
                wrap_release_review_key(self.master_key, RELEASE_ID, review_key),
                "2026-09-05T08:00:00+00:00",
            ),
        )
        self.connection.execute(
            """INSERT INTO attempts
               (attempt_id,student_id,test_id,status,total_questions,score,percentage,release_id)
               VALUES (?,?,7,'submitted',2,1,50,?)""",
            (ATTEMPT_ID, "S100", RELEASE_ID),
        )
        self.connection.execute(
            """INSERT INTO submissions
               (attempt_id,bundle_hash,bundle_json,accepted_at,receipt_json)
               VALUES (?,?,?,?,'{}')""",
            (ATTEMPT_ID, "b" * 64, "{}", "2026-09-05T09:00:00+00:00"),
        )
        self.connection.executemany(
            """INSERT INTO responses
               (attempt_id,question_id,selected_answer,correct,category,chapter,question_order)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (ATTEMPT_ID, 7, "B", 1, "Q", "C", 0),
                (ATTEMPT_ID, 3, None, 0, "Q", "C", 1),
            ],
        )

    def tearDown(self):
        self.connection.close()

    def test_review_keys_are_withheld_until_explicit_close(self):
        summaries = list_completed_assessments(self.connection, student_id="S100")
        self.assertEqual("waiting", summaries[0].review_state)
        with self.assertRaisesRegex(ReviewProblem, "Faculty closes"):
            issue_review_grant(
                self.connection, attempt_id=ATTEMPT_ID,
                student_id="S100", pack_master_key=self.master_key,
            )

        self.connection.execute("UPDATE tests SET launched=0 WHERE test_id=7")
        grant = issue_review_grant(
            self.connection, attempt_id=ATTEMPT_ID,
            student_id="S100", pack_master_key=self.master_key,
        )
        self.assertEqual([7, 3], [item.question_id for item in grant.responses])
        self.assertEqual(["B", None], [item.selected_answer for item in grant.responses])
        self.assertNotEqual(grant.content_key_b64, grant.review_key_b64)
        self.assertEqual("available", list_completed_assessments(
            self.connection, student_id="S100"
        )[0].review_state)

    def test_review_is_owned_by_the_submitting_student(self):
        self.connection.execute("UPDATE tests SET launched=0 WHERE test_id=7")
        with self.assertRaises(ReviewProblem):
            issue_review_grant(
                self.connection, attempt_id=ATTEMPT_ID,
                student_id="S200", pack_master_key=self.master_key,
            )


if __name__ == "__main__":
    unittest.main()
