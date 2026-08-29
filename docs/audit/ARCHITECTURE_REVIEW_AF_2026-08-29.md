# Architecture Review A--F — completion record

## Scope

This record covers the post-close Sentinel architecture work only. It does not
authorize a broker order, scheduler change, credential change, account change,
remote push, or competition-platform submission.

The competition constraint is modelled separately from broker capability:
Alpaca can support multiple asset classes, while this competition policy
requires every declared Sentinel strategy to contain an executable options
component. The present Sentinel runtime remains options-only. Reusing JrAlpha
stock-selection or risk signals as inputs remains compatible with that model;
mixed-asset execution remains a separately designed future capability.

## A. Gate evaluation

`gates/evaluation.py` supplies `CycleSubject`, `ProposalSubject`, and the
`GateEvaluator` interface. Phase selection and context construction are owned
there rather than reconstructed by the cycle orchestrator. Proposal-only gates
therefore receive proposal facts through the same evaluation seam as live
execution and tests.

## B. Entry submission

`agent/entry_submission.py::submit_entries` owns the submission loop and
returns submission outcomes. The cycle orchestrator supplies adapters for the
broker, recorder, and state; it no longer owns the loop's individual
submission decisions.

## C. Structure ledger

`agent/ledger.py::StructureLedger` owns pending-entry recording, fill
promotion, terminal non-fill outcomes, group state, and reconciliation.
Accounting begins as a pending entry before broker dispatch and promotes only
on an exact fill, preventing an accepted or later-cancelled broker request
from being represented as a position.

## D. Values and deliverables

`scripts/check_deliverables.py` discovers current deliverables by default and
reports explicit exemptions. `scripts/write_deliverable_provenance.py` records
SHA-256 provenance for the canonical cover, slides, and video from their
actual inputs. The checker is blocking for missing or mismatched binary
provenance, while quoted external authority and documented historical material
remain visible ATTENTION findings.

## E. Timeline compiler

`sentinel-video/tools/timeline.py` has one live split-scene representation:
the emitter and validator both resolve the declared scene modules. The obsolete
one-off gate-count migration helper was removed after its completed migration,
so it cannot become an alternate source of delivery text.

## F. Position lifecycle

`agent/position_lifecycle.py::PositionLifecycle` owns structure close-order
reconciliation, quantities, shared-symbol ownership, quarantine, and orphan
handling. A group is closed by its own close order and expected quantity, not
by the account's net position for a shared option symbol. Single-leg close
limits retain their positive debit form; multi-leg credit closes retain their
credit sign.

## Competition-composition enforcement

The manifest declares each strategy's `execution_shape_ids`; the loader
derives reachable asset classes from those actual `OrderShape` declarations.
It rejects missing, unknown, or non-options strategy shapes. Both the pretrade
`order_shape_declared` gate and `Executor.submit` enforce the strategy-bound
shape for entries. Closing orders deliberately retain the global declared-shape
check so safety-maintenance exits cannot be suppressed by an entry policy.

## Verification commands

The following commands must pass before a release candidate is considered
ready:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_deliverables.py
cd sentinel-video && python3 tools/timeline.py --check && npm run check
```

The deliverables checker may emit documented ATTENTION findings for historical
records and quotations from the organizer. It must emit no BLOCKING finding.
