"""Headless LAN demo: a host and a guest play one shot over a real loopback socket.

    .\\.venv\\Scripts\\python.exe tools\\lan_demo.py

Nothing here opens a window, touches the Shell or moves a desktop icon: it is the
same path the tests use, printed so the protocol can be watched.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dib import net  # noqa: E402
from dib.lanplay import LanPlay  # noqa: E402
from dib.table import BilliardsMotion  # noqa: E402


class StubOverlay:
    """Just enough of BilliardsOverlay for LanPlay."""

    def __init__(self, table):
        self.table = table

    def trim_balls(self, count):
        return self.table.set_ball_count(count)

    def after_reset(self):
        pass


def positions(table):
    left, top, right, bottom = table.bounds
    width, height = right - left, bottom - top
    return [((body.x - left) / width, (body.y - top) / height) for body in table.bodies]


def main():
    server = net.Server(0).start()
    client = net.Client('127.0.0.1', server.port).start()
    print(f'host listening on {server.address} (loopback {server.port})')

    host_table = BilliardsMotion((0, 0, 1600, 900), [(64, 64)] * 20)
    guest_table = BilliardsMotion((-1920, -120, 0, 780), [(64, 64)] * 30)
    host = LanPlay(StubOverlay(host_table), 'host', server, ('主机', '客机'))
    guest = LanPlay(StubOverlay(guest_table), 'guest', client, ('主机', '客机'))

    now = 0.0
    for index in range(600):
        now = index / 60
        host.pump(now)
        guest.pump(now)
        if host.started and guest.started:
            break
        time.sleep(.002)
    print(f'handshake: {host.status} / {guest.status}')
    print(f'rack: host {len(host_table.icons)} balls, guest {len(guest_table.icons)} balls')

    print('host shoots at full power...')
    host.shoot((1.0, .04), .9, now)
    frames = 0
    while frames < 1200:
        frames += 1
        now = 10 + frames / 60
        host.pump(now)
        host_table.step(1 / 60, now)
        host.after_step(now)
        guest.pump(now)
        guest.render(now)
        if host_table.state in ('ready', 'ball_in_hand', 'finished'):
            break
        time.sleep(.0005)
    for index in range(90):  # let the final snapshots land
        now = 30 + index / 60
        host.pump(now)
        host.after_step(now)
        guest.pump(now)
        guest.render(now)
        time.sleep(.002)

    worst = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                for a, b in zip(positions(host_table), positions(guest_table)))
    print(f'settled after {frames} frames: state={host_table.state} '
          f'sunk={len(host_table.sunk)} score={host.match.scores}')
    print(f'guest render differs by {worst:.5f} of the table (positions, normalised)')

    host.close()
    guest.close()
    server.close('demo done')
    client.close('demo done')
    return 0 if worst < .002 else 1


if __name__ == '__main__':
    raise SystemExit(main())
