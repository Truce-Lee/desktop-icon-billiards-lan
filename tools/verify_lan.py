"""Maintainer smoke test for LAN play on the real desktop.

This tool takes over the real desktop for ~30 seconds exactly like playing the
game: it hosts a room from the source tree, connects a headless guest over
loopback, aims with the real mouse, lets the shot settle, hands the turn to the
guest and checks the guest's copy converges. It then restores the desktop and
verifies the restoration is exact.

Run from the repository root:

    ..\\.venv\\Scripts\\python.exe tools\\verify_lan.py

Artifacts (screenshots and a JSON report) land in ``build/verify``.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import dib.ui as ui  # noqa: E402
from dib import config, net  # noqa: E402
from dib.lanplay import LanPlay  # noqa: E402
from dib.paths import runtime_directory  # noqa: E402
from dib.shell import LayoutSession, ShellDesktop  # noqa: E402
from dib.table import BilliardsMotion  # noqa: E402

ARTIFACTS = ROOT / 'build' / 'verify'
JOURNAL = runtime_directory() / 'layout.json'
RESULT = ARTIFACTS / 'lan-smoke.json'
CLASS_NAME = 'DesktopIconBilliardsOverlay'
TOLERANCE = .005


class VerificationError(RuntimeError):
    pass


class GuestStub:
    """The part of BilliardsOverlay that LanPlay uses, for the headless guest."""

    def __init__(self, table):
        self.table = table

    def trim_balls(self, count):
        return self.table.set_ball_count(count)

    def after_reset(self):
        pass


class Guest(threading.Thread):
    """A headless guest with its own, differently sized table."""

    def __init__(self):
        super().__init__(name='lan-smoke-guest', daemon=True)
        # A second "machine": different resolution and a different icon count.
        self.table = BilliardsMotion((0, 0, 1280, 720), [(64, 64)] * 30)
        self.play = None
        self.stop = threading.Event()
        self.error = None
        self.frames = 0

    def run(self):
        endpoint = None
        try:
            endpoint = net.Client('127.0.0.1', config.LAN_PORT).start()
            self.play = LanPlay(GuestStub(self.table), 'guest', endpoint, ('主机', '客机'))
            while not self.stop.is_set():
                now = time.perf_counter()
                self.play.pump(now)
                self.play.render(now)
                self.frames += 1
                time.sleep(1 / 120)
        except Exception as exc:  # surfaced in the report
            self.error = exc
            traceback.print_exc()
        finally:
            if endpoint is not None:
                endpoint.close('smoke done')


def normalised(table):
    left, top, right, bottom = table.bounds
    width, height = right - left, bottom - top
    return [((body.x - left) / width, (body.y - top) / height) for body in table.bodies]


def overlay_children(parent):
    found = []

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == CLASS_NAME:
            found.append(hwnd)
        return True

    win32gui.EnumChildWindows(parent, visit, None)
    return found


def port_is_free(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('', port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    ui.messagebox = types.SimpleNamespace(
        showinfo=lambda *args, **kwargs: None,
        showerror=lambda *args, **kwargs: None,
        showwarning=lambda *args, **kwargs: None,
        askyesno=lambda *args, **kwargs: False,
        askokcancel=lambda *args, **kwargs: False,
    )
    result = {'success': False, 'stages': [], 'errors': []}
    desktop = ShellDesktop()
    original = desktop.items()
    original_flags = desktop.get_flags()
    result['icon_count'] = len(original)
    app = None
    root = None
    guest = None
    try:
        if not port_is_free(config.LAN_PORT):
            raise VerificationError(f'TCP port {config.LAN_PORT} is already in use')

        if JOURNAL.exists() and json.loads(JOURNAL.read_text(encoding='utf-8')).get('active'):
            LayoutSession(desktop, JOURNAL).restore()
            result['stages'].append('recovered-stale-journal')

        from dib.instance import claim_instance_mutex

        handle = claim_instance_mutex()
        if handle is None:
            raise VerificationError('Another Desktop Icon Billiards instance is running')

        root = tk.Tk()
        app = ui.DesktopBilliardsApp(root, handle)

        def pump(seconds=0.02):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                root.update()
                time.sleep(0.008)

        def wait_for(predicate, timeout, message):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                root.update()
                time.sleep(0.01)
                if predicate():
                    return True
            raise VerificationError(f'timeout waiting for {message}')

        wait_for(lambda: app.runner is not None and app.runner.shown.is_set(), 30, 'overlay shown')
        overlay = app.runner.overlay
        table = overlay.table
        wait_for(lambda: overlay.transition is None, 8, 'rack intro to finish')
        result['stages'].append('overlay-shown')

        # -- host a room and connect the headless guest ---------------------
        app.host_lan()
        if app.lan_endpoint is None:
            raise VerificationError(f'hosting failed: {overlay.lan.status if overlay.lan else "no session"}')
        play = overlay.lan
        result['host_address'] = app.lan_endpoint.address
        result['stages'].append('hosting')

        guest = Guest()
        guest.start()
        wait_for(lambda: play.started and guest.play is not None and guest.play.started, 20,
                 'LAN handshake')
        result['stages'].append('handshake')
        result['rack'] = {'host': len(table.icons), 'guest': len(guest.table.icons)}
        if len(table.icons) != len(guest.table.icons):
            raise VerificationError('the two ends disagree on the rack size')
        result['scores_after_start'] = play.scores()

        screenshot(overlay, 'lan-host.png')

        # -- aim with the real mouse and take the opening shot --------------
        # If another window covers the whole table, the desktop hit test refuses the
        # click (by design), so the shot is injected through the same pointer callback
        # the mouse would use. Either way the LAN routing code is the one under test.
        point = pick_desktop_point(root, overlay, table)
        if point is not None:
            win32api.SetCursorPos(point)
            pump(0.2)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            pump(0.45)
            charge = table.charge
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            pump(0.1)
            result['charge_observed'] = round(charge, 3)
            if charge <= 0:
                raise VerificationError('holding the left button did not charge the cue over LAN')
            result['shot_path'] = 'mouse'
            screenshot(overlay, 'lan-host.png')
        else:
            result['shot_path'] = 'pointer-callback'
            overlay.pointer.send('shot', (1.0, 0.05), 0.62)
            pump(0.1)
        wait_for(lambda: table.shots >= 1, 3, 'the shot to leave through the pointer')
        result['stages'].append('mouse-shot')
        wait_for(lambda: table.state in ('ready', 'ball_in_hand', 'finished'), 15, 'host shot to settle')
        result['host_shot'] = {
            'state': table.state,
            'sunk': len(table.sunk),
            'scores': play.scores(),
            'turn': play.match.turn,
            'foul': play.match.foul,
        }

        # -- hand the turn over and let the guest shoot ---------------------
        if play.match.turn == 0:
            play.match.turn = 1  # the opening shot kept the turn; force the guest's path
        wait_for(lambda: guest.play.local_turn, 5, 'the guest to learn it is its turn')
        result['stages'].append('guest-turn')
        if not guest.play.shoot((1.0, 0.12), 0.62, time.perf_counter()):
            raise VerificationError(f'guest shot refused: {guest.play.status}')
        wait_for(lambda: play.match.shots[1] >= 1, 5, 'the guest shot to arrive')
        wait_for(lambda: table.state in ('ready', 'ball_in_hand', 'finished'), 15, 'guest shot to settle')
        result['guest_shot'] = {'state': table.state, 'scores': play.scores()}
        result['stages'].append('guest-shot')

        # -- the guest copy must converge on the host positions -------------
        pump(0.6)
        worst = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                    for a, b in zip(normalised(table), normalised(guest.table)))
        result['convergence'] = round(worst, 6)
        if worst > TOLERANCE:
            raise VerificationError(f'guest diverged by {worst:.4f} of the table')
        result['stages'].append('guest-converged')

        # -- a restart request travels back to the host ---------------------
        round_before = table.round_serial
        guest.play.request_restart()
        wait_for(lambda: table.round_serial > round_before and play.match.scores == [0, 0]
                 and table.state == 'ready', 8, 'the guest restart to rebuild the rack')
        result['stages'].append('guest-restart')

        # -- shutdown and exact restoration ---------------------------------
        listview = overlay.listview
        parent = overlay.parent
        guest.stop.set()
        guest.join(3)
        app.close()
        app = None
        root = None
        result['stages'].append('closed')

        if not win32gui.IsWindowVisible(listview):
            raise VerificationError('Explorer icon list view was not shown again')
        if overlay_children(parent):
            raise VerificationError('overlay child windows survived the shutdown')
        if desktop.items() != original:
            raise VerificationError('desktop icon layout was not restored exactly')
        if desktop.get_flags() != original_flags:
            raise VerificationError('desktop arrange flags were not restored')
        if JOURNAL.exists() and json.loads(JOURNAL.read_text(encoding='utf-8')).get('active'):
            raise VerificationError('layout journal is still active after shutdown')
        result['stages'].append('exact-restore')
        result['success'] = True
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
        result['traceback'] = traceback.format_exc()
        if guest is not None:
            guest.stop.set()
        if app is not None:
            try:
                app.close()
            except Exception as close_error:
                result['errors'].append(f'close failed: {close_error}')
        elif root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    finally:
        # Last-resort safety net: never leave the desktop hidden or rearranged.
        try:
            if JOURNAL.exists() and json.loads(JOURNAL.read_text(encoding='utf-8')).get('active'):
                LayoutSession(ShellDesktop(), JOURNAL).restore()
                result['stages'].append('safety-restore')
        except Exception as exc:
            result['errors'].append(f'safety restore failed: {exc}')
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['success']:
        raise SystemExit(1)


def screenshot(overlay, name):
    left, top, right, bottom = overlay.primary_bounds
    x, y = win32gui.ClientToScreen(overlay.parent, (left, top))
    try:
        ImageGrab.grab(bbox=(x, y, x + right - left, y + bottom - top)).save(ARTIFACTS / name)
    except Exception:
        pass


def pick_desktop_point(root, overlay, table):
    """Find a table spot that is not covered by another window, then aim there.

    Returns ``None`` when every candidate is covered, so the caller can fall back to
    the pointer callback instead of clicking into someone else's window.
    """
    left, top, right, bottom = table.bounds
    white = table.white
    candidates = [
        (white.x, top + 90),               # straight up: a clean foul (no ball contact)
        (right - 80, white.y),             # along the horizontal, towards the right rail
        (left + 80, bottom - 90),
    ]
    for x, y in candidates:
        point = win32gui.ClientToScreen(overlay.parent, (round(x), round(y)))
        win32api.SetCursorPos(point)
        for _ in range(6):
            root.update()
            time.sleep(0.02)
        if overlay.blocked_by is None:
            return point
    return None


if __name__ == '__main__':
    main()
