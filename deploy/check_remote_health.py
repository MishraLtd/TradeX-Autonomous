"""Run outside the VM. Failure is surfaced through GitHub workflow notifications."""
import json
import sys
import time

def check(data, now, max_age=180):
    stamp = data['checked_at']
    if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= max_age:
        raise ValueError('Cloud health publication is stale or future-dated')
    if data['status'] != 'HEALTHY':
        raise ValueError('Cloud worker reports: ' + ', '.join(data['codes']))

if __name__ == '__main__':
    with open(sys.argv[1]) as handle:
        check(json.load(handle), time.time())
