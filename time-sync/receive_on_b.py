#!/usr/bin/env python3
"""Run only on B through a restricted SSH command. Feed A time to local chrony."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
import subprocess
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chronyc', required=True, help='Absolute chronyc executable')
    parser.add_argument('--socket', required=True, help='Local chronyd Unix socket')
    parser.add_argument('--max-offset', type=float, default=300,
                        help='Refuse larger corrections, in seconds')
    args = parser.parse_args()
    samples = {}
    for line in sys.stdin:
        received, mono = time.time(), time.monotonic()
        try:
            message = json.loads(line)
            if message['action'] == 'probe':
                if len(samples) >= 10:
                    raise ValueError('Too many probes')
                identifier = uuid.uuid4().hex
                samples[identifier] = (received, mono)
                reply = {'id': identifier, 'received_at': received,
                         'sent_at': time.time()}
            elif message['action'] == 'adjust':
                original, original_mono = samples.pop(message['id'])
                age = mono - original_mono
                offset, rtt = float(message['offset']), float(message['rtt'])
                if age > 30 or age < 0:
                    raise ValueError('Sample expired')
                if abs((received - original) - age) > 0.1:
                    raise ValueError('B clock changed during measurement')
                if not math.isfinite(offset) or abs(offset) > args.max_offset:
                    raise ValueError('Offset exceeds configured limit')
                if not math.isfinite(rtt) or not 0 <= rtt <= 1:
                    raise ValueError('Network delay exceeds limit')
                # Account for elapsed time since probe. Positive offset means B is ahead.
                target = time.time() - offset
                reference = datetime.fromtimestamp(round(target), timezone.utc)
                command = [args.chronyc, '-h', args.socket, 'settime',
                           reference.strftime('%b %d, %Y %H:%M:%S UTC')]
                applied = message.get('apply') is True
                if applied:
                    output = subprocess.run(command, capture_output=True, text=True,
                                            timeout=10, check=True,
                                            env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'})
                    reply = {'submitted_to_chrony': True, 'output': output.stdout.strip()}
                else:
                    reply = {'submitted_to_chrony': False, 'reference_utc': reference.isoformat()}
                print(json.dumps(reply), flush=True)
                return
            else:
                raise ValueError('Unknown action')
            print(json.dumps(reply), flush=True)
        except Exception as exc:
            print(json.dumps({'error': str(exc)}), flush=True)
            return


if __name__ == '__main__':
    main()
