"""systemd entry point. Paper is default; live needs explicit configuration."""
import os
from pathlib import Path
import sys
from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Phase2A_Execution'))
from execution.operations import run_fenced

mode = os.environ.get('TRADEX_MODE', 'paper')
if mode == 'upstox-paper':
    # Exclusive local paper trial; never reaches the live/Kite branch below.
    os.environ['TRADEX_TRIAL_LEASE_RECEIPT'] = '/run/tradex/lease.json'
    os.environ['TRADEX_TRIAL_RELEASE'] = Path(__file__).resolve().parents[1].name
    command = [sys.executable, '-m', 'execution.paper_trial',
               '--config', os.environ.get('TRADEX_TRIAL_CONFIG', '/var/lib/tradex/upstox-trial/config.json'),
               '--db', os.environ.get('TRADEX_TRIAL_DB', '/var/lib/tradex/upstox-trial/trial.sqlite'), 'run']
    sys.exit(run_fenced(os.environ['TRADEX_LEASE_URL'], '/run/tradex/lease.json',
                        os.environ['TRADEX_ACCOUNT_ID'], command))
if mode not in ('paper', 'live'):
    raise ValueError('TRADEX_MODE must be paper or live')
config = os.environ.get('TRADEX_CONFIG', '/var/lib/tradex/config.json')
command = [sys.executable, '-m', 'execution', '--config', config, '--db', '/var/lib/tradex/engine.sqlite',
           '--paper-db', '/var/lib/tradex/paper.sqlite', '--session-file', '/var/lib/tradex/live.kite-session.json']
if mode == 'live':
    client = SecretClient(vault_url=os.environ['TRADEX_VAULT_URL'], credential=ManagedIdentityCredential())
    os.environ['KITE_API_KEY'] = client.get_secret('kite-api-key').value
    command.append('--live')
command.append('run')
sys.exit(run_fenced(os.environ['TRADEX_LEASE_URL'], '/run/tradex/lease.json', os.environ['TRADEX_ACCOUNT_ID'], command))
