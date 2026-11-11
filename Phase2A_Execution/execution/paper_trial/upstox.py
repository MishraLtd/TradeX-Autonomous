"""Read-only Upstox data transport: GET allowlist + market-data subscription.

Deliberately no generic authenticated request method, order API, trading SDK,
OAuth app secret or live-mode fallback. Error messages never include responses,
tokens or signed WebSocket URLs.
"""
from datetime import datetime, timedelta
import gzip
import io
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import D, IST, TrialConfig, stamp, CURRENT_FEES

API = 'https://api.upstox.com'
ASSETS = 'https://assets.upstox.com/market-quote/instruments/exchange/'


class DataError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DataError('HTTP_REDIRECT_REFUSED')


class DataClient:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not 20 <= len(token) <= 8192 or any(c.isspace() for c in token):
            raise DataError('ANALYTICS_TOKEN_REQUIRED')
        self._token = token
        self._opener = opener  # Optional deterministic transport for offline tests.

    def _get(self, path):
        # Checked before attaching a credential or performing I/O.
        if not (path == '/v3/feed/market-data-feed/authorize' or
                re.fullmatch(r'/v2/market/timings/\d{4}-\d{2}-\d{2}', path)):
            raise DataError('DATA_ENDPOINT_NOT_ALLOWED')
        request = Request(API + path, method='GET', headers={
            'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'})
        try:
            if self._opener is not None:
                with self._opener.open(request, timeout=5) as response:
                    raw = response.read(2_000_001)
            else:
                # The VM diagnostic succeeds with requests while urllib receives
                # HTTP 403. Keep its standard client headers and TLS verification.
                import requests
                with requests.get(API + path, headers={
                        'Authorization': 'Bearer ' + self._token,
                        'Accept': 'application/json'}, timeout=5,
                        allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        raise DataError('HTTP_REDIRECT_REFUSED')
                    if response.status_code != 200:
                        raise DataError('UPSTOX_HTTP_' + str(response.status_code))
                    raw = response.raw.read(2_000_001, decode_content=True)
            if len(raw) > 2_000_000:
                raise DataError('DATA_RESPONSE_TOO_LARGE')
            body = json.loads(raw)
            if body.get('status') != 'success':
                raise DataError('DATA_RESPONSE_UNSUCCESSFUL')
            return body['data']
        except HTTPError as error:
            raise DataError('UPSTOX_HTTP_' + str(error.code)) from None
        except DataError:
            raise
        except Exception:
            raise DataError('UPSTOX_DATA_REQUEST_FAILED') from None

    def websocket_url(self):
        result = self._get('/v3/feed/market-data-feed/authorize')
        url = result.get('authorized_redirect_uri', '')
        parsed = urlsplit(url)
        if (parsed.scheme != 'wss' or not parsed.hostname or not parsed.hostname.endswith('.upstox.com') or
                parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            raise DataError('INVALID_MARKET_STREAM_URL')
        return url  # One use, in memory only. Never print/persist it.

    def timings(self, day):
        if datetime.fromisoformat(day).date().isoformat() != day:
            raise ValueError('Invalid date')
        data = self._get('/v2/market/timings/' + day)
        if not isinstance(data, list):
            raise DataError('INVALID_MARKET_CALENDAR')
        rows = [r for r in data if r.get('exchange') == 'NSE']
        if not rows:
            return None
        if len(rows) != 1:
            raise DataError('AMBIGUOUS_MARKET_CALENDAR')
        try:
            start, end = (float(D(rows[0][k])) / 1000 for k in ('start_time', 'end_time'))
            if (datetime.fromtimestamp(start, IST).date().isoformat() != day or
                    datetime.fromtimestamp(end, IST).date().isoformat() != day or start >= end):
                raise ValueError()
            return start, end
        except Exception:
            raise DataError('INVALID_MARKET_CALENDAR') from None


def regular_session(client, day):
    timing = client.timings(day)
    # This experiment excludes exceptional shortened/evening sessions. Never
    # substitute weekday arithmetic for the provider's exchange calendar.
    return bool(timing and timing[0] <= stamp(day, '09:15') and timing[1] >= stamp(day, '15:10'))


def load_instruments(name):
    if name not in ('NSE.json.gz', 'NSE_MIS.json.gz'):
        raise ValueError('Instrument file not allowed')
    try:
        with build_opener(NoRedirect()).open(ASSETS + name, timeout=10) as response:
            raw = response.read(32_000_001)
        if len(raw) > 32_000_000:
            raise ValueError()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            decoded = handle.read(64_000_001)
        if len(decoded) > 64_000_000:
            raise ValueError()
        result = json.loads(decoded)
        if not isinstance(result, list):
            raise ValueError()
        return result
    except Exception:
        raise DataError('INSTRUMENT_DOWNLOAD_FAILED') from None


def resolve_instruments(symbols, bod, mis):
    eligible = {r.get('instrument_key') for r in mis if r.get('segment') == 'NSE_EQ'}
    result = {}
    for index, symbol in enumerate(symbols):
        matches = [r for r in bod if r.get('segment') == 'NSE_EQ' and r.get('instrument_type') == 'EQ'
                   and r.get('trading_symbol') == symbol and r.get('security_type') == 'NORMAL']
        if len(matches) != 1 or matches[0]['instrument_key'] not in eligible:
            raise DataError('APPROVED_SYMBOL_NOT_UNIQUELY_MIS_ELIGIBLE')
        row = matches[0]
        # Upstox JSON master publishes tick_size in paise, feed prices in INR.
        result[symbol] = dict(key=row['instrument_key'], tick=str(D(row['tick_size']) / 100),
                              lot_size=row['lot_size'], mis_eligible=True, slot=index)
    return result


APPROVED_SYMBOLS = ('SBIN', 'ITC', 'HDFCBANK', 'KLBRENG-B', 'ADANIENT', 'BAJFINANCE', 'PAYTM', 'INDUSINDBK')
APPROVED_TIMES = ('09:30', '10:30', '11:30', '12:00', '12:30', '13:00', '13:30', '14:00')


def expanded_instruments():
    return resolve_instruments(APPROVED_SYMBOLS, load_instruments('NSE.json.gz'), load_instruments('NSE_MIS.json.gz'))


def prepare_config(client, now=None, symbols=APPROVED_SYMBOLS):
    now = time.time() if now is None else now
    client.websocket_url()  # Verify streaming authorization; discard signed URL.
    instruments = resolve_instruments(symbols, load_instruments('NSE.json.gz'), load_instruments('NSE_MIS.json.gz'))
    sessions = []
    today = datetime.fromtimestamp(now, IST).date()
    for offset in range(30):
        day = (today + timedelta(days=offset)).isoformat()
        if offset == 0 and now >= stamp(day, '09:25'):
            continue
        if regular_session(client, day):
            sessions.append(day)
        if len(sessions) == 5:
            return TrialConfig(instruments=instruments, sessions=sessions, entry_times=APPROVED_TIMES[:len(symbols)], fee_model=CURRENT_FEES,
                               exit_policy='NET_PROFIT_PRICE_STOP', recycle_entries=True)
    raise DataError('FIVE_REGULAR_SESSIONS_NOT_FOUND')


def analytics_token():
    if os.environ.get('TRADEX_MODE', 'upstox-paper') != 'upstox-paper':
        raise DataError('PAPER_TRIAL_MODE_REQUIRED')
    path = os.environ.get('UPSTOX_ANALYTICS_TOKEN_FILE')
    if path:
        p = Path(path)
        if stat.S_IMODE(p.stat().st_mode) & 0o007:
            raise DataError('TOKEN_FILE_MUST_BE_PRIVATE')
        return p.read_text().strip()
    vault = os.environ.get('TRADEX_VAULT_URL', '')
    if not re.fullmatch(r'https://[a-zA-Z0-9-]+\.vault\.azure\.net/?', vault):
        raise DataError('KEY_VAULT_URL_REQUIRED')
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient
        with ManagedIdentityCredential() as credential, SecretClient(
                vault_url=vault, credential=credential, connection_timeout=5, read_timeout=5,
                retry_total=0) as client:
            return client.get_secret('upstox-analytics-token').value
    except Exception:
        raise DataError('KEY_VAULT_ANALYTICS_TOKEN_UNAVAILABLE') from None


class Decoder:
    """Per-connection decoder. First book per symbol is always a snapshot,
    even if the provider labels it live_feed; it cannot execute paper orders.
    """
    def __init__(self, instruments):
        self.symbols = {v['key']: k for k, v in instruments.items()}
        self.seen = set()
        self.normal = False
        self.last_frame = 0

    def decode(self, raw, received):
        from .MarketDataFeed_pb2 import FeedResponse
        from google.protobuf.json_format import MessageToDict
        message = FeedResponse()
        if not isinstance(raw, bytes) or len(raw) > 1_000_000:
            raise DataError('INVALID_MARKET_FRAME')
        try:
            message.ParseFromString(raw)
            return self.normalize(MessageToDict(message, preserving_proto_field_name=True), received)
        except DataError:
            raise
        except Exception:
            raise DataError('INVALID_MARKET_FRAME') from None

    def normalize(self, message, received):
        at = float(D(message.get('currentTs', 0))) / 1000
        if not -2 <= received - at <= 15 or at < self.last_frame:
            raise DataError('STALE_OR_REVERSED_MARKET_FRAME')
        self.last_frame = at
        info = message.get('marketInfo')
        if info is not None:
            segment = info.get('segmentStatus', {}).get('NSE_EQ')
            cas = info.get('casMarketStatus', {}).get('NSE_EQ', {}).get('status')
            pre = info.get('preOpenSessionStatus', {}).get('NSE_EQ', {}).get('status')
            self.normal = segment == 'NORMAL_OPEN' and cas in (None, 'NORMAL_OPEN') and pre in (None, 'PRE_OPEN_END', 'NORMAL_OPEN')
        quotes = {}
        for key, row in message.get('feeds', {}).items():
            if key not in self.symbols:
                continue
            full = row.get('fullFeed', {}).get('marketFF', {})
            ltpc, levels = full.get('ltpc', {}), full.get('marketLevel', {}).get('bidAskQuote', [])
            # Do not assemble cached book fragments or treat auction IEP as LTP.
            if not levels or 'iep' in ltpc:
                continue
            try:
                level = levels[0]
                traded = float(D(ltpc['ltt'])) / 1000
                if not -2 <= received - traded <= 15 or traded > at + 2:
                    continue
                symbol = self.symbols[key]
                quotes[symbol] = dict(at=at, received=received, traded=traded,
                    last=str(D(ltpc['ltp'])), bid=str(D(level['bidP'])), ask=str(D(level['askP'])),
                    bid_qty=int(level.get('bidQ', 0)), ask_qty=int(level.get('askQ', 0)),
                    live=key in self.seen and message.get('type') == 'live_feed')
                self.seen.add(key)
            except (KeyError, ValueError, TypeError):
                continue
        return quotes
