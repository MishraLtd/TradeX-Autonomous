"""Cloud worker fencing, consistent SQLite backups and offline restore.

Azure imports are optional. All credentials use managed identity/default Azure
credentials; no storage account keys or SAS tokens are placed in arguments.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from .acceptance import save_private


def dispatch_guard(path, account, timeout=5, now=None):
    now = time.time() if now is None else now
    data = json.loads(Path(path).read_text())
    if (data['account'] != account or not 0 <= now - data['issued_at'] <= 40 or
            not timeout + 2 < data['expires_at'] - now <= 60):
        raise RuntimeError('Cloud worker lease is not fresh; broker mutation blocked')


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Backup destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
            account = dst.execute("SELECT value FROM settings WHERE key='account'").fetchone()
            if not account:
                raise ValueError('Source has no bound account')
        with destination.open('rb') as handle:
            os.fsync(handle.fileno())
        metadata = dict(schema=1, created_at=time.time(), account=json.loads(account[0]),
                        sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
        save_private(str(destination) + '.json', metadata)
        return metadata
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore(source, destination, expected_account):
    source, destination = Path(source), Path(destination)
    metadata = json.loads(Path(str(source) + '.json').read_text())
    if metadata['account'] != expected_account or hashlib.sha256(source.read_bytes()).hexdigest() != metadata['sha256']:
        raise ValueError('Backup account or checksum mismatch')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        embedded = json.loads(db.execute("SELECT value FROM settings WHERE key='account'").fetchone()[0])
        if embedded != expected_account:
            raise ValueError('Embedded backup account mismatch')
    backup(source, destination)  # create-only, integrity-checked copy
    with closing(sqlite3.connect(destination)) as db, db:
        actual = json.loads(db.execute("SELECT value FROM settings WHERE key='account'").fetchone()[0])
        if actual != expected_account:
            raise ValueError('Embedded backup account mismatch')
        for key in ('preflight', 'heartbeat'):
            db.execute('DELETE FROM settings WHERE key=?', (key,))
        for key, value in dict(paused=True, polling=dict(recovery_required=True),
                               financial_reconciliation=dict(status='RESTORE_REQUIRES_RECONCILIATION')).items():
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
    restored_meta = dict(metadata, sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                         restored_from=metadata['sha256'], restored_at=time.time())
    save_private(str(destination) + '.json', restored_meta)
    return dict(status='RESTORED_PAUSED', account=expected_account)


def blob_client(url):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobClient
    if not url.startswith('https://') or '?' in url:
        raise ValueError('Use an HTTPS blob URL without SAS credentials')
    return BlobClient.from_blob_url(url, credential=DefaultAzureCredential(),
                                    connection_timeout=5, read_timeout=5, retry_total=0)


def run_fenced(url, receipt, account, command):
    client = blob_client(url)
    lease = client.acquire_lease(lease_duration=60)
    child = None
    receipt = Path(receipt)
    def stop(signum, frame):
        raise KeyboardInterrupt()
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        while True:
            now, started = time.time(), time.monotonic()
            lease.renew()  # ambiguity/failure stops the worker; never assume ownership
            if time.monotonic() - started > 10:
                raise RuntimeError('Cloud lease renewal took too long')
            save_private(receipt, dict(account=account, issued_at=now, expires_at=now + 35))
            if child is None:
                child = subprocess.Popen(command, start_new_session=True)
            try:
                return child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
    finally:
        receipt.unlink(missing_ok=True)
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        try:
            lease.release()
        finally:
            for s, handler in old.items():
                signal.signal(s, handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backup')
    b.add_argument('source'); b.add_argument('destination')
    r = sub.add_parser('restore')
    r.add_argument('source'); r.add_argument('destination'); r.add_argument('--account', required=True)
    u = sub.add_parser('upload')
    u.add_argument('source'); u.add_argument('blob_url')
    w = sub.add_parser('worker')
    w.add_argument('--lease-url', required=True); w.add_argument('--receipt', required=True)
    w.add_argument('--account', required=True); w.add_argument('engine_command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == 'backup':
        print(json.dumps(backup(args.source, args.destination)))
    elif args.command == 'restore':
        print(json.dumps(restore(args.source, args.destination, args.account)))
    elif args.command == 'upload':
        with open(args.source, 'rb') as handle:
            blob_client(args.blob_url).upload_blob(handle, overwrite=False)
    else:
        command = args.engine_command
        if command and command[0] == '--':
            command = command[1:]
        if not command:
            parser.error('worker needs an engine command after --')
        return run_fenced(args.lease_url, args.receipt, args.account, command)
    return 0


if __name__ == '__main__':
    sys.exit(main())
