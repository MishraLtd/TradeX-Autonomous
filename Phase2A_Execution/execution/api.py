"""Private operator API. Run alongside the exit worker; access via SSH tunnel."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
import re
import time

from .models import dumps, positive


class EntryService:
    def __init__(self, config, store, prepare, clock=time.time):
        self.config, self.store, self.prepare, self.clock = config, store, prepare, clock

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'request_id', 'stock', 'budget'}:
            raise ValueError('Exactly request_id, stock and budget are required')
        request_id, stock, budget = (payload[k] for k in ('request_id', 'stock', 'budget'))
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            raise ValueError('request_id must contain 1..128 letters, digits, _, ., : or -')
        if not isinstance(stock, str) or stock not in self.config.stocks:
            raise ValueError('Stock is outside the approved universe')
        if not isinstance(budget, str) or not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,2})?', budget):
            raise ValueError('budget must be a positive decimal string with at most two decimal places')
        amount = positive(budget)
        # Read-back works even after a restart/session expiry. Never resubmit a
        # persisted request whose broker outcome might still be unknown.
        with self.store.locked():
            old = next((t for t in self.store.all() if t['id'] == request_id), None)
            if old:
                if old['stock'] != stock or positive(old['budget']) != amount:
                    raise ValueError('Request id already used with different inputs')
                return dict(replayed=True, trade=old)
            heartbeat = self.store.get('heartbeat')
            if (not isinstance(heartbeat, (int, float)) or not math.isfinite(heartbeat) or
                    not 0 <= self.clock() - heartbeat <= self.config.health_max_age):
                raise ValueError('Start or recover the exit worker before requesting an entry')
        trade = self.prepare().request_entry(request_id, stock, budget)
        return dict(replayed=False, trade=trade)


def make_server(service, token, port=8765):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
        raise ValueError('TRADEX_API_TOKEN must be a random 32..256 character URL-safe token')

    class Handler(BaseHTTPRequestHandler):
        # Close every connection; bound slow/incomplete requests and payloads.
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            pass  # Never log credentials, request bodies or URL query strings.

        def reply(self, status, body):
            data = dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def authorized(self):
            values = self.headers.get_all('Authorization', [])
            if (len(values) != 1 or not hmac.compare_digest(values[0].encode(), ('Bearer ' + token).encode())):
                self.reply(401, dict(error='UNAUTHORIZED'))
                return False
            # No browser cross-origin requests, cookies, or URL tokens.
            if self.headers.get('Origin') is not None:
                self.reply(403, dict(error='BROWSER_ORIGIN_NOT_ALLOWED'))
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != '/v1/universe':
                return self.reply(404, dict(error='NOT_FOUND'))
            self.reply(200, dict(stocks=sorted(service.config.stocks),
                                 per_trade_limit=service.config.per_trade_limit,
                                 capital_limit=service.config.capital_limit,
                                 profit_pct=service.config.profit_pct, stop_pct=service.config.stop_pct))

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != '/v1/entries':
                return self.reply(404, dict(error='NOT_FOUND'))
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
                return self.reply(400, dict(error='CONTENT_LENGTH_REQUIRED'))
            length = int(lengths[0])
            if not 1 <= length <= 4096:
                return self.reply(413, dict(error='BODY_TOO_LARGE'))
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self.reply(415, dict(error='JSON_REQUIRED'))
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete body')
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('Duplicate JSON field')
                        result[key] = value
                    return result
                payload = json.loads(raw, object_pairs_hook=unique)
            except (ValueError, UnicodeError, TimeoutError):
                return self.reply(400, dict(error='INVALID_JSON'))
            try:
                result = service.submit(payload)
            except ValueError as exc:
                return self.reply(409, dict(error='ENTRY_BLOCKED', detail=str(exc)))
            except Exception:
                # A broker request may already have been sent. Retry ONLY the
                # same request_id and payload; never claim that no order exists.
                return self.reply(503, dict(error='UNAVAILABLE_OR_OUTCOME_UNKNOWN',
                                           detail='Retry the same request_id and payload; inspect worker health.'))
            self.reply(200 if result['replayed'] else 202, result)

    return HTTPServer(('127.0.0.1', port), Handler)


def serve(service, port):
    with make_server(service, os.environ.get('TRADEX_API_TOKEN'), port) as server:
        print(dumps(dict(event='entry_api_ready', host='127.0.0.1', port=server.server_port)), flush=True)
        server.serve_forever()
