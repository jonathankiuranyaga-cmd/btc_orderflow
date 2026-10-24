# BTC Order-Flow Paper Desk

A free, local Streamlit dashboard for BTC scalping research and paper trading. It reads public market data; it does not connect to an account, submit orders, or require API keys.

## Setup on Windows PowerShell

From this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

If PowerShell blocks activation, run the app with the environment's executable directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Streamlit prints the local URL, usually `http://localhost:8501`. Stop it with `Ctrl+C`.

## Data and instruments

- **BTCUSDT perpetual:** Binance USDⓈ-M futures aggregate trades (`/fapi/v1/aggTrades`) and order-book snapshots (`/fapi/v1/depth`). Prices and notional are quoted in USDT. `m=true` on an aggregate trade means the buyer was maker, so the seller was the aggressor.
- **BTC-USD spot:** Coinbase Exchange public ticker (`/products/BTC-USD/ticker`). This is a separate spot product quoted in USD, not the Binance perpetual and not a USDT conversion.
- **Basis:** the perp price is quoted in USDT and the spot price in USD, so the dashboard also reads Coinbase `USDT-USD` and converts spot to a USDT equivalent before computing basis. The raw USD-vs-USDT price difference is *not* a tradable spread; the converted figure still ignores fees, FX, and funding.
- The dashboard polls every three seconds. Trade delta is calculated over the most recent 60 seconds of fetched Binance aggregate trades. Book imbalance uses the selected number of visible price levels on each side; the book is a snapshot, not a queue-position feed.
- **Data health:** every feed has its own connected/disconnected state, reconnect counter, and last-success age. A feed is only marked stale once it has missed more than one full poll cycle, so a healthy three-second poll is never mistaken for stale data.
- **Trade-feed gaps:** the aggregate-trade endpoint returns at most 1,000 rows per request. When more than 1,000 trades print between two polls the oldest rows fall off the response and the 60-second delta would silently under-count. The dashboard detects the skipped ids and reports them, because the aggressor delta, CVD, and one-second candles are only as complete as the feed.
- **Forced liquidations:** Binance public WebSocket `btcusdt@forceOrder`, with reconnect backoff that resets after a successful connection.
- **Positioning:** Binance open interest (with a 60-second change) and the last settled funding rate are shown alongside the price/open-interest regime read.
- **Next one-minute candle estimate:** A regularized logistic model uses completed Binance BTCUSDT one-minute candles and features from 1/3/5-minute returns, candle body, and relative volume. It predicts whether the next candle will close above the latest completed close. The dashboard reports a recent chronological holdout accuracy and majority-class baseline; the displayed model score is not a calibrated probability. It returns `NO CLEAR EDGE` near a 50/50 score.

## Signals and paper risk

- **LONG:** 60-second taker-buy delta divided by total traded BTC exceeds the selected flow threshold, and top-book bid-size imbalance exceeds the selected book threshold.
- **SHORT:** both conditions are met in the negative direction. Otherwise the signal is **FLAT**.
- This deliberately simple, inspectable conjunction is a research heuristic, not a backtested or predictive strategy. A possible absorption note appears when one-sided aggression is strong but the observed 60-second price move stays below twice the median inter-trade step (floor of 3 bps); that is also only a heuristic.
- The candle model is experimental and can perform worse than its baseline or change behavior across regimes. Its recent holdout score is not a guarantee of future performance and is not financial advice.
- A paper long/short setup is shown only when the one-minute estimate agrees with the live order-flow signal and the daily loss limit is not hit. The corresponding manual paper-entry button is enabled only for that aligned setup; no order is ever placed automatically.
- By default, paper entries are also blocked whenever recent holdout accuracy is below the majority baseline. This reduces exposure when the model lacks evidence of an edge; it cannot establish or guarantee profitability.
- The live BTCUSDT trade-price chart shows a rolling five-minute window with UTC time on the horizontal axis and refreshes every three seconds. The latest observed trade time is printed below it.
- The historical backtest starts at 10 USDT by default and downloads 7 or 30 days of Binance one-minute candles. Its default five-fold walk-forward mode uses expanding chronological training data and five sequential, non-overlapping test windows; each fold starts with the selected balance for comparison. Single 80/20 holdout remains available. The model fits on at most the latest 600 labeled bars before each test fold. By default, entries are blocked unless that fold's earlier validation beats its majority baseline. It applies selected sizing, fees, slippage, stops/targets, flow thresholds, and forecast cutoff. Since historical order-book imbalance is not supplied by the kline feed, it is a bar-only approximation, not an exact replay of live entry rules. The $10 balance is paper-only and does not imply any real exchange minimum order size.
- The backtest includes fixed model-score cutoffs with held-out hit rates and sample counts. These scores are not calibrated probabilities; small-sample high hit rates are not evidence of a reliable 90% strategy.
- Position entry is manual. The signal never trades automatically. Quantity is limited by the risk budget divided by estimated stop loss per BTC (including configured round-trip fees and slippage), and by the maximum notional/equity setting.
- The app simulates a stop, target, manual close, fees, and slippage using observed prices. It locks new entries when that UTC day's realized plus open net paper PnL reaches the configured loss limit, when the session drawdown from the running equity peak reaches the configured threshold, when consecutive losses reach the configured count, or when the UTC-day trade count reaches the configured cap.
- A risk limit is never disabled by a data outage. Stops and targets keep being evaluated against the last observed price while a feed is stale, and the manual close button stays enabled, so a position can never be trapped by a broken feed.
- Closed trades are appended to a SQLite journal (`paper_trades.db`) as they close, newest first, and are restored on restart. Clearing the paper history clears the journal too.
- The paper panel reports profit factor, expectancy in USDT and in R, per-trade Sharpe and Sortino (unannualised mean-over-deviation ratios, because a scalping sample cannot support an annualised figure), average hold time, fees paid, and win/loss streaks.

## Dashboard layout

The dashboard is split into tabs so only one section is on screen at a time. The decision strip, the four headline metrics, and the data-health strip stay pinned above the tabs, so the current call is always visible without scrolling.

| Tab | Contents |
|---|---|
| **Context** | Open interest and its 60-second change, funding and the next settlement countdown, liquidations, venue lead-lag, and the one-minute estimate with its holdout accuracy and live hit rate |
| **Trade timing** | Setup state, reference entry/stop/target, and the thin-liquidity jump zones |
| **Charts** | Rolling 1/3/5-minute candles with marked levels, one-second microstructure with volume and CVD, and the embedded order-book heatmap |
| **Flow & book** | Aggressor flow, large prints, the institutional footprint scorecard, and the order book with its cumulative depth curve |
| **Paper trading** | Position panel, manual entry and close, session statistics, and the equity curve |
| **Signal tape** | Every setup the rule produced, its forward outcome, and the live forecast scorecard |
| **Research** | Historical backtest replay and the methodology notes |

The embedded heatmap is the standalone `heatmap.html`. It opens its own Binance and Coinbase websocket streams, so it runs independently of the polling loop and does not slow it down. Opening it is optional and collapsible.

## Signal tape

The dashboard shows a setup as soon as the order-flow signal and the candle model agree, but that alone does not say whether the rule pays. The signal tape closes that gap.

- Every **distinct** setup is recorded the moment it appears, whether or not a position is ever opened. A setup that keeps firing on successive refreshes is recorded once, and a new row is only written after the side changes or the previous row has resolved and the dedup window has passed.
- Each recorded row carries the entry, stop, and target implied by the configured stop distance and reward/risk ratio, plus the context that produced it: the flow signal, the forecast direction and score, the 60-second aggressor delta, the trade count, and the book imbalance.
- The tape then follows the observed perp trade price until the target is hit, the stop is hit, or the 15-minute horizon expires. It records the excursion in both directions, so a setup that went 30 bps in your favour before failing still shows that.
- Results are reported **net of the configured round-trip fees and slippage**, because a gross target hit that costs more to enter than the move is worth is not a win.
- The summary reports the number of setups, how many resolved, the target and stop counts, the target rate, total and average R, expectancy per setup, average maximum favourable and adverse excursion, and the best and worst rows.
- The tape persists across restarts and can be exported to CSV or cleared.

**This is a measurement of the rule, not a profit claim.** Given the findings below, a negative expectancy on the tape is the expected result for the current defaults, and that is exactly what the tape is for.

## Conviction score

The decision strip shows a 0-100 conviction score alongside the signal. It is a weighted summary of how much the current inputs agree: flow dominance, book imbalance, participation, model agreement, and footprint confidence, each measured as a ratio of the reading against the threshold you configured. A model that disagrees with the flow signal is heavily penalised. It is a measure of agreement, not a probability of profit, and it is zero whenever there is no actionable setup or the data is stale.

## Live forecast scorecard

Every forecast the dashboard displays is logged with the candle it was based on, then scored against the next candle once that candle closes. The Context tab shows this live hit rate next to the model's historical holdout accuracy, and the Signal tape tab reports it in full. A live hit rate below the holdout figure is surfaced as a warning, because it means the model is not currently working in this regime.

## Session durability

The SQLite database holds more than the closed-trade journal:

- The **open paper position** is written when it opens and removed when it closes, so a restart no longer silently discards a live position and its risk state.
- The **equity peak** behind the drawdown breaker is stored, so the breaker keeps working across restarts instead of forgetting the high-water mark.
- The **signal tape** and the **live forecast log** are written on a slow cadence rather than every three-second refresh, so the disk is not hit on every cycle.


## Measured findings

These numbers were produced by the tools in `tools/` over 180 days of Binance 1m candles (259,200 bars, 2026-04-22 to 2026-10-19). They are recorded here because the app's defaults were chosen before any of it was measured, and several of them are wrong in hindsight.

**The cost hurdle dominates everything else.** Median absolute move versus a 12 bps round trip (5 bps fee + 1 bps slippage, per side):

| horizon | median move | clears 12 bps cost? |
|---|---|---|
| 1m | 2.2 bps | no |
| 5m | 4.8 bps | no |
| 15m | 8.3 bps | no |
| 30m | 11.9 bps | no |
| 60m | 16.9 bps | yes |

At a 1-15 minute horizon the typical move is smaller than the cost of taking it, so a perfectly correct signal still loses money. With post-only maker fees (2 bps per side) the round trip is 6 bps and the 30 minute horizon becomes viable.

**Order flow has no forward predictive power at any tested horizon.** Over a 5x5 grid of signal windows (1/3/5/15/30m) and forward horizons (1/3/5/15/30/60m), every correlation was insignificant (|t| < 2) and every top-minus-bottom-decile spread was within a few basis points. A first version of that sweep looked excellent (correlations up to 0.44) but the forward-return window overlapped the signal window, so it was measuring contemporaneous order-flow/price correlation rather than prediction. `tools/diagnose_timeframes.py` now measures forward returns strictly after the signal window closes, and `tests/test_event_study.py` pins that with a look-ahead guard.

**The footprint event study finds nothing that survives correction.** 40 window/horizon/pattern combinations over 180 days: zero survive Benjamini-Hochberg FDR at q < 0.10, at taker fees and again at maker fees. The placebo test (randomly relabelling event times) confirms it. A 30-day run had shown absorption events hitting 54-64% and looked promising; over 180 days the same cells fall back to 46-52%, so that was small-sample noise. The dose response is flat: edge does not grow with event confidence, volume ratio, or delta magnitude, so trading only the strongest events does not help either.

**The one-minute forecast is worse than the baseline it is measured against.** On a held-out third of the same 180 days it scores 51.88% against a 52.23% majority-class baseline, and its score deciles are not monotonic (46.3% in the lowest decile, 48.8% in the highest, with a dip to 46.0% in the middle). Its score carries roughly half a basis point of forward information. Relabelling the model to other horizons improves the decile ordering (15m gives the cleanest gradient, Spearman +0.76) but the best top-to-bottom spread is still +0.62 bps, about 5% of the round-trip cost.

**Order flow is persistent even though it is not predictive.** The taker-delta series correlates +0.17 at one minute, +0.10 at two and decays to zero by one hour, so sustained aggression campaigns are real and visible in the data. The persistence does not transfer to forward returns, which means it is usable as a description of the tape (and as an urgency or regime filter) but not as a directional signal.

**The exit geometry is mis-sized.** At a 1 minute hold the median favourable excursion is 2.6-3.9 bps, while the configured stop is 20 bps and the target 40 bps. The stop is five to seven times the typical excursion, which is why 92% of the 30-day backtest's trades ended as time exits and fees were 85% of the total loss. Excursions only become comparable to a sane stop at 30-60 minute holds, where the measured favourable excursion is 10-24 bps.

**Consequences for the defaults.** The one-minute label, the 20 bps stop, and the 2R target are not supported by any of the above. The dashboard still runs them, so treat the paper results as an instrument for inspection rather than as evidence of a working strategy.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe tools/check_names.py app.py backtest.py forecast.py
.\.venv\Scripts\python.exe tools\event_study.py --days 180 --placebos 400
.\.venv\Scripts\python.exe tools\diagnose_timeframes.py --days 180
.\.venv\Scripts\python.exe tools\scan_forecast_horizons.py --days 180
```

- `tests/` covers the pure logic: flow summarisation, book summary, aggressor strength, absorption, lead-lag, the decision strip, the conviction score, the risk locks, the trade journal, the trade statistics, the signal tape, the live forecast tracker, and the event-study statistics. `tests/test_regressions.py` pins each defect fixed during the phase audit, so the same bug cannot come back silently.
- `tools/check_names.py` is a scope-aware undefined-name checker (pyflakes-style deferred binding). It catches names that are read but never bound, which is how the `aggressor_strength` `NameError` was found. It also reports unused imports.
- `tools/event_study.py` measures whether a footprint event predicts a forward move. It uses a non-overlapping sample grid per horizon, a sign-flip randomisation test, Benjamini-Hochberg FDR across the whole family of tests, a placebo relabelling check, and a dose-response breakdown.
- `tools/diagnose_timeframes.py` sweeps flow windows against forward horizons using every bar rather than only thresholded events, which gives far more statistical power. Its forward return starts strictly after the signal window closes.
- `tools/diagnose_forecast.py` and `tools/scan_forecast_horizons.py` check the forecast model against the majority-class baseline and score its decile monotonicity, at several label horizons.
- `tools/run_backtest.py`, `tools/sweep_backtest.py`, and `tools/baseline_compare.py` run the strategy headlessly, sweep its parameters, and compare it against random-entry and buy-and-hold baselines.

## Limitations

Public endpoints can rate-limit or become unavailable. Order-book liquidity can be canceled, and polling can miss short-lived changes. This tool does not model funding, liquidation, queue priority, partial fills, latency, exchange outages, or your actual fee tier. Paper fills are not evidence of live execution quality. Do not use it as financial advice or connect real funds based on these signals.