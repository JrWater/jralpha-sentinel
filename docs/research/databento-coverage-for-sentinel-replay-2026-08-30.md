# Databento coverage for a Sentinel historical replay

## Conclusion

**Databento can supply the missing QQQ and other listed-equity/ETF market and
option-market data needed to extend an execution-level Sentinel replay. It
cannot, from `OPRA.PILLAR`, supply an independent historical IV/Delta series
with which to validate IV/Delta derived from the same BBO prices.**

Therefore, a complete replay can obtain its point-in-time prices, definitions,
and NBBO from Databento, but it must either (a) treat Black-Scholes
IV/Greeks reconstructed from those prices as model outputs, not independently
validated inputs, or (b) add a separate independently sourced historical-Greeks
validation dataset. No data was requested or downloaded *as part of this
assessment*; any later acquisition must be recorded and validated separately.

## 1. QQQ regime and execution data

**Available, subject to an explicit per-session coverage check.** Databento US
Equities Mini (`EQUS.MINI`) offers MBP-1, BBO, TBBO, trades, definitions, and
OHLCV at one-second, one-minute, one-hour, and one-day intervals. That is
sufficient in format for QQQ daily regime features and intraday replay inputs.
Its BBO and OHLCV are, however, derived and aggregated across the dataset's
component venues, not a full SIP/NBBO feed. A claim of an *exact* reproduction
of a different feed's values therefore needs a prior source-parity decision and
session-by-session symbol/row/continuity checks.

Sources: [EQUS.MINI data-feed specification](https://databento.com/docs/venues-and-datasets/equs-mini),
[Databento schema definitions](https://databento.com/docs/schemas-and-data-formats/whats-a-schema).

## 2. Sentinel universe option definitions and BBO

**Available in principle for the current listed stock/ETF universe.**
`OPRA.PILLAR` provides consolidated last sale and national BBO across US equity
options exchanges, including single-name stock options and ETF options such as
SPY and QQQ. Its `definition` schema supplies point-in-time contract reference
data including strike and expiration. Databento documents parent symbology for
retrieving an individual underlying's option chain, so the same retrieval
pattern applies to each declared Sentinel name that was OPRA-listed on a given
session.

For new files, prefer the consolidated schemas: CMBP-1 holds NBBO updates and
trades from 2023-03-28; CBBO-1s holds interval NBBO from that date; CBBO-1m,
definitions, statistics, trades, and OHLCV have history from 2013-04-01.
The replay importer must still validate every chosen contract and decision/exit
timestamp. OPRA's definition expiration is date-granular (UTC midnight), so
same-day expiration handling cannot infer an intraday expiry timestamp from
that field alone.

Sources: [Databento equity-options introduction](https://databento.com/docs/examples/options/equity-options-introduction/using-parent-symbology-to-fetch-an-option-chain),
[OPRA Pillar specification](https://databento.com/docs/knowledge-base/datasets/opra-pillar),
[OPRA historical coverage announcement](https://databento.com/blog/opra-improvements-coming-soon),
[NBBO resampling example](https://databento.com/docs/examples/options/nbbo-resampling).

## 3. Independent historical IV and Greeks

**Not available from Databento's OPRA dataset.** Databento's `statistics`
schema defines statistic type 14 (volatility) and 15 (delta), but its
dataset-availability table marks neither type as published by `OPRA.PILLAR`.
The official Databento IV example instead numerically solves IV from historical
definition and top-of-book price data. Greeks produced from that IV share the
same BBO/model inputs, so they are useful reconstructed values but cannot
independently validate the reconstruction.

The required control is a separate source with timestamped historical
equity-option IV/Greeks, matched by OCC contract and observation time, plus a
documented tolerance and reconciliation rule. Until then, label any
BBO-derived IV/Delta as `derived_unvalidated` and do not use it for a
decision-validating performance claim.

Sources: [statistics schema and dataset matrix](https://databento.com/docs/schemas-and-data-formats/statistics),
[Databento's historical IV reconstruction example](https://databento.com/docs/examples/options/estimating-implied-volatility).

## Acquisition decision

| Missing input | Databento verdict | Required safeguard |
|---|---|---|
| QQQ daily/intraday price series | Yes, `EQUS.MINI` | Accept/decline derived-feed parity; validate coverage before import. |
| Other Sentinel listed equities/ETFs and their option chains/BBO | Yes in principle, `EQUS.MINI` + `OPRA.PILLAR` | Resolve each symbol/session and verify definitions plus NBBO at every entry/exit. |
| Independent historical equity-option IV/Delta/Greeks | No, not from `OPRA.PILLAR` | Acquire a separate validation source; do not relabel BBO-derived values as independent. |
