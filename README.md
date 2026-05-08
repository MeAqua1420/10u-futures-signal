# 10U Binance USD-M Futures Signal System

This project implements a research-first terminal signal scanner for Binance USD-M perpetual futures.

It is not an auto-trader. It prints candidate signals only after a closed 1m candle and includes:

`time_utc/time_cn, symbol, side, leverage, margin_usdt, entry_reference, take_profit_price, stop_price, target_pnl, max_loss, expires_at, score, reason_codes`

The main deployable strategy uses `10USDT` isolated margin, `+5U` gross take profit, `-2U` gross hard stop, and a maximum hold time of `4h`. The original `+5/-10` liquidation-style stop is implemented only as a backtest comparison.

## Quick Start

Create and activate the conda environment:

```bash
conda env create -f environment.yml
conda activate 10U
python -m pip install -e . --no-deps --no-build-isolation
```

If conda/PyPI SSL verification fails on this machine, the working offline fallback is:

```bash
conda create --clone base -n 10U -y
conda run -n 10U python -m pip install -e . --no-deps --no-build-isolation
```

Or run commands without activating:

```bash
conda run -n 10U python -m ten_u.cli --help
```

Run unit tests:

```bash
conda run -n 10U python -m pytest
```

Show the current Binance liquidity pool:

```bash
conda run -n 10U python -m ten_u.cli symbols --top 20
```

Run a small smoke backtest:

```bash
conda run -n 10U python -m ten_u.cli backtest --symbols BTCUSDT ETHUSDT --days 14 --grid quick --strategy manuscript
```

Run the full intended research pass:

```bash
conda run -n 10U python -m ten_u.cli backtest --top 60 --days 365 --grid full --walk-forward --strategy manuscript
```

Start a REST polling terminal scanner:

```bash
conda run -n 10U python -m ten_u.cli realtime --top 60 --lookback 720 --poll-seconds 15 --strategy manuscript
```

WebSocket support is included in the conda environment through `websocket-client`:

```bash
conda run -n 10U python -m ten_u.cli realtime --top 60 --mode ws --strategy manuscript
```

## OKX Demo Trading

OKX demo trading uses the same strategy engine, but reads OKX `SWAP` candles and submits orders to OKX V5 with the simulated-trading header. The execution command is safe by default: it prints a dry-run order plan unless `--execute` is provided.

The OKX scanner uses only confirmed/closed candles. The default is now `--risk-profile balanced` on `--bar 1m`, which relaxes the previous conservative cap while still avoiding unclosed bars:

- `balanced`: max leverage `15x`, `RangeY >= 40`, `PSY >= 0.35`, mean deviation `>= 0.0020`
- `conservative`: max leverage `10x`, `RangeY >= 50`, `PSY >= 0.40`, mean deviation `>= 0.0025`
- `standard`: research defaults, max leverage `15x`
- `aggressive`: max leverage `20x`, looser gates; use only for deliberate higher-frequency testing
- `scalp-1s`: forces confirmed `1s` candles, targets `+2U/-1U`, max leverage `50x` but clamps to the OKX instrument leverage limit; v2 adds 5-minute direction confirmation and a default 5-minute post-order cooldown
- `weekend-1s`: experimental `microburst` profile for US/Eastern non-workdays, targets `+1U/-0.6U`, uses `30x-55x`, and exits after at most `180s`

OKX currently accepts `--bar 1s` and `--bar 1m` here. `1s` can produce more opportunities, but it is much noisier and is not the original backtested timeframe.

The OKX symbol pool filters to crypto USDT swaps only (`instCategory=1`) and excludes `TEST*` instruments, so demo-only stock/test swaps such as `AAPL-USDT-SWAP` do not enter `--top` scans.

Create an OKX demo API key in OKX and export credentials:

```bash
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
```

Inspect OKX USDT swap symbols:

```bash
conda run -n 10U ten-u okx-symbols --top 20
```

Generate the current best OKX signal without trading:

```bash
conda run -n 10U ten-u okx-signal --top 20 --strategy manuscript
```

Keep scanning for OKX signals until you stop it with `Ctrl-C`:

```bash
conda run --no-capture-output -n 10U ten-u okx-signal --top 20 --strategy manuscript --loop --poll-seconds 60
```

Prepare an OKX demo order plan without sending it:

```bash
conda run -n 10U ten-u okx-demo --top 20 --strategy manuscript
```

Actually place the selected signal in OKX demo trading:

```bash
conda run -n 10U ten-u okx-demo --top 20 --strategy manuscript --execute
```

For accounts using long/short position mode:

```bash
conda run -n 10U ten-u okx-demo --top 20 --pos-mode long-short --execute
```

Keep scanning the OKX demo market until you stop it with `Ctrl-C`. In dry-run mode it keeps printing plans; in `--execute` mode it keeps scanning after an order is sent and skips the same `symbol + side` until that signal expires:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --pos-mode long-short --loop --poll-seconds 60
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --pos-mode long-short --loop --poll-seconds 60 --execute
```

For more signals, run the balanced profile on confirmed `1s` candles:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --bar 1s --risk-profile balanced --pos-mode long-short --loop --poll-seconds 5 --execute
```

To measure the scanner's minimum loop latency, set `--poll-seconds 0`. This removes the fixed sleep; each printed scan includes `scan_duration_seconds`:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --bar 1s --risk-profile balanced --pos-mode long-short --loop --poll-seconds 0
```

For the loosest current test profile:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --bar 1s --risk-profile aggressive --pos-mode long-short --loop --poll-seconds 5 --execute
```

For the dedicated `1s` scalping profile with `+2U/-1U`:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --risk-profile scalp-1s --pos-mode long-short --loop --poll-seconds 0 --execute
```

For the experimental US/Eastern non-workday hyper-scalp profile:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy microburst --risk-profile weekend-1s --pos-mode long-short --loop --poll-seconds 0 --execute
```

`weekend-1s` only emits or executes signals when the current US/Eastern date is a Saturday, Sunday, or NYSE full holiday. On regular NYSE workdays it prints `MARKET_DAY_FILTERED`. It uses `1s` closed candles, a `+1U/-0.6U` attached TP/SL, allows only one managed position at a time, and sends an OKX simulated `close-position` request when the 180-second max hold expires.

Search OKX `1s` history for US/Eastern non-workday factor combinations:

```bash
conda run -n 10U ten-u okx-weekend-backtest --top 20 --weekends 8 --grid full --min-oos-trades 100
```

For a faster exploratory run, fetch only the most recent N US/Eastern non-workdays:

```bash
conda run -n 10U ten-u okx-weekend-backtest --symbols ETH-USDT-SWAP --non-workdays 3 --grid quick --min-oos-trades 1
```

`scalp-1s` defaults to `--trade-cooldown-seconds 300` after every accepted order. You can override this, but setting it to `0` is only recommended for latency tests, not strategy evaluation:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --top 20 --strategy manuscript --risk-profile scalp-1s --pos-mode long-short --loop --poll-seconds 0 --trade-cooldown-seconds 0
```

BTC and ETH open fewer trades under the top-20 scanner because they usually have lower relative ATR and cleaner order books than high-beta alts, while the strategy requires both directional Heikin-Ashi structure and a reachable target/stop geometry. The scanner also emits only the best signal at each scan. To test majors directly, restrict the symbol set:

```bash
conda run --no-capture-output -n 10U ten-u okx-demo --symbols BTC-USDT-SWAP ETH-USDT-SWAP --strategy manuscript --risk-profile scalp-1s --pos-mode long-short --loop --poll-seconds 0 --execute
```

When you stop the loop with `Ctrl-C`, the program prints `SESSION_SUMMARY` with scan counts, accepted/rejected orders, closed-trade win rate, realized PnL from OKX fills, and current open-position unrealized PnL when API read permission is available. Realized PnL is based on OKX `fills-history`; avoid manual trades on the same instruments during one bot session if you want the session report to stay clean.

The OKX order plan uses isolated margin, market entry, `10USDT` margin sizing, the strategy-selected leverage, and attached market TP/SL orders. The `weekend-1s` runtime also manages its own short time exit; other profiles still only print `expires_at`.

## Manuscript Strategy

The default `manuscript` signal model follows the uploaded strategy notes:

- double-smoothed Heikin Ashi candles
- signed `RangeY` from the prior smoothed candle body ranges
- `PSY` bullish smoothed-candle ratio
- 5-bar smoothed mean deviation
- entry only when the trend body-ratio condition and mean-deviation momentum condition confirm each other

Current tuned defaults from the 90d top-5 Binance USD-M test:

- `max_loss_usdt=2`
- `max_leverage=15`
- `ha_range_window=30`
- `ha_range_y_threshold=30`
- `ha_deviation_threshold=0.0015`
- `ha_score_threshold=100`

Latest local 90d top-5 backtest result:

- out-of-sample: `238` trades, `41.18%` win rate, `+0.251932U/trade` expectancy, `1.2109` profit factor
- full period: `787` trades, `41.68%` win rate, `+0.112685U/trade` expectancy, `1.1021` profit factor
- deployment gate remains `NO_DEPLOYABLE_SIGNAL_RULESET` because win rate is below `60%`

## Deployment Gate

The optimizer refuses to mark a strategy deployable unless the frozen out-of-sample result satisfies all of:

- win rate `>= 60%`
- out-of-sample trades `>= 100`
- net expectancy `> 0`
- profit factor `>= 1.2`

If those conditions are not met, the program prints `NO_DEPLOYABLE_SIGNAL_RULESET` instead of pretending the strategy is valid.

## Notes

- Default cost model is taker fee `0.05%` per side and slippage `0.03%` per side.
- Backtests enter on the next 1m candle open after a signal.
- If take-profit and stop-loss touch in the same candle, the stop-loss is filled first.
- Time is stored internally in UTC and printed with both UTC and China Standard Time.
