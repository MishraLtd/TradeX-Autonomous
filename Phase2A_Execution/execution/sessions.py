"""Supported Kite login, private token receipts and reloadable guarded SDK calls.

No password/OTP automation, refresh-token assumption, or trading POST retry.
Only safe session metadata is written to the execution journal.
"""
from datetime import datetime, time as wall_time, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from .market import IST


class SessionUnavailable(RuntimeError):
    pass


def login_epoch(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid Kite login time") from None
    if not isinstance(value, datetime):
        raise ValueError("Missing Kite login time")
    return value.replace(tzinfo=IST).timestamp() if value.tzinfo is None else value.timestamp()


def expires_at(issued):
    day = datetime.fromtimestamp(issued, IST).date() + timedelta(days=1)
    return datetime.combine(day, wall_time(6), IST).timestamp()


def key_digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def save_session(path, response, api_key, expected_account, now):
    """Save only the required token and identity, after a supported token exchange."""
    if response.get("user_id") != expected_account or response.get("api_key") != api_key:
        raise ValueError("Login returned a different account or API key")
    token = response.get("access_token")
    issued = login_epoch(response.get("login_time"))
    if not isinstance(token, str) or not token or issued > now + 2 or expires_at(issued) <= now:
        raise ValueError("Login returned an invalid or expired session")
    receipt = dict(schema=1, account_id=expected_account, api_key_hash=key_digest(api_key),
                   access_token=token, issued_at=issued, expires_at=expires_at(issued))
    target = Path(path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError("Session path must not be a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".kite-session-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {k: v for k, v in receipt.items() if k not in ("access_token", "api_key_hash")}


def read_session(path, api_key, expected_account, now, environ=None):
    environ = os.environ if environ is None else environ
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # Migration path for an explicitly dated environment token; no inferred issue date.
        token, issued = environ.get("KITE_ACCESS_TOKEN"), environ.get("KITE_ACCESS_TOKEN_ISSUED_AT")
        if not token or not issued:
            raise SessionUnavailable("SESSION_MISSING: complete login and save the private session file") from None
        try:
            issued = login_epoch(issued)
        except (TypeError, ValueError):
            raise SessionUnavailable("SESSION_INVALID: invalid environment token issue time") from None
        data = dict(schema=1, account_id=expected_account, api_key_hash=key_digest(api_key),
                    access_token=token, issued_at=issued, expires_at=expires_at(issued))
    except OSError:
        raise SessionUnavailable("SESSION_INVALID: session file cannot be safely opened") from None
    else:
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                    info.st_uid != os.getuid() or info.st_size > 16384):
                raise ValueError("Unsafe session file")
            with os.fdopen(fd, "r") as handle:
                fd = None
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            raise SessionUnavailable("SESSION_INVALID: session file must be valid private JSON owned by this user (0600)") from None
        finally:
            if fd is not None:
                os.close(fd)
    try:
        issued, expiry = float(data["issued_at"]), float(data["expires_at"])
        if (data["schema"] != 1 or data["account_id"] != expected_account or
                data["api_key_hash"] != key_digest(api_key) or
                not isinstance(data["access_token"], str) or not data["access_token"] or
                not math.isfinite(issued) or not math.isfinite(expiry) or issued > now + 2 or
                expiry != expires_at(issued)):
            raise ValueError("Invalid session binding")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SessionUnavailable("SESSION_INVALID: invalid session identity or lifetime") from None
    if now >= expiry:
        raise SessionUnavailable("SESSION_EXPIRED: renew through the supported Kite login flow")
    return data


class SessionClient:
    """SDK proxy re-reads atomic session receipts before every call, including POSTs."""
    def __init__(self, factory, api_key, account, path, store, clock=time.time, environ=None, request_timeout=10):
        if not api_key or not account:
            raise ValueError("Set KITE_API_KEY and live_account_id before live operation")
        self.factory, self.api_key, self.account, self.path = factory, api_key, account, path
        self.store, self.clock, self.environ = store, clock, environ
        self.client, self.revision, self.last_verified = None, None, 0
        self.request_timeout = request_timeout

    def reconnect(self):
        """Discard a broken HTTP transport; preserve token revocation and identity checks."""
        if self.client is not None:
            session = getattr(self.client, "reqsession", None)
            if session is not None:
                session.close()
        self.client, self.last_verified = None, 0

    def _state(self, state, **extra):
        data = dict(state=state, account_id=self.account, checked_at=self.clock(), **extra)
        self.store.set("session", data)
        return data

    def _revoke(self):
        self.store.set("revoked_session", self.revision)
        self._state("REVOKED")

    def ensure(self):
        now = self.clock()
        try:
            receipt = read_session(self.path, self.api_key, self.account, now, self.environ)
        except SessionUnavailable as exc:
            self._state(str(exc).split(":")[0].removeprefix("SESSION_"))
            raise
        revision = key_digest(receipt["access_token"])
        if self.store.get("revoked_session") == revision:
            self._state("REVOKED")
            raise SessionUnavailable("SESSION_REVOKED: renew through the supported Kite login flow")
        changed = revision != self.revision
        if changed or self.client is None:
            self.client = self.factory(api_key=self.api_key, timeout=self.request_timeout)
            self.client.set_access_token(receipt["access_token"])
            self.revision, self.last_verified = revision, 0
        if changed or not self.last_verified or now - self.last_verified >= 60 or now < self.last_verified:
            try:
                profile = self.client.profile()
                if profile.get("user_id") != self.account:
                    self._revoke()
                    raise SessionUnavailable("SESSION_REVOKED: session belongs to a different account")
            except SessionUnavailable:
                raise
            except Exception as exc:
                self._failure(exc)
            self.last_verified = now
        self._state("VALID", issued_at=receipt["issued_at"], expires_at=receipt["expires_at"],
                    revision=revision, verified_at=self.last_verified)
        return self.client

    def _failure(self, exc):
        if any(cls.__name__ == "TokenException" for cls in type(exc).__mro__):
            self._revoke()
            raise SessionUnavailable("SESSION_REVOKED: broker rejected the session; renew login") from None
        self._state("UNAVAILABLE")
        # SDK errors can contain request URLs or credentials. Do not journal them.
        raise RuntimeError("Broker request failed (" + type(exc).__name__ + ")") from None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def call(*args, **kwargs):
            return self.request(name, *args, **kwargs)
        return call

    def request(self, name, *args, before_send=None, **kwargs):
        client = self.ensure()
        guard = self.__dict__.get('dispatch_guard')
        if guard and name in ('place_order', 'modify_order', 'cancel_order', 'place_gtt', 'modify_gtt', 'delete_gtt'):
            guard()
        if before_send is not None:
            before_send()  # after session renewal/validation, immediately before SDK dispatch
        if guard and name in ('place_order', 'modify_order', 'cancel_order', 'place_gtt', 'modify_gtt', 'delete_gtt'):
            guard()  # a durable dispatch callback can itself take time
        try:
            return getattr(client, name)(*args, **kwargs)
        except Exception as exc:
            self._failure(exc)
