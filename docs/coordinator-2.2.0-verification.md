# Coordinator 2.2.0 verification — 9 October 2026

## Scope

This release packages the previously reviewed book-grouped multi-chapter selection
feature, adds direct master-ZIP import, and retains completed CSV summaries when
tests or banks are deleted. Client source and binaries are unchanged.

## Automated evidence

- Application suite (`pytest tests -q`): **848 passed, 48 skipped, 595 subtests passed**
  in 587.34 seconds. This run began before the final compressed-data exception fix.
- Final-source targeted suite: **128 passed, 132 subtests passed**, covering master
  ZIP import, books, balanced selection, combined tests, deletion/CSV archives,
  runtime upgrades, Windows entrypoints and packaging. It includes the final
  corrupted-DEFLATE regression and preservation of successful chapter results.
- Final master-import tests: **14 passed**, including malformed manifests, corrupt
  archives, source pages, unsafe paths, duplicates, total expansion limits,
  normalized paths, replacement handling, and non-blocking server requests.
- Root application/media/package tests: **84 passed, 5 subtests passed**. One old
  cache-version expectation initially failed and was updated for the deliberate
  2.2.0 cache keys before this passing rerun.
- Faculty browser fixture and JavaScript bulk-import checks passed, including
  mixed chapter results inside a master ZIP and accurate success/failure totals.
- Real quantitative master archive: **39 chapters imported** into a disposable
  database. Real reasoning source-page archive: both chapters rejected with the
  explanation that question extraction is required.
- Independent review: malformed-manifest continuation, normalized manifest paths,
  and compressed-data failures were reproduced and fixed with regression tests.
  No outstanding findings remain. No live lab installation was performed.

## Binary evidence

Both the executable and installer have product version **2.2.0** and valid
Authenticode signatures from `CN=KSAT LAB RELEASE SIGNING`, using the existing
lab identity. The packaged executable passed disposable HTTPS startup for both
the product-version endpoint and the unchanged legacy client handshake.
The elevated executable and non-elevated smoke build have equivalent application
payloads; extracting the installer confirmed its embedded executable matches.
The signed executable's embedded UI was inspected for the 2.2.0 label,
multi-chapter selection and direct master-ZIP import.

SHA-256:

```text
bb105412fbeea8e18472354b1664b19fd291308dfc2e8c4541ce9820a361e9b7  KSATCoordinator-2.2.0.exe
1bad39f56f211d6d0b4bd973ff70e830d33332755ab12f11fad9a9903d836970  KSATCoordinatorSetup-2.2.0.exe
```

## Compatibility decisions

- UI, Windows product metadata, installer and `/api/coordinator-build`: 2.2.0.
- `/api/build` and public trust metadata: 2.1.0, intentionally retained because
  already-installed clients/builders require the exact compatibility contract.
- Existing installer identity, path and owned firewall rule names remain stable.
- Runtime configuration upgrades accept both 2.0.0 and 2.1.0; the new migration
  backup uses `pre-2.2.0`, preserving any earlier `pre-2.1.0` backup.
- Master imports are not all-or-nothing: successful chapters remain, errors are
  listed per chapter. Outer-archive safety/combined expansion checks run before
  any chapter is saved. Nested masters are not recursively imported.

## Limits of verification

The bare repository-wide `pytest -q` still stops at collection of the ten
unrelated textbook-extraction modules listed in
[the earlier verification report](book-chapter-tests-verification.md). Missing
dependencies are `pdfplumber`, `pypdf`, and `jsonschema`, alongside a duplicate
`test_build` module name. These suites are not claimed as passing.

Automated verification is not a physical-server upgrade pilot. The build did not
modify installed lab ProgramData, trust stores or firewall settings. Preserve a
backup and perform an initial login/test/export check on the upgraded server.
