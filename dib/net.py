"""LAN transport for Desktop Icon Billiards.

One TCP connection carries length-prefixed JSON between a host and a guest on
the same network. The host owns the table; the guest sends shot intentions and
renders the snapshots the host streams back.

Why the host is authoritative
-----------------------------
``BilliardsMotion.step`` integrates with whatever ``dt`` the frame loop reports,
and the sub-step count depends on that ``dt``: the same shot taken at 60 Hz and
at 144 Hz ends hundreds of pixels apart (measured: 438 px on a 1600x1000 table).
Two machines therefore cannot both simulate the shot and stay together, so only
the host runs the physics and streams positions.

Threading
---------
Every socket lives on its own reader and writer thread. The animation thread
only calls :meth:`Link.send` (never blocks; a full outbox drops the message) and
:meth:`Link.poll` (drains a queue), so a slow peer can never stall a frame.
"""
from __future__ import annotations

import ctypes
import json
import queue
import socket
import struct
import threading

PROTOCOL_VERSION = 1
DEFAULT_PORT = 47810
MAX_MESSAGE = 256 * 1024
CONNECT_TIMEOUT = 6.0

# Message types, kept in one place so the two ends cannot drift.
HELLO = 'hello'
WELCOME = 'welcome'
SHOT = 'shot'
PLACE = 'place'
AIM = 'aim'
STATE = 'state'
EVENT = 'event'
ERROR = 'error'
BYE = 'bye'


class ProtocolError(RuntimeError):
    """The peer sent a frame this build cannot read."""


def encode(message):
    """Serialise a message into one length-prefixed frame."""
    body = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_MESSAGE:
        raise ValueError('message too large')
    return struct.pack('>I', len(body)) + body


def _read_exact(sock, count):
    """Read exactly ``count`` bytes, or return ``None`` when the peer closed."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def local_ipv4():
    """The address a peer on the same network should connect to."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # A UDP connect sends no packets; it only lets Windows pick the outbound interface.
        probe.connect(('10.255.255.255', 1))
        return probe.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        probe.close()


class _IpAddressString(ctypes.Structure):
    pass


_IpAddressString._fields_ = [
    ('Next', ctypes.POINTER(_IpAddressString)),
    ('IpAddress', ctypes.c_char * 16),
    ('IpMask', ctypes.c_char * 16),
    ('Context', ctypes.c_ulong),
]


class _IpAdapterInfo(ctypes.Structure):
    pass


_IpAdapterInfo._fields_ = [
    ('Next', ctypes.POINTER(_IpAdapterInfo)),
    ('ComboIndex', ctypes.c_ulong),
    ('AdapterName', ctypes.c_char * 260),
    ('Description', ctypes.c_char * 132),
    ('AddressLength', ctypes.c_ulong),
    ('Address', ctypes.c_ubyte * 8),
    ('Index', ctypes.c_ulong),
    ('Type', ctypes.c_ulong),
    ('DhcpEnabled', ctypes.c_ulong),
    ('CurrentIpAddress', ctypes.POINTER(_IpAddressString)),
    ('IpAddressList', _IpAddressString),
    ('GatewayList', _IpAddressString),
    ('DhcpServer', _IpAddressString),
    ('HaveWins', ctypes.c_int),
    ('PrimaryWinsServer', _IpAddressString),
    ('SecondaryWinsServer', _IpAddressString),
    ('LeaseObtained', ctypes.c_ulong),
    ('LeaseExpires', ctypes.c_ulong),
]


def local_ipv4_all():
    """Every IPv4 address this machine can be reached on, as ``[(ip, adapter name)]``.

    Windows machines usually have several: Wi-Fi, Ethernet, a VPN, Hyper-V/virtual
    adapters. The host picks the one both players can reach; this list is what the
    dialog offers.
    """
    try:
        iphlp = ctypes.WinDLL('iphlpapi', use_last_error=True)
        size = ctypes.c_ulong(0)
        iphlp.GetAdaptersInfo(None, ctypes.byref(size))
        if not size.value:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if iphlp.GetAdaptersInfo(buffer, ctypes.byref(size)) != 0:
            return []
    except OSError:
        return []

    rows, seen = [], set()
    adapter = ctypes.cast(buffer, ctypes.POINTER(_IpAdapterInfo))
    while adapter:
        info = adapter.contents
        name = info.Description.decode('mbcs', 'replace').strip() or '网卡'
        entry = ctypes.pointer(info.IpAddressList)
        while entry:
            address = entry.contents.IpAddress.decode('ascii', 'replace').strip()
            if address and address not in seen and not address.startswith(('0.', '127.', '169.254.')):
                seen.add(address)
                rows.append((address, name))
            entry = entry.contents.Next
        adapter = info.Next
    # The interface Windows routes the internet through is the best default; keep the
    # rest in adapter order so a VPN or a second NIC stays one click away.
    primary = local_ipv4()
    rows.sort(key=lambda row: 0 if row[0] == primary else 1)
    return rows


def parse_address(text, default_port=DEFAULT_PORT):
    """Accept ``ip``, ``ip:port``, ``host:port`` and return ``(host, port)``."""
    value = (text or '').strip()
    if not value:
        raise ValueError('请填写房主的 IP 地址')
    if value.count(':') == 1 and not value.startswith('['):
        host, _, port = value.partition(':')
        if not port.isdigit():
            raise ValueError('端口不是数字')
        port = int(port)
    else:
        host, port = value, default_port
    if not host:
        raise ValueError('请填写房主的 IP 地址')
    if not 0 < port < 65536:
        raise ValueError('端口超出范围')
    return host, port


def format_address(host, port):
    return f'{host}:{port}'


class Link:
    """An open connection: a send queue, a reader thread and an event queue.

    ``poll()`` yields dicts with ``t`` in ``open`` / ``msg`` / ``close`` /
    ``error``. ``close`` is reported once, after every message the peer managed
    to send has already been delivered.
    """

    def __init__(self, sock, peer=None):
        self.socket = sock
        self.peer = peer
        self.events = queue.Queue()
        self._outbox = queue.Queue(maxsize=512)
        self._closing = threading.Event()
        self._closed = threading.Event()
        self._reason = ''
        self.dropped = 0
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._reader = threading.Thread(target=self._read_loop, name='dib-lan-read', daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name='dib-lan-write', daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.events.put({'t': 'open', 'peer': self.peer})
        self._reader.start()
        self._writer.start()
        return self

    @property
    def connected(self):
        return not (self._closed.is_set() or self._closing.is_set())

    def send(self, message):
        """Queue one message. Returns ``False`` when the outbox is full (message dropped)."""
        if not self.connected:
            return False
        try:
            self._outbox.put_nowait(message)
        except queue.Full:
            self.dropped += 1
            return False
        return True

    def close(self, reason='closed'):
        """Stop both threads. Safe to call more than once and from any thread."""
        if self._closing.is_set():
            return
        self._closing.set()
        self._reason = reason
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        # Let the writer flush what is already queued, so a final event (the last
        # turn, the result) still reaches the peer before the socket goes away.
        if self._writer.is_alive() and threading.current_thread() is not self._writer:
            self._writer.join(0.4)
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def poll(self):
        """Return every event seen since the last call; never blocks."""
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                return items

    # -- threads -----------------------------------------------------------
    def _read_loop(self):
        try:
            while not self._closing.is_set():
                header = _read_exact(self.socket, 4)
                if header is None:
                    break
                (length,) = struct.unpack('>I', header)
                if length == 0 or length > MAX_MESSAGE:
                    raise ProtocolError(f'bad frame length {length}')
                body = _read_exact(self.socket, length)
                if body is None:
                    break
                try:
                    message = json.loads(body.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ProtocolError('bad json') from error
                if not isinstance(message, dict):
                    raise ProtocolError('frame is not an object')
                self.events.put({'t': 'msg', 'data': message})
        except (OSError, ProtocolError) as error:
            self.events.put({'t': 'error', 'error': str(error)})
        finally:
            self._finish()

    def _write_loop(self):
        try:
            while True:
                message = self._outbox.get()
                if message is None:
                    return
                self.socket.sendall(encode(message))
        except OSError:
            pass
        finally:
            self._closing.set()
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _finish(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._closing.set()
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.socket.close()
        except OSError:
            pass
        self.events.put({'t': 'close', 'reason': self._reason or 'peer closed'})


class Server:
    """Listen on a port and accept exactly one guest.

    ``poll()`` first reports ``listening``, then the :class:`Link` events once a
    guest arrives. ``port`` holds the real port, which matters when 0 was asked
    for (tests use that to get a free port).
    """

    def __init__(self, port=DEFAULT_PORT, host=''):
        self.requested_port = port
        self.host = host
        self.socket = None
        self.port = 0
        self.link = None
        self.events = queue.Queue()
        self._closing = threading.Event()
        self._thread = None

    def start(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.requested_port))
        self.socket.listen(1)
        self.socket.settimeout(0.4)
        self.port = self.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name='dib-lan-accept', daemon=True)
        self._thread.start()
        self.events.put({'t': 'listening', 'port': self.port})
        return self

    def addresses(self):
        """Every ``ip:port`` a guest can use, given the interface this server bound to."""
        if self.host:
            return [format_address(self.host, self.port)]
        rows = [format_address(ip, self.port) for ip, _ in local_ipv4_all()]
        return rows or [format_address(local_ipv4(), self.port)]

    @property
    def address(self):
        return self.addresses()[0]

    def _accept_loop(self):
        while not self._closing.is_set():
            try:
                connection, peer = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._closing.is_set():
                connection.close()
                break
            self.link = Link(connection, peer).start()
            self._closing.set()  # one guest per room
            try:
                self.socket.close()
            except OSError:
                pass
            return
        self.events.put({'t': 'close', 'reason': 'listener stopped'})

    def send(self, message):
        return self.link.send(message) if self.link else False

    @property
    def connected(self):
        return bool(self.link and self.link.connected)

    def poll(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        if self.link:
            items.extend(self.link.poll())
        return items

    def close(self, reason='closed'):
        self._closing.set()
        if self.link:
            self.link.close(reason)
        try:
            if self.socket:
                self.socket.close()
        except OSError:
            pass


class Client:
    """Connect to a host and expose the same ``send`` / ``poll`` / ``close`` interface."""

    def __init__(self, host, port=DEFAULT_PORT, timeout=CONNECT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.link = None
        self.events = queue.Queue()
        self._closing = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._connect, name='dib-lan-connect', daemon=True)
        self._thread.start()
        return self

    def _connect(self):
        try:
            connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as error:
            if not self._closing.is_set():
                self.events.put({'t': 'error', 'error': f'连接失败：{error}'})
            return
        if self._closing.is_set():
            connection.close()
            return
        connection.settimeout(None)
        self.link = Link(connection, (self.host, self.port)).start()

    def send(self, message):
        return self.link.send(message) if self.link else False

    @property
    def connected(self):
        return bool(self.link and self.link.connected)

    def poll(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        if self.link:
            items.extend(self.link.poll())
        return items

    def close(self, reason='closed'):
        self._closing.set()
        if self.link:
            self.link.close(reason)


# -- table state over the wire ---------------------------------------------
#
# Positions travel normalised to the table rectangle, so a guest whose desktop is
# a different size still draws the same shot in the same place. Velocities are
# normalised by the table height, which is the unit the physics already uses for
# drag, rail rebound and the stop threshold.

def pack_state(table, seq=0, match=None):
    """Serialise one frame of the table."""
    left, top, right, bottom = table.bounds
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    bodies = [[round((body.x - left) / width, 5), round((body.y - top) / height, 5),
               round(body.vx / height, 5), round(body.vy / height, 5),
               round(body.angle, 3), round(body.impact, 4)]
              for body in table.bodies]
    return {
        't': STATE,
        'n': seq,
        'round': table.round_serial,
        'state': table.state,
        'clock': round(table.clock, 4),
        'first': None if table.first_shot_at is None else round(table.first_shot_at, 4),
        'finished': None if table.finished_at is None else round(table.finished_at, 4),
        'shots': table.shots,
        'sunk': sorted(table.sunk),
        'white_sunk': bool(table.white_sunk),
        'bodies': bodies,
        'aim': [round(table.aim[0], 5), round(table.aim[1], 5)],
        'charge': round(table.charge, 3),
        'match': match.summary() if match is not None else None,
    }


def blend_state(previous, current, alpha):
    """Interpolate positions between two packed frames; ``alpha`` 0 = previous, 1 = current."""
    if not previous or previous.get('round') != current.get('round'):
        return current
    alpha = max(0.0, min(1.0, float(alpha)))
    if alpha >= 1.0:
        return current
    old = previous.get('bodies') or []
    blended = []
    for index, body in enumerate(current.get('bodies') or []):
        if index < len(old):
            before = old[index]
            body = [before[0] + (body[0] - before[0]) * alpha,
                    before[1] + (body[1] - before[1]) * alpha,
                    body[2], body[3], body[4], body[5]]
        blended.append(body)
    result = dict(current)
    result['bodies'] = blended
    return result


def apply_state(table, payload, keep_aim=False):
    """Copy a packed frame into the guest's render-only table.

    ``keep_aim`` leaves the local aim and charge alone: the guest aims with its own
    mouse, and only the balls come from the host.
    """
    if payload.get('round') not in (None, table.round_serial):
        table.reset()
        table.round_serial = int(payload['round'])
    left, top, right, bottom = table.bounds
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    for body, values in zip(table.bodies, payload.get('bodies') or []):
        body.x = left + float(values[0]) * width
        body.y = top + float(values[1]) * height
        body.vx = float(values[2]) * height
        body.vy = float(values[3]) * height
        if len(values) > 4:
            body.angle = float(values[4])
        if len(values) > 5:
            body.impact = float(values[5])
    table.state = payload.get('state', table.state)
    table.clock = float(payload.get('clock') or 0.)
    table.first_shot_at = payload.get('first')
    table.finished_at = payload.get('finished')
    table.shots = int(payload.get('shots') or 0)
    table.sunk = set(payload.get('sunk') or ())
    table.white_sunk = bool(payload.get('white_sunk'))
    if not keep_aim:
        aim = payload.get('aim')
        if aim and len(aim) == 2:
            table.aim = (float(aim[0]), float(aim[1]))
        table.charge = float(payload.get('charge') or 0.)
    table.poses = table._poses()
    return table
