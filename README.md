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
- The displayed basis is the nominal percentage difference between those two quoted prices. It is only a comparison, not a tradable cross-venue spread after fees, FX, and funding.
- The dashboard polls every three seconds. Trade delta is calculated over the most recent 60 seconds of fetched Binance aggregate trades. Book imbalance uses the selected number of visible price levels on each side; the book is a snapshot, not a queue-position feed.
- **Next one-minute candle estimate:** A regularized logistic model uses completed Binance BTCUSDT one-minute candles and features from 1/3/5-minute returns, candle body, and relative volume. It predicts whether the next candle will close above the latest completed close. The dashboard reports a recent chronological holdout accuracy and majority-class baseline; the displayed model score is not a calibrated probability. It returns `NO CLEAR EDGE` near a 50/50 score.

## Signals and paper risk

- **LONG:** 60-second taker-buy delta divided by total traded BTC exceeds the selected flow threshold, and top-book bid-size imbalance exceeds the selected book threshold.
- **SHORT:** both conditions are met in the negative direction. Otherwise the signal is **FLAT**.
- This deliberately simple, inspectable conjunction is a research heuristic, not a backtested or predictive strategy. A possible absorption note appears when one-sided aggression is strong but the observed 60-second price move stays below 3 bps; that is also only a heuristic.
- The candle model is experimental and can perform worse than its baseline or change behavior across regimes. Its recent holdout score is not a guarantee of future performance and is not financial advice.
- A paper long/short setup is shown only when the one-minute estimate agrees with the live order-flow signal and the daily loss limit is not hit. The corresponding manual paper-entry button is enabled only for that aligned setup; no order is ever placed automatically.
- By default, paper entries are also blocked whenever recent holdout accuracy is below the majority baseline. This reduces exposure when the model lacks evidence of an edge; it cannot establish or guarantee profitability.
- The live BTCUSDT trade-price chart shows a rolling five-minute window with UTC time on the horizontal axis and refreshes every three seconds. The latest observed trade time is printed below it.
- The historical backtest starts at 10 USDT by default and downloads 7 or 30 days of Binance one-minute candles. Its default five-fold walk-forward mode uses expanding chronological training data and five sequential, non-overlapping test windows; each fold starts with the selected balance for comparison. Single 80/20 holdout remains available. The model fits on at most the latest 600 labeled bars before each test fold. By default, entries are blocked unless that fold's earlier validation beats its majority baseline. It applies selected sizing, fees, slippage, stops/targets, flow thresholds, and forecast cutoff. Since historical order-book imbalance is not supplied by the kline feed, it is a bar-only approximation, not an exact replay of live entry rules. The $10 balance is paper-only and does not imply any real exchange minimum order size.
- The backtest includes fixed model-score cutoffs with held-out hit rates and sample counts. These scores are not calibrated probabilities; small-sample high hit rates are not evidence of a reliable 90% strategy.
- Position entry is manual. The signal never trades automatically. Quantity is limited by the risk budget divided by estimated stop loss per BTC (including configured round-trip fees and slippage), and by the maximum notional/equity setting.
- The app simulates a stop, target, manual close, fees, and slippage using observed prices. It locks new entries when that UTC day's realized plus open net paper PnL reaches the configured loss limit.
- Paper positions and closed trades live in Streamlit's in-memory session state. They are cleared when the app session is reset or the server restarts; there is no database or durable ledger.

## Limitations

Public endpoints can rate-limit or become unavailable. Order-book liquidity can be canceled, and polling can miss short-lived changes. This tool does not model funding, liquidation, queue priority, partial fills, latency, exchange outages, or your actual fee tier. Paper fills are not evidence of live execution quality. Do not use it as financial advice or connect real funds based on these signals.