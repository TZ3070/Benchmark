#!/usr/bin/env python3
"""Run on A. Read A's clock and submit a correction to B over SSH."""
import argparse
import json
import math
import subprocess
import time


def exchange(proc, message):
    proc.stdin.write(json.dumps(message) + '\n')
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError('B closed the SSH connection')
    reply = json.loads(line)
    if 'error' in reply:
        raise RuntimeError(reply['error'])
    return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH alias or user@B')
    parser.add_argument('--remote-command', required=True, help='Fixed B helper command')
    parser.add_argument('--apply', action='store_true', help='Submit sample to B chrony')
    args = parser.parse_args()
    # No clock-changing command is executed locally on A.
    with subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes', '-o',
                           'ConnectTimeout=10', '-o', 'ServerAliveInterval=5',
                           '-o', 'ServerAliveCountMax=2', args.host,
                           args.remote_command], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, bufsize=1) as proc:
        try:
            samples = []
            for _ in range(3):
                m1, t1 = time.monotonic(), time.time()
                reply = exchange(proc, {'action': 'probe'})
                t4, m4 = time.time(), time.monotonic()
                if abs((t4 - t1) - (m4 - m1)) > 0.1:
                    raise RuntimeError('A clock changed during measurement')
                t2, t3 = reply['received_at'], reply['sent_at']
                rtt = (m4 - m1) - (t3 - t2)
                offset = ((t2 - t1) + (t3 - t4)) / 2
                if not all(math.isfinite(v) for v in (rtt, offset)) or rtt < -0.01:
                    raise RuntimeError('Invalid timing sample')
                samples.append({'id': reply['id'], 'offset': offset,
                                'rtt': max(0, rtt)})
            sample = min(samples, key=lambda item: item['rtt'])
            if sample['rtt'] > 1:
                raise RuntimeError('Network round-trip exceeds one second; refusing correction')
            result = exchange(proc, {'action': 'adjust', **sample,
                                     'apply': args.apply})
            print(json.dumps({'sample': sample, 'result': result}), flush=True)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    main()
