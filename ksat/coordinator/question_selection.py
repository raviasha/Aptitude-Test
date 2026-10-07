"""Deterministic capacity allocation; random question sampling stays in app."""
from __future__ import annotations
import hashlib
import json
import re

UNCATEGORIZED_CHAPTER = "Uncategorized"


def balanced_counts(capacities: list[int], total: int) -> list[int]:
    if type(total) is not int or not 1 <= total <= 500:
        raise ValueError("Choose a whole-number total between 1 and 500.")
    if not capacities or any(type(n) is not int or n <= 0 for n in capacities):
        raise ValueError("Every selected chapter must have eligible questions.")
    if total < len(capacities):
        raise ValueError("Choose at least one question per selected chapter.")
    if total > sum(capacities):
        raise ValueError(f"Only {sum(capacities)} eligible questions are available.")
    counts = [0] * len(capacities)
    remaining = total
    while remaining:
        for i, capacity in enumerate(capacities):
            if counts[i] < capacity:
                counts[i] += 1
                remaining -= 1
                if not remaining:
                    break
    return counts


def _natural(text):
    return tuple((0, int(s)) if s.isdigit() else (1, s.casefold()) for s in re.split(r"(\d+)", text))


def chapter_catalogue(connection) -> list[dict]:
    chapters = {}
    for row in connection.execute("""SELECT q.bank_id,b.bank_name,b.book_id,bk.title AS book_title,
        COALESCE(NULLIF(q.chapter,''),?) AS chapter,q.difficulty,COUNT(*) AS quantity
        FROM questions q JOIN question_banks b ON b.bank_id=q.bank_id
        LEFT JOIN books bk ON bk.book_id=b.book_id WHERE q.active=1
        GROUP BY q.bank_id,COALESCE(NULLIF(q.chapter,''),?),q.difficulty""",
        (UNCATEGORIZED_CHAPTER, UNCATEGORIZED_CHAPTER)):
        key = (row["bank_id"], row["chapter"])
        item = chapters.setdefault(key, {k: row[k] for k in
            ("bank_id","bank_name","book_id","book_title","chapter")})
        item["question_count"] = item.get("question_count", 0) + row["quantity"]
        item.setdefault("difficulties", {})[row["difficulty"]] = row["quantity"]
    bank_counts = {}
    for bank, _ in chapters:
        bank_counts[bank] = bank_counts.get(bank, 0) + 1
    def order(item):
        match = re.match(r"^(?:chapter\s*)?(\d+)\b", item["chapter"], re.I)
        if not match and bank_counts[item["bank_id"]] == 1:
            match = re.search(r"\bchapter\s*(\d+)\b", item["bank_name"], re.I)
        return (item["book_id"] is None, (item["book_title"] or "").casefold(),
                int(match[1]) if match else float("inf"), _natural(item["chapter"]), item["bank_id"])
    return sorted(chapters.values(), key=order)


def preview_selection(connection, chapters: list[dict], total: int, difficulties: list[str]) -> dict:
    if not chapters or len(chapters) > 500:
        raise ValueError("Select between 1 and 500 chapters.")
    if not difficulties or any(d not in ("Easy", "Medium", "Hard") for d in difficulties):
        raise ValueError("Choose valid difficulty levels.")
    keys = [(c["bank_id"], c["chapter"]) for c in chapters]
    if len(keys) != len(set(keys)):
        raise ValueError("A chapter can only be selected once.")
    wanted = set(keys)
    selected = [c for c in chapter_catalogue(connection) if (c["bank_id"],c["chapter"]) in wanted]
    if len(selected) != len(keys):
        raise ValueError("A selected chapter no longer exists or has no active questions.")
    allocations = []
    for chapter in selected:
        available = sum(chapter["difficulties"].get(d, 0) for d in set(difficulties))
        if not available:
            raise ValueError(f"{chapter['bank_name']} / {chapter['chapter']} has no questions at this difficulty.")
        allocations.append({k: chapter[k] for k in
            ("bank_id","bank_name","book_id","book_title","chapter")} | {"available": available})
    counts = balanced_counts([a["available"] for a in allocations], total)
    for allocation, count in zip(allocations, counts):
        allocation["quantity"] = count
    fingerprint = {"allocations": allocations, "total": total, "difficulties": sorted(set(difficulties))}
    token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"allocations": allocations, "total_questions": total, "preview_token": token,
            "redistributed": max(counts) - min(counts) > 1}
