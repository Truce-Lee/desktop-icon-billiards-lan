"""End-to-end loopback test: two LanPlay ends over a real TCP socket.

This is the closest thing to a two-machine test that runs headless: the host
steps a real table, streams snapshots, and the guest's render-only copy has to
land on the same positions.
"""
import time
import unittest

from dib import net
from dib.lanplay import LanPlay
from dib.table import BilliardsMotion


class StubOverlay:
    """The part of BilliardsOverlay that LanPlay touches."""

    def __init__(self, table):
        self.table = table
        self.trimmed = None

    def trim_balls(self, count):
        self.trimmed = self.table.set_ball_count(count)
        return self.trimmed

    def after_reset(self):
        pass


def normalised(table):
    left, top, right, bottom = table.bounds
    width, height = right - left, bottom - top
    return [((body.x - left) / width, (body.y - top) / height) for body in table.bodies]


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.server = net.Server(0).start()
        self.client = net.Client('127.0.0.1', self.server.port).start()
        self.host_table = BilliardsMotion((0, 0, 1600, 900), [(64, 64)] * 15)
        self.guest_table = BilliardsMotion((-1920, -120, 0, 780), [(64, 64)] * 30)
        self.host = LanPlay(StubOverlay(self.host_table), 'host', self.server, ('host', 'guest'))
        self.guest = LanPlay(StubOverlay(self.guest_table), 'guest', self.client, ('host', 'guest'))

    def tearDown(self):
        self.host.close()
        self.guest.close()
        self.server.close('test')
        self.client.close('test')

    def run_frames(self, frames, tick=None):
        """Pump both ends for ``frames`` frames of a fake 60 Hz clock."""
        for index in range(frames):
            now = index / 60
            self.host.pump(now)
            self.guest.pump(now)
            if self.host.started and tick is not None:
                tick(now)
                self.host_table.step(1 / 60, now)
                self.host.after_step(now)
            if self.guest.started:
                self.guest.render(now)
            time.sleep(.0005)

    def wait_started(self, frames=600):
        for index in range(frames):
            now = index / 60
            self.host.pump(now)
            self.guest.pump(now)
            if self.host.started and self.guest.started:
                return now
            time.sleep(.002)
        self.fail(f'handshake did not finish: host={self.host.status} guest={self.guest.status}')

    def test_handshake_uses_the_smaller_rack(self):
        now = self.wait_started()
        for index in range(40):
            self.host.pump(now + index / 60)
            self.host.after_step(now + index / 60)
            self.guest.pump(now + index / 60)
            self.guest.render(now + index / 60)
            time.sleep(.002)
        self.assertEqual(self.host.ball_count, 15)
        self.assertEqual(self.guest.ball_count, 15)
        self.assertEqual(len(self.guest_table.icons), 15)
        self.assertEqual(self.guest.remote.get('scores'), [0, 0])

    def test_guest_copy_converges_after_a_shot(self):
        now = self.wait_started()
        self.assertTrue(self.host.shoot((1.0, 0.05), .85, now))  # host opens
        settle = {'done': False}

        def tick(time_now):
            self.host.match.observe()
            if self.host_table.state in ('ready', 'ball_in_hand', 'finished'):
                settle['done'] = True

        self.run_frames(900, tick)
        self.assertTrue(settle['done'], 'the shot never settled')
        # One more frame carries the final snapshot across and renders it.
        for index in range(120):
            now = 15 + index / 60
            self.host.pump(now)
            self.host.after_step(now)
            self.guest.pump(now)
            self.guest.render(now)
            time.sleep(.002)
        worst = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                    for a, b in zip(normalised(self.host_table), normalised(self.guest_table)))
        self.assertLess(worst, .002, f'guest diverged by {worst:.4f} of the table')
        self.assertEqual(sorted(self.guest_table.sunk), sorted(self.host_table.sunk))

    def test_guest_shot_reaches_the_host(self):
        now = self.wait_started()
        self.host.match.turn = 1  # hand the table to the guest
        self.host._last_send = 0.0
        for index in range(60):
            stamp = now + index / 60
            self.host.pump(stamp)
            self.host.after_step(stamp)
            self.guest.pump(stamp)
            self.guest.render(stamp)
            time.sleep(.002)
        self.assertTrue(self.guest.can_shoot())
        self.assertTrue(self.guest.shoot((-1.0, 0.0), .5, now))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.host.match.shots[1] == 0:
            self.host.pump(time.monotonic())
            time.sleep(.002)
        self.assertEqual(self.host.match.shots[1], 1)
        self.assertEqual(self.host_table.state, 'stroke')

    def test_restart_from_the_guest(self):
        now = self.wait_started()
        self.host.match.scores = [3, 1]
        self.guest.request_restart()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.host.match.scores != [0, 0]:
            self.host.pump(time.monotonic())
            time.sleep(.002)
        self.assertEqual(self.host.match.scores, [0, 0])


    def test_host_aim_is_visible_to_the_guest(self):
        now = self.wait_started()
        self.host.match.turn = 0
        self.host_table.aim = (0.6, 0.8)
        self.host_table.charge = 0.5
        for index in range(80):
            stamp = now + index / 60
            self.host.pump(stamp)
            self.host.after_step(stamp)
            self.guest.pump(stamp)
            self.guest.render(stamp)
            time.sleep(.002)
        self.assertAlmostEqual(self.guest.table.aim[0], 0.6, places=3)
        self.assertAlmostEqual(self.guest.table.aim[1], 0.8, places=3)
        self.assertAlmostEqual(self.guest.table.charge, 0.5, places=2)

    def test_guest_aim_is_visible_on_the_host(self):
        now = self.wait_started()
        self.host.match.turn = 1
        for index in range(90):  # the guest has to learn that it is its turn first
            stamp = now + index / 60
            self.host.pump(stamp)
            self.host.after_step(stamp)
            self.guest.pump(stamp)
            self.guest.render(stamp)
            time.sleep(.002)
        self.assertTrue(self.guest.local_turn)

        self.guest.table.aim = (0.0, -1.0)
        self.guest.table.charge = 0.7
        for index in range(90):
            stamp = now + 3 + index / 60
            self.guest.pump(stamp)          # mirrors the local aim to the host
            self.host.pump(stamp)           # receives it
            self.host.after_step(stamp)     # shows it on the host table
            time.sleep(.002)
        self.assertAlmostEqual(self.host.remote_aim[0], 0.0, places=3)
        self.assertAlmostEqual(self.host.remote_aim[1], -1.0, places=3)
        self.assertAlmostEqual(self.host_table.aim[1], -1.0, places=3)
        self.assertAlmostEqual(self.host_table.charge, 0.7, places=2)

    def test_shooter_keeps_its_own_aim(self):
        now = self.wait_started()
        self.host.match.turn = 0
        self.host.remote_aim = (0.0, 1.0)
        self.host_table.aim = (1.0, 0.0)
        self.host.after_step(now + 1.0)
        self.assertEqual(self.host_table.aim, (1.0, 0.0))


if __name__ == '__main__':
    unittest.main()
