# Post-close architecture execution plan

## Status and authority

Recorded at the user's direction on 2026-08-28. This is a decision record and
execution order for the live Alpaca paper-trading competition system. It does
not authorize changes to trading, scheduling, credentials, broker accounts, or
submission platforms by itself.

## Competition-rule boundary

Keep three facts distinct:

1. **Platform capabilities.** Alpaca supports orders for US stocks, options,
   ETFs, and crypto.
2. **Competition constraint.** The organizer's event email says every
   competition strategy must incorporate options trading.
3. **Current Sentinel runtime capability.** Sentinel currently executes only
   declared `us_option` orders.

The competition constraint does not mean that Alpaca, or every future
Sentinel deployment, is options-only. It does mean a standalone pure-stock,
pure-ETF, or pure-crypto strategy is not an acceptable competition strategy.
JrAlpha stock-selection and risk logic may be reused as a signal source for an
options-executing Sentinel strategy. A future mixed-asset execution strategy
needs its own design and eligibility review; it is not a small manifest edit.

## Execution order after market close

1. Preserve an evidence baseline for the running version: broker facts,
   positions, WAL/reconciliation state, snapshot, policy identity, commit, and
   working-tree state. Do not alter the running entry path while this baseline
   is being captured.
2. Complete Architecture Review A--F in the order below. Each change is
   test-first, independently reviewed, and is not promoted into the running
   entry path until its integration evidence is green.
3. Add a policy-level competition-composition check: a competition strategy
   must contain an actual options component. This is distinct from, and must
   not falsely claim, that every supported asset class is options-only.
4. Choose explicitly between:
   - **signal reuse (default):** JrAlpha supplies stock-selection, regime, or
     risk signals; Sentinel executes declared options structures; or
   - **mixed-asset execution:** stocks/ETFs/crypto orders and options orders
     jointly form one strategy. This requires a separately approved design for
     order shapes, risk budgets, ledger/reconciliation, lifecycle management,
     and the competition-composition gate.
5. Only after A--F and the above rule model are complete, resolve the three
   delivery blockers together: regenerate canonical video, cover PNG, and
   slides PDF; write and verify their provenance; run the deliverables checker;
   then prepare the final platform submission for explicit approval.

## Architecture Review A--F

### A. Gate evaluation

Create one deep gate-evaluation module that owns phase selection and context
construction for cycle and proposal evaluation. This removes the duplicated
preflight/pretrade contexts that made proposal-specific entry-window logic
unreachable.

### B. Entry submission

Move the entry-submission loop out of `scripts/run_cycle.py` into one testable
module with a narrow interface. Live broker and recorder are adapters at its
seam; callers receive submission outcomes rather than patching globals.

### C. Structure ledger

Move structure metadata, group recording, and take-profit/stop-loss updates
into the ledger module. `run_cycle` remains orchestration, not a second
file-backed ledger.

### D. Values and deliverables

Make one machine-readable values module the source for manifest values,
documentation, video/slides narration, and the deliverables checker. Discovery
must be default-on with explicit, reported exemptions so a scene split cannot
silently remove material from coverage.

### E. Timeline compiler

Remove the video timing compiler's dead half-migrated root-scene mode. Keep one
emitter and one validator for the live scene-module representation.

### F. Position lifecycle

Deepen the exit module so pricing, structure netting, orphan detection, and
close-proposal construction live behind one interface. The caller must not
reconstruct the position schema before it can ask whether a structure exits.

## Non-goals for this work window

- No automatic expansion to standalone stock, ETF, or crypto execution.
- No transfer of IBKR account history or results into the Alpaca competition.
- No changes to credentials, schedules, competition account identity, or final
  submission without a separate explicit user instruction.
