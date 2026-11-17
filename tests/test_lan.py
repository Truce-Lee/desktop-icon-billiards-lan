"""Tests for the LAN layer: framing, transport, state transfer and match rules.

Nothing here opens a window, touches the Shell or reads a desktop, so the whole
multiplayer core is testable headless.
"""
import socket
import time
import unittest

from dib import net
from dib.match import FOUL_NO_CONTACT, FOUL_WHITE, Match
from dib.table import BilliardsMotion


def wait_for(predicate, timeout=3.0):
    """Spin until ``predicate()`` is true; return its value or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('timed out waiting for condition')


def drain_messages(endpoint, timeout=3.0):
    """Collect every ``msg`` event from an endpoint until one arrives."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        for event in endpoint.poll():
            if event['t'] == 'msg':
                return event['data']
            seen.append(event)
        time.sleep(.01)
    raise AssertionError(f'no message received, events={seen}')


def wait_events(endpoint, kinds, timeout=3.0):
    """Collect events until every kind in ``kinds`` has been seen."""
    deadline = time.monotonic() + timeout
    found = []
    while time.monotonic() < deadline:
        found.extend(endpoint.poll())
        if all(any(event['t'] == kind for event in found) for kind in kinds):
            return found
        time.sleep(.01)
    raise AssertionError(f'events {kinds} not all seen, got {found}')


def make_table(count=15, bounds=(0, 0, 1600, 1000)):
    return BilliardsMotion(bounds, [(64, 64)] * count)


class FramingTests(unittest.TestCase):
    def test_encode_round_trip(self):
        frame = net.encode({'t': 'shot', 'aim': [1.0, 0.5], 'power': .75, '名字': '主机'})
        length = int.from_bytes(frame[:4], 'big')
        self.assertEqual(length, len(frame) - 4)
        self.assertLessEqual(length, net.MAX_MESSAGE)

    def test_oversized_message_is_refused(self):
        with self.assertRaises(ValueError):
            net.encode({'t': 'state', 'bodies': [[0] * 8] * 40000})

    def test_parse_address_forms(self):
        self.assertEqual(net.parse_address('192.168.1.5'), ('192.168.1.5', net.DEFAULT_PORT))
        self.assertEqual(net.parse_address('192.168.1.5:5000'), ('192.168.1.5', 5000))
        self.assertEqual(net.parse_address('  pc.local:1234 '), ('pc.local', 1234))
        for bad in ('', 'host:abc', 'host:70000'):
            with self.assertRaises(ValueError):
                net.parse_address(bad)


class LinkTests(unittest.TestCase):
    def test_socketpair_round_trip(self):
        left, right = socket.socketpair()
        first = net.Link(left).start()
        second = net.Link(right).start()
        try:
            self.assertTrue(second.send({'t': 'hello', 'v': 1}))
            payload = drain_messages(first)
            self.assertEqual(payload['v'], 1)
            first.send({'t': 'shot', 'aim': [0.0, 1.0], 'power': .4})
            self.assertEqual(drain_messages(second)['t'], 'shot')
        finally:
            first.close('test')
            second.close('test')
            wait_for(lambda: not first.connected and not second.connected)

    def test_close_flushes_pending_messages(self):
        left, right = socket.socketpair()
        first = net.Link(left).start()
        second = net.Link(right).start()
        second.send({'t': 'event', 'kind': 'turn'})
        second.close('bye')
        events = wait_events(first, ('msg', 'close'))
        self.assertEqual([event['data']['kind'] for event in events if event['t'] == 'msg'], ['turn'])
        self.assertEqual(sum(1 for event in events if event['t'] == 'close'), 1)
        first.close('test')

    def test_bad_frame_length_is_reported(self):
        left, right = socket.socketpair()
        link = net.Link(left).start()
        right.sendall(b'\xff\xff\xff\xff')
        error = wait_for(lambda: [event for event in link.poll() if event['t'] == 'error'] or None)
        self.assertIn('frame', error[0]['error'])
        link.close('test')
        right.close()

    def test_server_accepts_one_guest(self):
        server = net.Server(0).start()
        try:
            client = net.Client('127.0.0.1', server.port).start()
            wait_for(lambda: server.connected and client.connected)
            client.send({'t': 'hello', 'v': net.PROTOCOL_VERSION})
            payload = drain_messages(server)
            self.assertEqual(payload['v'], net.PROTOCOL_VERSION)
            server.send({'t': 'welcome', 'v': net.PROTOCOL_VERSION, 'balls': 15})
            self.assertEqual(drain_messages(client)['balls'], 15)
        finally:
            server.close('test')
            client.close('test')

    def test_client_reports_unreachable_host(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()  # the port is now free, so the connection is refused
        client = net.Client('127.0.0.1', port, timeout=1.0).start()
        error = wait_for(lambda: [event for event in client.poll() if event['t'] == 'error'] or None)
        self.assertIn('连接失败', error[0]['error'])
        client.close('test')


class StateTransferTests(unittest.TestCase):
    def test_positions_survive_a_different_table_size(self):
        host = make_table(15, (0, 0, 1600, 900))
        host.aim = (1.0, .1)
        host.shoot(.8, 0.)
        for frame in range(40):
            host.step(1 / 60, frame / 60)
        packed = net.pack_state(host, seq=3, match=Match(host))

        guest = make_table(15, (-1920, -120, 0, 780))  # second monitor, different size
        net.apply_state(guest, packed)

        left, top, right, bottom = host.bounds
        for host_body, guest_body in zip(host.bodies, guest.bodies):
            self.assertAlmostEqual((host_body.x - left) / (right - left),
                                   (guest_body.x - guest.bounds[0]) / (guest.bounds[2] - guest.bounds[0]),
                                   places=4)
            self.assertAlmostEqual((host_body.y - top) / (bottom - top),
                                   (guest_body.y - guest.bounds[1]) / (guest.bounds[3] - guest.bounds[1]),
                                   places=4)
        self.assertEqual(guest.state, host.state)
        self.assertEqual(guest.sunk, host.sunk)

    def test_apply_state_resets_an_older_round(self):
        table = make_table(15)
        table.sunk.add(3)
        table.state = 'rolling'
        packed = net.pack_state(make_table(15), seq=0)
        packed['round'] = table.round_serial + 5
        net.apply_state(table, packed)
        self.assertEqual(table.round_serial, packed['round'])
        self.assertEqual(table.sunk, set())

    def test_blend_state_interpolates_positions(self):
        table = make_table(3)
        before = net.pack_state(table, seq=1)
        table.white.x += 100
        after = net.pack_state(table, seq=2)
        middle = net.blend_state(before, after, .5)
        start = before['bodies'][0][0]
        end = after['bodies'][0][0]
        self.assertAlmostEqual(middle['bodies'][0][0], (start + end) / 2, places=6)
        self.assertEqual(middle['n'], 2)

    def test_blend_state_ignores_a_new_round(self):
        table = make_table(3)
        before = net.pack_state(table, seq=1)
        table.reset()
        after = net.pack_state(table, seq=2)
        self.assertIs(net.blend_state(before, after, .5), after)


class FirstContactTests(unittest.TestCase):
    def test_straight_shot_records_the_first_icon(self):
        table = make_table(5)
        table.aim = (1.0, 0.0)
        table.shoot(.7, 0.)
        for frame in range(1, 600):
            if table.state not in ('stroke', 'rolling'):
                break
            table.step(1 / 60, frame / 60)
        self.assertIsNotNone(table.last_hit_index)
        self.assertGreaterEqual(table.last_hit_index, 1)

    def test_next_shot_forgets_the_previous_contact(self):
        table = make_table(5)
        table.last_hit_index = 4
        table.aim = (0.0, 1.0)
        table.shoot(.5, 0.)
        self.assertIsNone(table.last_hit_index)


class MatchRuleTests(unittest.TestCase):
    def setUp(self):
        self.table = make_table(15)
        self.match = Match(self.table)

    def pot(self, *indexes):
        self.table.sunk.update(indexes)

    def settle(self):
        self.table.state = 'ready'

    def test_potting_scores_and_keeps_the_turn(self):
        self.assertTrue(self.match.shoot(0, (1.0, 0.0), .8)[0])
        self.table.last_hit_index = 1
        self.pot(1, 2, 3)
        self.match.observe()
        self.assertEqual(self.match.scores, [3, 0])
        self.settle()
        self.match.observe()
        self.assertEqual(self.match.turn, 0)

    def test_missing_everything_passes_the_turn(self):
        self.assertTrue(self.match.shoot(0, (1.0, 0.0), .8)[0])
        self.table.last_hit_index = None
        self.settle()
        self.match.observe()
        self.assertEqual(self.match.turn, 1)
        self.assertEqual(self.match.foul, FOUL_NO_CONTACT)

    def test_scratch_passes_the_turn_and_keeps_the_points(self):
        self.assertTrue(self.match.shoot(0, (1.0, 0.0), .8)[0])
        self.pot(0, 5)
        self.table.white_sunk = True
        self.settle()
        self.match.observe()
        self.assertEqual(self.match.scores, [1, 0])
        self.assertEqual(self.match.turn, 1)
        self.assertEqual(self.match.foul, FOUL_WHITE)

    def test_cannot_shoot_out_of_turn_or_while_rolling(self):
        self.match.turn = 1
        self.assertFalse(self.match.shoot(0, (1.0, 0.0), .5)[0])
        self.match.turn = 0
        self.table.state = 'rolling'
        self.assertFalse(self.match.shoot(0, (1.0, 0.0), .5)[0])
        self.table.state = 'ball_in_hand'
        self.assertFalse(self.match.shoot(0, (1.0, 0.0), .5)[0])
        self.assertTrue(self.match.can_place(0)[0])

    def test_clear_table_finishes_with_a_winner(self):
        self.assertTrue(self.match.shoot(0, (1.0, 0.0), .8)[0])
        self.match.scores = [4, 2]
        self.pot(*range(1, 16))
        self.table.state = 'finished'
        self.match.observe()
        self.assertTrue(self.match.finished)
        self.assertEqual(self.match.winner, 0)
        self.assertEqual(self.match.summary()['scores'], [4 + 15, 2])

    def test_draw_when_scores_are_equal(self):
        # The rack is already cleared before the shot, so the shot scores nothing and
        # the match simply ends on equal scores.
        self.table.sunk = set(range(1, 16))
        self.assertTrue(self.match.shoot(0, (1.0, 0.0), .8)[0])
        self.match.scores = [3, 3]
        self.table.state = 'finished'
        self.match.observe()
        self.assertTrue(self.match.finished)
        self.assertIsNone(self.match.winner)

    def test_restart_clears_scores(self):
        self.match.scores = [5, 1]
        self.match.finished = True
        self.match.restart()
        self.assertEqual(self.match.scores, [0, 0])
        self.assertFalse(self.match.finished)
        self.assertEqual(self.table.state, 'ready')
        self.assertIn('restart', [event['kind'] for event in self.match.take_events()])

    def test_place_accepts_normalised_coordinates(self):
        self.table.state = 'ball_in_hand'
        self.table.white_sunk = True
        self.table.sunk = {0}
        ok, reason = self.match.place(0, .3, .5)
        self.assertTrue(ok, reason)
        left, top, right, bottom = self.table.bounds
        self.assertAlmostEqual(self.table.white.x, left + .3 * (right - left), places=6)
        self.assertEqual(self.table.state, 'ready')
        self.assertNotIn(0, self.table.sunk)


class BallCountTests(unittest.TestCase):
    def test_trimming_keeps_the_cue_ball_first(self):
        table = make_table(30)
        self.assertEqual(table.set_ball_count(20), 20)
        self.assertEqual(len(table.bodies), 21)
        self.assertIs(table.bodies[0], table.white)
        self.assertEqual(len(table.poses), 20)
        self.assertEqual(table.set_ball_count(99), 20)  # cannot grow again


if __name__ == '__main__':
    unittest.main()
