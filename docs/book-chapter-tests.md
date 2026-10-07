# Tests from several chapters

## Organize imported chapters

In **Question banks**, check the banks belonging to a textbook. Under **Organize by textbook**, choose an existing book or enter a new title, then click **Assign selected to book**. You can rename a book using the same controls.

Older imports initially appear as **Unassigned**. Assigning a book does not reimport, replace, or copy questions, and does not change existing tests or results. Future chapter packages can include an optional `book_title` in their manifest.

## Create a combined test

1. Open **Tests → Create an assessment**.
2. Leave **Question selection** set to **Balanced across chapters**.
3. Enter a name and total question count, then check chapters under one or more books.
4. Choose a difficulty and review the chapter allocation.
5. Click **Create test**.

The Coordinator shares the total roughly equally and randomly selects questions within each chapter. For example, 50 questions across three chapters gives 17, 17, and 16. If one chapter has only four eligible questions, the split becomes 4, 23, and 23. Every checked chapter must supply at least one question, and the combined pool must cover the requested total (maximum 500).

If the question availability changes before creation, refresh the allocation and review it again. Creating a test freezes its selected questions using the existing assessment release mechanism; it does not independently redraw questions for each student.

**Manual single-bank counts** remains available for the previous test-building workflow. Student practice is unchanged.

## Existing data and deletion

The schema upgrade adds book/source metadata without replacing student accounts, question banks, tests, or results. Existing chapter ZIPs do not need to be reimported.

Deleting a chapter bank also deletes every test depending on it, including combined tests. The confirmation shows the affected test count. Other chapter banks remain intact, and submitted faculty result summaries remain in the CSV export. Full answer reviews for deleted tests are not retained by that summary archive.

If the database deletion succeeds but quarantined files cannot yet be removed, the Coordinator reports **file cleanup pending**. The data deletion has committed; do not retry it as if nothing happened. The existing recovery mechanism retains the cleanup record for a later retry.

This feature uses the existing client assessment format. No client update is required for chapter selection. Installing or publishing a new Coordinator binary is a separate release step.
