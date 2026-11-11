"""Frozen, explicit trial inputs. Money is in INR; times are Asia/Kolkata."""
from dataclasses import dataclass, asdict
from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
MODE = 'UPSTOX_DATA_LOCAL_PAPER'
LEGACY_FEES = 'upstox-basic-nse-intraday-estimate-2026-11-10'
CURRENT_FEES = 'upstox-basic-nse-intraday-estimate-2026-11-11'


def implementation_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.iterdir()):
        if path.suffix in ('.py', '.proto'):
            digest.update(path.name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def D(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite number')
    return result


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def stamp(day, hhmm):
    return datetime.combine(datetime.fromisoformat(day).date(), time.fromisoformat(hhmm), IST).timestamp()


def price_tick(value, tick, up):
    tick = D(tick)
    return (D(value) / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


@dataclass(frozen=True)
class TrialConfig:
    # Filled by read-only preflight, never by a trading endpoint.
    instruments: dict
    sessions: list
    mode: str = MODE
    initial_cash: str = '4000'
    per_trade_limit: str = '2000'
    max_positions: int = 3
    profit_pct: str = '2'
    stop_pct: str = '1'
    daily_loss_limit: str = '100'
    entry_times: tuple = ('09:30', '10:30', '11:30')
    flatten_time: str = '15:00'
    hard_close_time: str = '15:10'
    max_quote_age: float = 15
    entry_window: float = 60
    latency_seconds: float = 0.25
    exit_response_timeout: float = 60
    slippage_bps: str = '5'
    depth_participation: str = '0.10'
    fee_model: str = LEGACY_FEES
    # Legacy defaults keep old reports/config hashes reproducible. New trials
    # and upgrades select NET_PROFIT_PRICE_STOP with recycling enabled.
    exit_policy: str = 'FIXED_PERCENT'
    net_profit_inr: str = '1'
    net_loss_inr: str = '2'
    recycle_entries: bool = False
    price_loss_inr: str = '2'

    def __post_init__(self):
        if self.mode != MODE or self.fee_model not in (LEGACY_FEES, CURRENT_FEES):
            raise ValueError('Only the reviewed local paper mode and fee estimate are supported')
        if self.exit_policy not in ('FIXED_PERCENT', 'NET_CASH', 'NET_PROFIT_PRICE_STOP') or type(self.recycle_entries) is not bool:
            raise ValueError('Invalid paper exit policy')
        if self.recycle_entries and self.exit_policy == 'FIXED_PERCENT':
            raise ValueError('Recycling requires the reviewed net-cash exit policy')
        if any(D(v) <= 0 for v in (self.net_profit_inr, self.net_loss_inr, self.price_loss_inr)):
            raise ValueError('Positive whole-trade profit and loss thresholds required')
        if not 0 < D(self.per_trade_limit) <= D(self.initial_cash):
            raise ValueError('Invalid account or trade budget')
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 3:
            raise ValueError('At most three paper positions')
        if len(self.instruments) != 3 or len(self.entry_times) != 3:
            raise ValueError('This trial requires three approved NSE equities and three entry slots')
        keys = set()
        if {row.get('slot') for row in self.instruments.values()} != {0, 1, 2}:
            raise ValueError('Instrument slots must be 0, 1 and 2')
        for symbol, row in self.instruments.items():
            if not re.fullmatch(r'[A-Z0-9&_.-]+', symbol):
                raise ValueError('Invalid symbol')
            if not re.fullmatch(r'NSE_EQ\|INE[A-Z0-9]{9}', row['key']):
                raise ValueError('Only NSE cash equity instrument keys are permitted')
            if row['key'] in keys or not 0 < D(row['tick']) <= 1 or row.get('lot_size') != 1:
                raise ValueError('Invalid instrument metadata')
            if row.get('mis_eligible') is not True:
                raise ValueError('Intraday eligibility must be checked')
            keys.add(row['key'])
        if len(self.sessions) != 5 or self.sessions != sorted(set(self.sessions)):
            raise ValueError('Exactly five distinct planned sessions, in date order, are required')
        for day in self.sessions:
            if datetime.fromisoformat(day).date().isoformat() != day:
                raise ValueError('Invalid session date')
        ordered = tuple(self.entry_times) + (self.flatten_time, self.hard_close_time)
        if ordered != tuple(sorted(set(ordered))) or ordered[0] < '09:15' or ordered[-1] > '15:10':
            raise ValueError('Invalid intraday timetable')
        for hhmm in ordered:
            if time.fromisoformat(hhmm).strftime('%H:%M') != hhmm:
                raise ValueError('Use HH:MM times')
        for val in (self.profit_pct, self.stop_pct, self.daily_loss_limit):
            if D(val) <= 0:
                raise ValueError('Positive risk thresholds required')
        if D(self.stop_pct) >= 100 or D(self.profit_pct) >= 50:
            raise ValueError('Invalid target or stop')
        if not 0 <= D(self.slippage_bps) <= 100 or not 0 < D(self.depth_participation) <= 1:
            raise ValueError('Invalid simulation assumptions')
        for val in (self.max_quote_age, self.entry_window, self.latency_seconds, self.exit_response_timeout):
            if isinstance(val, bool) or not math.isfinite(val) or val <= 0:
                raise ValueError('Positive finite timing required')
        if self.max_quote_age > 15 or self.entry_window > 120 or self.latency_seconds > 10 or self.exit_response_timeout > 120:
            raise ValueError('Unsafe timing configuration')

    @property
    def fingerprint(self):
        values = asdict(self)
        # Preserve both previous fixed-percent and NET_CASH config hashes.
        if self.exit_policy != 'NET_PROFIT_PRICE_STOP' and self.price_loss_inr == '2':
            values.pop('price_loss_inr')
        if (self.exit_policy == 'FIXED_PERCENT' and not self.recycle_entries
                and self.net_profit_inr == '1' and self.net_loss_inr == '2'):
            for key in ('exit_policy', 'net_profit_inr', 'net_loss_inr', 'recycle_entries'):
                values.pop(key)
        return hashlib.sha256(dumps(values).encode()).hexdigest()

    @classmethod
    def load(cls, path):
        with open(path) as handle:
            return cls(**json.load(handle))
