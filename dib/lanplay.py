"""Glue between one table, one socket and the turn rules.

The host owns a :class:`~dib.match.Match` and the only simulation; it streams
packed table states and streams match events along with them. The guest sends
shot intentions and renders whatever arrives, interpolating between the last two
frames so 30 Hz snapshots still look smooth at 60 fps.

All methods run on the animation thread, which is why nothing here blocks: the
endpoint only queues and drains.
"""
from __future__ import annotations

from . import net
from .match import Match

STATE_HZ = 30
IDLE_HZ = 2


class LanPlay:
    """One end of a LAN round."""

    def __init__(self, overlay, role, endpoint, names=None):
        self.overlay = overlay
        self.table = overlay.table
        self.role = role
        self.endpoint = endpoint
        self.names = list(names or (('主机', '对手') if role == 'host' else ('对手', '我方')))
        self.match = Match(self.table, self.names) if role == 'host' else None
        self.status = '等待对手连接…' if role == 'host' else '正在连接…'
        self.error = None
        self.connected = False
        self.started = False
        self.remote = {}
        self.ball_count = len(self.table.icons)
        self._seq = 0
        self._last_send = 0.0
        self._prev = None
        self._prev_at = 0.0
        self._curr = None
        self._curr_at = 0.0
        self._frame_at = 0.0

    # -- properties --------------------------------------------------------
    @property
    def is_host(self):
        return self.role == 'host'

    @property
    def alive(self):
        return self.error is None

    def scores(self):
        """Scores ordered as (mine, theirs) for the local player."""
        if self.is_host:
            return list(self.match.scores) if self.match else [0, 0]
        scores = self.remote.get('scores') or [0, 0]
        return [scores[1], scores[0]]

    @property
    def local_turn(self):
        if self.finished:
            return False
        if self.is_host:
            return bool(self.match) and self.match.turn == 0
        return self.remote.get('turn') == 1

    @property
    def finished(self):
        if self.is_host:
            return bool(self.match) and self.match.finished
        return bool(self.remote.get('finished'))

    @property
    def winner(self):
        """1 when the local player won, 0 when they lost, ``None`` for a draw."""
        if not self.finished:
            return None
        if self.is_host:
            if self.match.winner is None:
                return None
            return 1 if self.match.winner == 0 else 0
        winner = self.remote.get('winner')
        if winner is None:
            return None
        return 1 if winner == 1 else 0

    # -- frame pump --------------------------------------------------------
    def pump(self, now):
        """Drain network events and update the local view of the match."""
        for event in self.endpoint.poll():
            kind = event['t']
            if kind == 'open':
                self._opened()
            elif kind == 'msg':
                self._message(event['data'], now)
            elif kind == 'error':
                self._fail(event.get('error') or '网络错误')
            elif kind == 'close':
                self._closed()

    def after_step(self, now):
        """Host only: run the rules against the stepped table, then stream a frame."""
        if self.error is not None or not self.started:
            return
        self.match.observe()
        self._stream(now)

    def render(self, now):
        """Guest only: place the balls at their interpolated remote positions."""
        if self.error is not None or self._curr is None:
            return
        span = max(1 / 240, self._curr_at - self._prev_at)
        alpha = min(1.0, (now - self._frame_at) / span)
        net.apply_state(self.table, net.blend_state(self._prev, self._curr, alpha), keep_aim=True)

    def _opened(self):
        self.connected = True
        if self.is_host:
            self.status = '对手已连接，等待回应…'
            self.endpoint.send({
                't': net.WELCOME, 'v': net.PROTOCOL_VERSION,
                'balls': len(self.table.icons), 'names': self.names,
            })
        else:
            self.status = '已连接，等待开局…'
            self.endpoint.send({
                't': net.HELLO, 'v': net.PROTOCOL_VERSION,
                'balls': len(self.table.icons), 'names': self.names,
            })

    def _closed(self):
        self.connected = False
        if self.started:
            self._fail('对手已断开连接')
        else:
            self.status = '连接已关闭'

    def _fail(self, text):
        self.error = text
        self.status = text
        self.started = False

    def _message(self, message, now):
        kind = message.get('t')
        if kind == net.HELLO and self.is_host:
            self._greet(message)
        elif kind == net.WELCOME and not self.is_host:
            self._greet(message)
            self.started = True
            self.status = '对局开始'
        elif kind == net.SHOT and self.is_host:
            # Everything arriving from the socket was played by the guest, player 1.
            ok, reason = self.match.shoot(1, message.get('aim') or (1., 0.), message.get('power') or 0.,
                                          now)
            if not ok:
                self.status = reason
        elif kind == net.PLACE and self.is_host:
            ok, reason = self.match.place(1, message.get('x', .5), message.get('y', .5))
            if not ok:
                self.status = reason
        elif kind == net.STATE and not self.is_host:
            self._receive(message, now)
        elif kind == net.EVENT and self.is_host:
            if message.get('kind') == 'restart':
                self._restart()
        elif kind == net.BYE:
            self._closed()

    def _greet(self, message):
        """Fix the shared rack size as the smaller of the two desktops."""
        if int(message.get('v', 0)) != net.PROTOCOL_VERSION:
            self._fail('双方版本不一致，无法联机')
            return
        theirs = max(1, int(message.get('balls') or self.ball_count))
        self.ball_count = min(self.ball_count, theirs)
        if self.is_host:
            self.overlay.trim_balls(self.ball_count)
            self._restart()
            self.started = True
            self.status = '对局开始'
        else:
            self.overlay.trim_balls(self.ball_count)
            self.remote = {**self.remote, 'names': list(message.get('names') or self.names)}

    def _restart(self):
        self.match.restart()
        self.overlay.after_reset()
        self.status = '对局开始'
        self._last_send = 0.0  # stream immediately

    def _receive(self, payload, now):
        self._prev, self._prev_at = self._curr, self._curr_at
        self._curr, self._curr_at = payload, now
        self._frame_at = now
        if payload.get('match'):
            self.remote = payload['match']
            self.remote['names'] = self.remote.get('names') or self.names
        for event in payload.get('events') or ():
            if event.get('kind') == 'finished':
                self.status = '本局结束'

    def _stream(self, now):
        interval = 1 / STATE_HZ if self.table.state != 'ready' else 1 / IDLE_HZ
        if now - self._last_send < interval:
            return
        self._last_send = now
        self._seq += 1
        payload = net.pack_state(self.table, self._seq, self.match)
        payload['events'] = self.match.take_events()
        self.endpoint.send(payload)

    # -- local input -------------------------------------------------------
    def can_shoot(self):
        if not self.started or not self.connected or self.finished:
            return False
        if self.table.state != 'ready':
            return False
        return self.local_turn

    def can_place(self):
        if not self.started or not self.connected or self.finished:
            return False
        if self.table.state != 'ball_in_hand':
            return False
        return self.local_turn

    def shoot(self, aim, power, now):
        if not self.can_shoot():
            self.status = '还没轮到你' if not self.local_turn else '球还在滚动'
            return False
        if self.is_host:
            ok, reason = self.match.shoot(0, aim, power, now)
            if not ok:
                self.status = reason
            return ok
        self.endpoint.send({'t': net.SHOT, 'aim': [float(aim[0]), float(aim[1])], 'power': float(power)})
        return True

    def place(self, x, y):
        if not self.can_place():
            return False
        if self.is_host:
            ok, reason = self.match.place(0, x, y)
            if not ok:
                self.status = reason
            return ok
        self.endpoint.send({'t': net.PLACE, 'x': float(x), 'y': float(y)})
        return True

    def request_restart(self):
        if self.is_host:
            self._restart()
        elif self.started:
            self.endpoint.send({'t': net.EVENT, 'kind': 'restart'})

    def close(self):
        try:
            self.endpoint.send({'t': net.BYE})
        except Exception:
            pass
        self.endpoint.close('leaving')
