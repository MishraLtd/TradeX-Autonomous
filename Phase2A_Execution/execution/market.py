"""Bounded, operator-reviewable exchange schedules; never guess an unknown date."""
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class MarketClosed(RuntimeError):
    pass


class MarketCalendar:
    def __init__(self, data):
        if data.get("schema") != 1 or data.get("timezone") != "Asia/Kolkata":
            raise ValueError("Unsupported exchange calendar schema/timezone")
        self.start, self.end = (date.fromisoformat(data[k]) for k in ("valid_from", "valid_through"))
        if self.start > self.end or (self.end - self.start).days > 366:
            raise ValueError("Calendar coverage must be at most one year")
        self.exchanges = data["exchanges"]
        if not self.exchanges or set(self.exchanges) - {"NSE", "BSE"}:
            raise ValueError("Unsupported exchange calendar")
        for spec in self.exchanges.values():
            if not spec.get("sources"):
                raise ValueError("Calendar requires reviewed source references")
            self._window(spec["regular"])
            for day in spec["holidays"]:
                self._date(day)
            for day, window in spec.get("special", {}).items():
                self._date(day)
                self._window(window)
            for day in spec.get("unverified", []):
                self._date(day)
        self.data = data

    def _date(self, value):
        day = date.fromisoformat(value)
        if not self.start <= day <= self.end:
            raise ValueError("Calendar exception is outside coverage")

    @staticmethod
    def _window(window):
        opening, closing = (time.fromisoformat(v) for v in window)
        if opening.tzinfo or closing.tzinfo or not opening < closing:
            raise ValueError("Execution window must be ordered local times in one day")
        return opening, closing

    @classmethod
    def load(cls, path=None):
        with open(path or Path(__file__).with_name("calendar.json")) as handle:
            return cls(json.load(handle))

    def window(self, exchange, day):
        spec = self.exchanges.get(exchange)
        if not spec or not self.start <= day <= self.end or day.isoformat() in spec.get("unverified", []):
            return None  # unknown; distinct from a verified closed day
        special = spec.get("special", {}).get(day.isoformat())
        if special is None and (day.weekday() >= 5 or day.isoformat() in spec["holidays"]):
            return ()
        return tuple(datetime.combine(day, t, IST).timestamp()
                     for t in self._window(special or spec["regular"]))

    def status(self, exchange, now):
        day = datetime.fromtimestamp(now, IST).date()
        window = self.window(exchange, day)
        state, next_open, close = "UNKNOWN", None, None
        if window is not None:
            state = "CLOSED"
            if window:
                opening, close = window
                if opening <= now < close:
                    state = "OPEN"
                elif opening - 45 * 60 <= now < opening:
                    state = "PREMARKET"
                if now < opening:
                    next_open = opening
            cursor = day + timedelta(days=1)
            while next_open is None and cursor <= self.end:
                future = self.window(exchange, cursor)
                if future is None:
                    break  # do not skip an unverified special session
                if future:
                    next_open = future[0]
                cursor += timedelta(days=1)
        return dict(exchange=exchange, trading_date=day.isoformat(), state=state,
                    next_open=next_open, execution_close=close, checked_at=now,
                    valid_through=self.end.isoformat())

    def require_open(self, stock, now, buffer=10):
        state = self.status(stock.split(":")[0], now)
        if state["state"] != "OPEN" or state["execution_close"] - now <= buffer:
            raise MarketClosed("Outside a verified continuous-trading execution window")
        return state

    def snapshot(self, stocks, now):
        return {exchange: self.status(exchange, now) for exchange in sorted({s.split(":")[0] for s in stocks})}
