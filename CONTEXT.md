# Sentinel domain glossary

## Proposal

A strategy engine's inert description of a possible defined-risk options
trade. A proposal has no broker capability; gates may refuse it before the
executor sees it.

## Cycle

One scheduled attempt to reconcile the account, evaluate readiness, generate
candidates, evaluate proposals, and manage exits. A cycle may manage exits
when entry readiness is red, but it must not open new exposure.

## Gate

A deterministic policy check. A BLOCKING gate answers: if it is red, why
should the agent not open new exposure right now? A gate belongs to one phase
and one operational dimension.

## Preflight readiness

The cycle-level decision that says whether the system may evaluate proposals
for new exposure. It is separate from individual proposal refusal.

## Pretrade authorization

The proposal-level decision made immediately before submission. It applies to
one proposal only and does not authorize exits.

## Entry Maintenance

The state in which no new exposure may be opened while reconciliation and
risk-reducing exits continue to run.

## Submission record

A durable record of one broker dispatch attempt, keyed by a client order ID.
An unresolved dispatch blocks new entries until broker reconciliation resolves
it; it never blocks exits.

## Deliverable provenance

The SHA-256 record binding a canonical submission binary to the source inputs
discovered by its build topology.
