# Focused textbook fidelity fixes

The current chapter ZIPs are accepted as the baseline. Fix only demonstrated
pipeline failures and the clearly visual data-interpretation chapters.

1. Allow an explicit safe update of an already imported, unused bank so a
   corrected package refreshes stable source keys instead of returning 409.
2. Render recurring decimals with font-independent notation so ordinary 2.64
   and recurring 2.64 remain visibly distinct.
3. Add source-PDF question media for Chapters 36-39 using their reviewed shared
   table/graph contexts. Never redraw textbook visuals.
4. Verify Chapter 1 Q124 and Chapter 3 Q113, then package only changed chapters.

