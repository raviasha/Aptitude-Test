# Repository consolidation — 18 September 2026

The consolidation starts from upstream main at `1b63123`, preserving the current
2.0 coordinator, faculty UI, DHCP fix, and published Windows/Ubuntu pilot files.

## Preserved on main

- Linux client entrypoint, identity protection, Debian packaging, build scripts,
  documentation, and tests recovered from the `build-student-signout-binaries`
  worktree. The shared client loader accepts its Linux identity protector while
  retaining the Windows default.
- Local Chapter 1 display repairs, mathematical superscript/subscript preservation,
  and multiline question display, with their regression tests.
- Textbook extraction/verification tools, logical-reasoning tools, chapter
  configurations, audits, completed/candidate ZIPs, and package metadata.
- New-reasoning-book Chapter 1 and Chapter 2 packages from `ba9ec2a`; their staged
  contents match that commit exactly.
- Historical reports and reusable guide-generation scripts.
- The complete tracked Android branch tree in
  [`archive/android-extension-76953dd.zip`](../archive/android-extension-76953dd.zip).
  See the [archive notes](../archive/README.md). It remains a separate legacy
  project, not a port of its functionality into the current coordinator.

Obsolete local 1.3.4 version/UI/installer edits were reconciled against the newer
main implementation rather than replacing the current 2.0 files. The old edits
remain available in the recovery snapshot.

## Files kept locally

Supplied textbook PDFs, machine-specific Android settings, local tool state,
caches, and duplicate/rendered deliverables remain outside version control.
The ignore rules name these categories; source scripts and question-bank outputs
remain tracked. Existing runtime data and build environments are preserved.

Before consolidation, five working directories were copied and verified by
SHA-256: 491 primary files, 309 Linux-worktree files, and 153, 261, and 185 files
from the other three worktrees (1,399 total). A verified Git bundle preserves the
original refs and their full history. The recovery folder beside the project is
`Aptitude-Test-recovery-20260918-091939`.

Retired worktree directories are retained there as file copies. Their embedded
`.git` worktree pointers may no longer resolve after retirement; use the Git
bundle when restoring history. Local PDFs and release copies are not deleted to
obtain a clean Git status.

## Verification

- Windows application, registration, question repair, UI, identity, and entrypoint
  selection: **104 passed, 22 Linux-only skips**, plus 7 passing subtests.
- Broad textbook/tool selection: **290 passed, 33 skipped**, plus 631 passing
  subtests. Six initial failures were five missing local-PDF checks and one stale
  expectation of the pre-2.0 data directory.
- After supplying the local PDF and correcting that test to the existing
  `KSAT Coordinator` default, the last-failed rerun completed with **105 passed,
  33 skipped, 6 deselected**, plus 148 passing subtests. The failed-test cache is
  empty. These selections overlap and should not be added into one total.
- All **95 ZIP archives** passed CRC integrity checks. This is an archive-integrity
  check, not a new assessment of textbook content accuracy.
- All four Debian maintainer scripts passed `sh -n` under Ubuntu 22.04.
- Whitespace checks pass. Debian script line endings are pinned to LF.

The Ubuntu 22.04 Linux protocol selection ran 22 tests: **20 passed, one failure,
one error**. Both problem names already appear in the pilot verification notes:

1. `test_load_gate_measures_the_requested_client_start_spread` observes 0.0 seconds
   because ticket timestamps have whole-second precision for a 0.2-second spread.
2. `test_closed_review_scores_locally_before_upload_then_matches_server_over_https`
   reports that restart did not recover its exact local attempt. It also failed
   in an isolated rerun during this consolidation and remains unresolved.

The complete test suite is therefore not claimed green. This consolidation does
not rebuild the installers, complete physical lab acceptance, or implement the
requested terminal-free Ubuntu GUI setup. Those remain separate follow-up work.
