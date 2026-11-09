"""Private API launcher sharing the worker's account, state and lease receipt."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Phase2A_Execution'))
from execution.__main__ import main

mode = os.environ.get('TRADEX_MODE', 'paper')
if mode not in ('paper', 'live'):
    raise ValueError('TRADEX_MODE must be paper or live')
args = ['--config', os.environ.get('TRADEX_CONFIG', '/var/lib/tradex/config.json'),
        '--db', '/var/lib/tradex/engine.sqlite', '--paper-db', '/var/lib/tradex/paper.sqlite',
        '--session-file', '/var/lib/tradex/live.kite-session.json']
if mode == 'live':
    from azure.identity import ManagedIdentityCredential
    from azure.keyvault.secrets import SecretClient
    client = SecretClient(vault_url=os.environ['TRADEX_VAULT_URL'], credential=ManagedIdentityCredential())
    os.environ['KITE_API_KEY'] = client.get_secret('kite-api-key').value
    args.append('--live')
sys.exit(main(args + ['serve-api']))
