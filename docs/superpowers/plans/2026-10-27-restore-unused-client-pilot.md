# Restore an unused withdrawn client update

User problem: the pilot PC was offline with KSAT closed; withdrawing the update
made the same bundle permanently unusable for another pilot. Restore only the
unused release in this case. Do not revive a published release or a release
with any client progress report. Keep Client/Builder 2.1.6 unchanged.

Approach selected for this maintenance fix: an explicit **Restore for pilot**
action, rather than silent reactivation on re-upload or deleting update records.
Reverify the stored signed bundle, bind it to the existing release, then change
withdrawn to uploaded in a serialized transaction. Preserve the original
bundle and withdrawal date; no device status, enrollment or assessment data is
deleted. An explicit new pilot selection is required; no fleet publication.
The restriction to unused releases is an implementation choice for this case;
general recovery of interrupted/published rollouts is outside this fix.

1. Reproduce withdrawn re-upload and missing restore; add store/API/UI tests
   for restoration, guards, signature corruption, authentication and selection.
2. Implement store guard and restore, authenticated API and visible action;
   explain that offline means KSAT must be opened and restarted after selection.
3. Bump Coordinator to 2.2.5, retain 2.2.4 runtime compatibility, verify focused
   update/UI/runtime/packaging regressions and get independent code review.
4. Build/sign Coordinator-only installer; inspect resources, HTTPS startup and
   unchanged Client/update-key bytes. Publish versioned artifacts and guide to
   GitHub and verify downloaded hashes. No live installation or data edits.

Existing isolated worktree is reused. A broader application run was completed
for 2.2.4 immediately before this localized change; one disposable HTTPS startup
timeout passed its isolated rerun. Use focused meaningful checks here.
