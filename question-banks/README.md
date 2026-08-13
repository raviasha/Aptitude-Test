# Standalone question banks

Files in this folder are published separately on GitHub and are not included in
`Aptitude-Lab-Setup.exe`. The installer contains only the example pair from the
repository's `templates` folder.

To add a question bank to an installed server:

1. Download the desired archive or matching `.html` and `.json` files.
2. Extract the archive when necessary.
3. Copy both files, keeping the same base filename, into:

   ```text
   C:\ProgramData\Aptitude Lab\Question Banks
   ```

4. In Aptitude Lab, sign in as Faculty, open **Question banks**, select
   **Refresh folder**, and import the detected pair.

Keep correct answers and explanations in the JSON file only. The HTML file is
student-facing and should contain only the question text and visual content.

## Available downloads

- `quantitative_aptitude_complete_extended.html` and
  `quantitative_aptitude_complete_extended.json` — the complete quantitative
  aptitude bank; copy both files together. This source bank contains five-choice
  questions, including answers keyed to option E. The current Aptitude Lab
  importer supports A–D only, so this pair must not be imported until option E
  support is added or the bank is converted to four choices.
- `assessment-sample-30-questions.zip` — a downloadable copy of the sample bank
  included with the installer.
