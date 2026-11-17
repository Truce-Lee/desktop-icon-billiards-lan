"""Turn rules for a two-player LAN round.

The host owns the only :class:`~dib.table.BilliardsMotion` and one :class:`Match`;
the guest renders snapshots. Everything here is plain Python, so the rules can be
unit-tested without a desktop, a window or a socket.

Rules (deliberately simpler than eight-ball)
--------------------------------------------
* Every potted desktop icon scores one point for the player who shot.
* Potting an icon keeps the turn; missing the table entirely or losing the cue
  ball hands the turn to the opponent.
* The round ends when the table is cleared; the higher score wins, a tie is a draw.
"""
from __future__ import annotations

FOUL_WHITE = '白球入袋'
FOUL_NO_CONTACT = '空杆'

# The table settles into one of these states once a shot has stopped moving.
SETTLED = ('ready', 'return_white', 'ball_in_hand', 'finished')


class Match:
    """Score, turn and foul bookkeeping around one shared table."""

    def __init__(self, table, names=('主机', '对手')):
        self.table = table
        self.names = list(names)
        self.scores = [0, 0]
        self.shots = [0, 0]
        self.turn = 0
        self.finished = False
        self.winner = None
        self.foul = None
        self.events = []
        self._seen = set(table.sunk)
        self._armed = False
        self._white_potted = False
        self._pots = 0

    # -- lifecycle ---------------------------------------------------------
    def restart(self):
        self.table.reset()
        self.scores = [0, 0]
        self.shots = [0, 0]
        self.turn = 0
        self.finished = False
        self.winner = None
        self.foul = None
        self._seen = set()
        self._armed = False
        self._white_potted = False
        self._pots = 0
        self._emit(kind='restart')
        return self

    def _emit(self, **event):
        self.events.append(event)
        return event

    def take_events(self):
        """Drain the events produced since the last call (the host forwards them)."""
        events, self.events = self.events, []
        return events

    # -- player input ------------------------------------------------------
    def can_shoot(self, player):
        if self.finished:
            return False, '本局已结束'
        if player != self.turn:
            return False, '还没轮到你'
        if self.table.state == 'ball_in_hand':
            return False, '请先摆放白球'
        if self.table.state != 'ready':
            return False, '球还在滚动'
        return True, ''

    def shoot(self, player, aim, power, now=None):
        allowed, reason = self.can_shoot(player)
        if not allowed:
            return False, reason
        x, y = float(aim[0]), float(aim[1])
        length = (x * x + y * y) ** .5
        if length < 1e-6:
            return False, '瞄准方向无效'
        self.table.aim = (x / length, y / length)
        if not self.table.shoot(max(.0, min(1., float(power))), now):
            return False, '无法击球'
        self.shots[player] += 1
        self._armed = True
        self._white_potted = False
        self._pots = 0
        self._seen = set(self.table.sunk)
        return True, ''

    def can_place(self, player):
        if self.finished:
            return False, '本局已结束'
        if player != self.turn:
            return False, '还没轮到你'
        if self.table.state != 'ball_in_hand':
            return False, '现在不需要摆球'
        return True, ''

    def place(self, player, x, y):
        """Place the cue ball from normalised table coordinates (0..1)."""
        allowed, reason = self.can_place(player)
        if not allowed:
            return False, reason
        left, top, right, bottom = self.table.bounds
        px = left + max(0., min(1., float(x))) * (right - left)
        py = top + max(0., min(1., float(y))) * (bottom - top)
        if not self.table.place_white(px, py):
            return False, '这个位置放不下白球'
        return True, ''

    # -- per-frame bookkeeping ---------------------------------------------
    def observe(self):
        """Advance the rules; call once per frame right after ``table.step``.

        Returns the events produced this frame so the host can forward them.
        """
        fresh = self.table.sunk - self._seen
        if fresh:
            self._seen |= fresh
            potted = len(fresh) - (1 if 0 in fresh else 0)
            if 0 in fresh:
                self._white_potted = True
            if potted:
                self._pots += potted
                self.scores[self.turn] += potted
                self._emit(kind='potted', count=potted, by=self.turn, scores=list(self.scores))
        if not self._armed or self.table.state not in SETTLED:
            return self.take_events()

        self._armed = False
        foul = None
        if self._white_potted:
            foul = FOUL_WHITE
        elif self.table.last_hit_index is None:
            foul = FOUL_NO_CONTACT
        self.foul = foul

        if self.table.state == 'finished':
            self.finished = True
            if self.scores[0] == self.scores[1]:
                self.winner = None
            else:
                self.winner = 0 if self.scores[0] > self.scores[1] else 1
            self._emit(kind='finished', winner=self.winner, scores=list(self.scores))
        elif foul or not self._pots:
            self.turn = 1 - self.turn
            self._emit(kind='turn', turn=self.turn, foul=foul, scores=list(self.scores))
        if foul:
            self._emit(kind='foul', reason=foul, by=1 - self.turn)
        return self.take_events()

    # -- wire format -------------------------------------------------------
    def summary(self):
        return {
            'names': list(self.names),
            'scores': list(self.scores),
            'shots': list(self.shots),
            'turn': self.turn,
            'foul': self.foul,
            'finished': self.finished,
            'winner': self.winner,
        }
