import unittest
import app
from tests.book_test_support import BookFixture


class BalancedCountsTests(unittest.TestCase):
    def test_equal_capped_and_invalid_counts(self):
        from ksat.coordinator.question_selection import balanced_counts
        for capacities, total, expected in [
            ([100,100,100],50,[17,17,16]), ([4,100,100],50,[4,23,23]),
            ([1,2,20],10,[1,2,7]), ([2,3],5,[2,3]), ([9],7,[7])]:
            self.assertEqual(balanced_counts(capacities,total),expected)
        for capacities,total in [([],3),([0,5],3),([5,5],1),([2,3],6),
                                 ([900],501),([2],0),([5],2.5),([5],True)]:
            with self.subTest(capacities=capacities,total=total), self.assertRaises(ValueError):
                balanced_counts(capacities,total)


class SelectionPreviewTests(BookFixture):
    def test_preview_pools_categories_separates_banks_and_is_read_only(self):
        ids = [self.bank(f"Chapter {i}",count=n) for i,n in [(1,4),(2,30),(3,30)]]
        for bank, title in zip(ids,["A","A","B"]):
            self.assertEqual(self.post("/api/admin/books/assign",{"bank_ids":[bank],"book_title":title}).status_code,200)
        chapters = [{"bank_id":b,"chapter":"Arithmetic"} for b in ids]
        payload = {"chapters":chapters,"total_questions":50,"difficulties":["Easy"]}
        response = self.post("/api/admin/tests/preview",payload)
        self.assertEqual(response.status_code,200,response.text)
        preview=response.json()
        self.assertEqual([a["quantity"] for a in preview["allocations"]],[4,23,23])
        self.assertEqual([a["available"] for a in preview["allocations"]],[4,30,30])
        again=self.post("/api/admin/tests/preview",{**payload,"chapters":list(reversed(chapters))})
        self.assertEqual(again.json()["preview_token"],preview["preview_token"])
        with app.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tests").fetchone()[0],0)
            db.execute("UPDATE questions SET active=0 WHERE question_id=(SELECT MAX(question_id) FROM questions)")
        changed=self.post("/api/admin/tests/preview",payload)
        self.assertNotEqual(changed.json()["preview_token"],preview["preview_token"])
        for altered in [{"chapters":chapters*2},{"difficulties":["Hard"]},{"total_questions":500}]:
            self.assertEqual(self.post("/api/admin/tests/preview",{**payload,**altered}).status_code,400)
        catalogue=self.client.get("/api/admin/chapter-catalogue")
        self.assertEqual(catalogue.status_code,200,catalogue.text)
        self.assertEqual(len(catalogue.json()["chapters"]),3)
