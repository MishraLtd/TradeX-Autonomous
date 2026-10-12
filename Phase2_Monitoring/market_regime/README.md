# TradeX Market Regime Model

A dependency-free (pure Python stdlib) market regime detector for the
TradeX low-capital NSE trading system. See `docs/ARCHITECTURE.md` for the
full design record, rationale, and an honest list of what is / isn't
validated yet.

## Quickstart

```python
from market_regime.inference import RegimeEngine
from market_regime.schemas import Bar, InstrumentSeries
from market_regime.enums import Timeframe

# Build an InstrumentSeries per timeframe from your OHLCV data.
# `Bar.timestamp` must be the bar's CLOSE time (unix epoch seconds).
bars = tuple(Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)
             for ts, o, h, l, c, v in your_ohlcv_rows)
nifty_daily = InstrumentSeries(symbol="NIFTY50", timeframe=Timeframe.D1, bars=bars)

engine = RegimeEngine()
state = engine.get_current_regime(
    {Timeframe.D1: nifty_daily},
    symbol_scope="MARKET:NIFTY50",
)

print(state.market_regime, state.market_regime_confidence)
print(engine.get_position_multiplier(state))
print(engine.should_allow_new_trade(state, strategy="momentum"))
print(state.to_json())
```

Pass more timeframes (`Timeframe.M15`, `Timeframe.H1`, ...) in the same
dict for multi-timeframe alignment, and `sector_series=`/`stock_series=`
for market/sector/stock cross-level context (§4 of the spec).

## Run the tests

```bash
python3 -m unittest discover -t . -s tests -v
```

34 tests, all passing, zero third-party dependencies required.

## Database

`db/schema.sql` has the Postgres/Supabase DDL. `market_regime/persistence.py`
builds parameterized `(sql, params)` tuples against it — wire those into
whatever DB client TradeX already uses. Nothing in this package opens a DB
connection itself.

## Before this touches real capital

Read the "Honesty flag" section in `docs/ARCHITECTURE.md`. All numeric
thresholds are uncalibrated priors (`config.CONFIG_VERSION =
"v0-uncalibrated"`). Run shadow mode → paper trade → walk-forward backtest
showing positive incremental value over a no-regime baseline, in that
order, before it influences a live order.
