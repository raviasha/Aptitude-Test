import json
from unittest.mock import patch
import app
from tests.book_test_support import BookFixture


class MultiChapterTests(BookFixture):
    def setup_selection(self, counts=(30,30,30), total=50):
        ids=[self.bank(f"Chapter {i+1}",count=n) for i,n in enumerate(counts)]
        for bank,title in zip(ids,["Book A","Book A","Book B"]):
            self.post("/api/admin/books/assign",{"bank_ids":[bank],"book_title":title})
        body={"chapters":[{"bank_id":b,"chapter":"Arithmetic"} for b in ids],
              "total_questions":total,"difficulties":["Easy"]}
        preview=self.post("/api/admin/tests/preview",body)
        self.assertEqual(preview.status_code,200,preview.text)
        return ids, {**body,"test_name":"Combined test","selection_mode":"balanced",
                     "preview_token":preview.json()["preview_token"]}

    def test_create_and_duplicate_keep_all_sources_and_quotas(self):
        ids,body=self.setup_selection()
        response=self.post("/api/admin/tests",body)
        self.assertEqual(response.status_code,200,response.text)
        created=response.json()
        with app.db() as db:
            composition=json.loads(db.execute("SELECT composition FROM tests WHERE test_id=?",(created["test_id"],)).fetchone()[0])
            self.assertEqual([r["quantity"] for r in composition],[17,17,16])
            selected=db.execute("""SELECT q.bank_id,q.question_id,q.correct_answer FROM release_questions r
                JOIN questions q ON q.question_id=r.question_id WHERE r.release_id=?""",(created["release_id"],)).fetchall()
            self.assertEqual(len({r["question_id"] for r in selected}),50)
            self.assertEqual([sum(r["bank_id"]==b for r in selected) for b in ids],[17,17,16])
            self.assertTrue(all(r["correct_answer"]=="A" for r in selected))
            self.assertEqual([r[0] for r in db.execute("SELECT bank_id FROM test_source_banks WHERE test_id=? ORDER BY bank_id",(created["test_id"],))],ids)
        duplicate=self.post(f"/api/admin/tests/{created['test_id']}/duplicate",{})
        self.assertEqual(duplicate.status_code,200,duplicate.text)
        self.assertNotEqual(duplicate.json()["release_id"],created["release_id"])
        tests=self.client.get("/api/admin/tests").json()["tests"]
        self.assertTrue(all(len(t["source_banks"])==3 for t in tests))

    def test_changed_preview_and_mixed_mode_never_create_test(self):
        ids,body=self.setup_selection()
        mixed=self.post("/api/admin/tests",{**body,"bank_id":ids[0]})
        self.assertEqual(mixed.status_code,400,mixed.text)
        with app.db() as db:
            db.execute("UPDATE questions SET active=0 WHERE question_id=(SELECT MAX(question_id) FROM questions)")
        changed=self.post("/api/admin/tests",body)
        self.assertEqual(changed.status_code,409,changed.text)
        with app.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tests").fetchone()[0],0)
        self.assertEqual(list(app.assessment_packs_dir().glob("*.ksat")),[])

    def test_manual_creation_still_works(self):
        bank=self.bank()
        response=self.post("/api/admin/tests",{"test_name":"Manual","bank_id":bank,
            "selection_rules":[{"category":"Category A","chapter":"Arithmetic","quantity":3}]})
        self.assertEqual(response.status_code,200,response.text)

    def test_release_failure_rolls_back_sources_and_test(self):
        _,body=self.setup_selection()
        with patch.object(app,"prepare_faculty_release",side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                self.post("/api/admin/tests",body)
        with app.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tests").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM test_source_banks").fetchone()[0],0)

    def test_secondary_source_replacement_and_usage(self):
        ids,body=self.setup_selection()
        response=self.post("/api/admin/tests",body)
        self.assertEqual(response.status_code,200,response.text)
        banks=self.client.get("/api/admin/question-banks").json()["banks"]
        self.assertEqual([b["test_count"] for b in banks if b["bank_id"] in ids],[1,1,1])
        with self.assertRaises(app.HTTPException) as error:
            app.save_question_package("Chapter 3",[],[],"new.zip",2,replace_existing=True)
        self.assertEqual(error.exception.status_code,409)

    def test_delete_secondary_source_keeps_other_banks_and_archived_csv(self):
        ids,body=self.setup_selection()
        created=self.post("/api/admin/tests",body).json()
        with app.db() as db:
            db.execute("INSERT INTO students VALUES ('S1','Student','unused','C','A',?)",(app.now(),))
            db.execute("""INSERT INTO attempts(attempt_id,student_id,test_id,started_at,submitted_at,
                total_questions,score,percentage,status) VALUES ('a1','S1',?,?,?,50,40,80,'submitted')""",
                (created["test_id"],app.now(),app.now()))
        before=self.client.get("/api/admin/export").text
        response=self.client.delete(f"/api/admin/question-banks/{ids[-1]}",headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["deleted_counts"]["tests"],1)
        self.assertEqual(self.client.get("/api/admin/export").text,before)
        with app.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM questions").fetchone()[0],60)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tests").fetchone()[0],0)

    def test_secondary_source_delete_rollback_restores_pack_and_sources(self):
        ids,body=self.setup_selection()
        created=self.post("/api/admin/tests",body).json()
        packs={p.name:p.read_bytes() for p in app.assessment_packs_dir().iterdir() if p.is_file()}
        with patch.object(app,"archive_result_rows",side_effect=RuntimeError("archive unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"/api/admin/question-banks/{ids[-1]}",headers=self.headers)
        self.assertEqual(packs,{p.name:p.read_bytes() for p in app.assessment_packs_dir().iterdir() if p.is_file()})
        with app.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM test_source_banks WHERE test_id=?",
                (created["test_id"],)).fetchone()[0],3)

    def test_same_passage_and_source_keys_keep_distinct_bank_media_and_answers(self):
        ids,body=self.setup_selection(counts=(1,1,1),total=3)
        with app.db() as db:
            for bank in ids:
                folder=app.question_assets_dir()/str(bank)
                folder.mkdir()
                (folder/"chart.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>'+str(bank)+'</text></svg>')
                db.execute("""INSERT INTO stimuli(bank_id,stimulus_id,stimulus_type,title,alt_text,asset_filename,content_json,created_at)
                    VALUES (?,'shared','image','Chart','Unique chart','chart.svg','{}',?)""",(bank,app.now()))
                db.execute("UPDATE questions SET stimulus_id='shared',correct_answer=? WHERE bank_id=?",
                           ("B" if bank==ids[1] else "A",bank))
            rules=[{"scope":"chapter","bank_id":b,"chapter":"Arithmetic","quantity":1} for b in ids]
            selected=app.sample_questions(db,ids[0],rules,["Easy"])
            public,assets=app.public_release_material(db,selected)
            self.assertEqual(len({q.stimulus.id for q in public}),3)
            self.assertEqual(len(assets),3)
            self.assertEqual(len({q.question_id for q in public}),3)
        created=self.post("/api/admin/tests",body)
        self.assertEqual(created.status_code,200,created.text)
        with app.db() as db:
            frozen=db.execute("""SELECT q.bank_id,r.correct_answer FROM release_questions r
                JOIN questions q ON q.question_id=r.question_id WHERE r.release_id=? ORDER BY q.bank_id""",
                (created.json()["release_id"],)).fetchall()
            self.assertEqual([r["correct_answer"] for r in frozen],["A","B","A"])

    def test_deletion_reports_pending_cleanup_without_losing_archived_data(self):
        ids,body=self.setup_selection()
        created=self.post("/api/admin/tests",body).json()
        with patch.object(app.ArtifactQuarantine,"purge",return_value=False):
            result=self.client.delete(f"/api/admin/tests/{created['test_id']}",headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertTrue(result.json()["cleanup_pending"])
            result=self.client.delete(f"/api/admin/question-banks/{ids[-1]}",headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertTrue(result.json()["cleanup_pending"])
