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
