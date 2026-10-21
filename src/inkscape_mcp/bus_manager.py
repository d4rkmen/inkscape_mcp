"""Managed D-Bus session + Inkscape auto-launcher for the live bridge.

On Linux the desktop session already provides a D-Bus session bus and the user's
Inkscape registers on it. Windows and macOS have neither: a normally-launched
Inkscape has no bus to register on, so the live bridge cannot see it.

This module makes the live bridge "just work" on those platforms by:

  1. ensuring a private ``dbus-daemon`` session bus is running (reused across MCP
     restarts via a state file), and
  2. ensuring Inkscape is running *under that bus* (launched with
     ``DBUS_SESSION_BUS_ADDRESS`` set), so its ``org.gtk.Actions`` surface is exported.

Everything here is best-effort and lazy: nothing is spawned until a live operation needs
it. Linux never touches this module.

Discovery / overrides (env):
  INKSCAPE_MCP_DBUS_DAEMON   full path to the dbus-daemon binary
  INKSCAPE_MCP_GDBUS         full path to gdbus
  INKS_INKSCAPE_BIN / INKSCAPE_BIN   Inkscape executable (already used elsewhere)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import platform_support

log = logging.getLogger(__name__)

STATE_DIR = Path(os.path.expanduser("~/.cache/inkscape_mcp"))
ADDRESS_FILE = STATE_DIR / "bus_address"
PID_FILE = STATE_DIR / "inkscape.pid"
MACOS_BUS_DIR = STATE_DIR / "bus"
MACOS_BUS_CONFIG = STATE_DIR / "dbus-session.conf"

# Where MSYS2 puts dbus-daemon; used if not overridden and not on PATH.
_DEFAULT_DBUS_DAEMONS = (
    r"C:\msys64\mingw64\bin\dbus-daemon.exe",
    r"C:\msys64\ucrt64\bin\dbus-daemon.exe",
)
# Homebrew (Apple Silicon / Intel) and MacPorts.
_MACOS_DBUS_DAEMONS = (
    "/opt/homebrew/bin/dbus-daemon",
    "/usr/local/bin/dbus-daemon",
    "/opt/local/bin/dbus-daemon",
)

_INKSCAPE_REGISTER_TIMEOUT_S = 25.0
_BUS_PRINT_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.3

# Detached, no console window — survives MCP-server restarts.
_DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW


class BusError(RuntimeError):
    pass


# ---- platform ----------------------------------------------------------------------


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def manages_bus() -> bool:
    """True on platforms where the MCP has to own the session bus."""
    return is_windows() or is_macos()


# ---- discovery ---------------------------------------------------------------------


def find_dbus_daemon() -> str | None:
    override = os.environ.get("INKSCAPE_MCP_DBUS_DAEMON")
    if override and Path(override).exists():
        return override
    from shutil import which

    found = which("dbus-daemon")
    if found:
        return found
    for cand in _DEFAULT_DBUS_DAEMONS if is_windows() else _MACOS_DBUS_DAEMONS:
        if Path(cand).exists():
            return cand
    return None


def find_gdbus(inkscape_executable: str | None = None) -> str | None:
    override = os.environ.get("INKSCAPE_MCP_GDBUS")
    if override and Path(override).exists():
        return override
    # gdbus.exe ships next to inkscape.exe on Windows.
    if is_windows() and inkscape_executable:
        cand = Path(inkscape_executable).with_name("gdbus.exe")
        if cand.exists():
            return str(cand)
    from shutil import which

    return which("gdbus")


# ---- bus lifecycle -----------------------------------------------------------------


def _bus_reachable(gdbus_path: str | None, address: str) -> bool:
    """True if a dbus-daemon is answering at ``address`` (independent of Inkscape)."""
    if not gdbus_path:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 — fixed gdbus path
            [
                gdbus_path,
                "call",
                "--address",
                address,
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.GetId",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _write_macos_bus_config() -> Path:
    """Minimal session-bus config for macOS.

    Homebrew's stock session.conf listens on a launchd-provided socket, which only
    exists inside a GUI login session; the MCP may run from a terminal or an IDE
    without that environment. A plain unix socket keeps the bus self-contained.
    ``DBUS_COOKIE_SHA1`` is required because jeepney cannot use SO_PEERCRED
    (Linux-only) for EXTERNAL auth on macOS.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MACOS_BUS_DIR.mkdir(parents=True, exist_ok=True)
    MACOS_BUS_CONFIG.write_text(
        '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"\n'
        ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
        "<busconfig>\n"
        "  <type>session</type>\n"
        "  <keep_umask/>\n"
        f"  <listen>unix:dir={MACOS_BUS_DIR}</listen>\n"
        "  <auth>EXTERNAL</auth>\n"
        "  <auth>DBUS_COOKIE_SHA1</auth>\n"
        '  <policy context="default">\n'
        '    <allow send_destination="*"/>\n'
        '    <allow receive_sender="*"/>\n'
        '    <allow own="*"/>\n'
        "  </policy>\n"
        "</busconfig>\n",
        encoding="utf-8",
    )
    return MACOS_BUS_CONFIG


def ensure_bus(gdbus_path: str | None = None) -> str:
    """Return a reachable session-bus address, reusing or starting a dbus-daemon.

    The address is cached in ``ADDRESS_FILE`` so repeated MCP-server starts reuse one
    daemon instead of leaking a new one each time. The address is also exported as
    ``DBUS_SESSION_BUS_ADDRESS`` so jeepney's ``bus="SESSION"`` connects to it.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if ADDRESS_FILE.exists():
        cached = ADDRESS_FILE.read_text(encoding="utf-8").strip()
        if cached and _bus_reachable(gdbus_path, cached):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = cached
            return cached

    daemon = find_dbus_daemon()
    if not daemon:
        hint = (
            "Install it with 'brew install dbus' or set INKSCAPE_MCP_DBUS_DAEMON to its path."
            if is_macos()
            else "Install it (MSYS2: 'pacman -S mingw-w64-x86_64-dbus') or set INKSCAPE_MCP_DBUS_DAEMON to its path."
        )
        raise BusError(f"dbus-daemon not found. {hint}")

    addr_out = STATE_DIR / "dbus_daemon.addr"
    if addr_out.exists():
        addr_out.unlink()

    if is_macos():
        argv = [daemon, "--config-file", str(_write_macos_bus_config()), "--print-address", "--nofork"]
        popen_kwargs: dict = {"start_new_session": True}
    else:
        argv = [daemon, "--session", "--print-address", "--nofork"]
        popen_kwargs = {"creationflags": _DETACHED}

    # dbus-daemon prints the address to stdout once, then keeps serving. Redirect to a
    # file and start detached so it outlives this process.
    with addr_out.open("w", encoding="utf-8") as fh:
        subprocess.Popen(  # noqa: S603 — fixed daemon path
            argv,
            stdout=fh,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **popen_kwargs,
        )

    deadline = time.monotonic() + _BUS_PRINT_TIMEOUT_S
    address = ""
    while time.monotonic() < deadline:
        if addr_out.exists():
            text = addr_out.read_text(encoding="utf-8").strip()
            line = text.splitlines()[0].strip() if text else ""
            if line and "guid=" in line:
                address = line
                break
        time.sleep(_POLL_INTERVAL_S)
    if not address:
        raise BusError("dbus-daemon did not print a bus address in time")

    ADDRESS_FILE.write_text(address, encoding="utf-8")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    log.info("Started dbus-daemon session bus at %s", address)
    return address


# ---- inkscape lifecycle ------------------------------------------------------------


def _inkscape_registered(gdbus_path: str | None, address: str) -> bool:
    """True once Inkscape has a live document window on the bus.

    The app name registers a beat before the document object
    (``/org/inkscape/Inkscape/document/N``); window-scope ops need the document, so
    "ready" means a numeric document child exists — not merely that the app pings.
    """
    if not gdbus_path:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 — fixed gdbus path
            [
                gdbus_path,
                "introspect",
                "--address",
                address,
                "--dest",
                "org.inkscape.Inkscape",
                "--object-path",
                "/org/inkscape/Inkscape/document",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return re.search(r"node\s+\d+\s*\{", proc.stdout) is not None


_BLANK_CANVAS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd" '
    'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    'width="210mm" height="297mm" viewBox="0 0 210 297" version="1.1">\n'
    '  <sodipodi:namedview inkscape:document-units="mm"/>\n'
    "</svg>\n"
)


def default_canvas() -> str:
    """Path to a persistent blank canvas, created on first use.

    Inkscape launched with NO file shows the welcome screen and exports no document
    object over D-Bus, so window-scope ops have nothing to target. Opening a concrete
    file skips the welcome screen and guarantees a ``/document/N`` node. This file is the
    default shared canvas when the caller doesn't specify one.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "canvas.svg"
    if not path.exists():
        path.write_text(_BLANK_CANVAS, encoding="utf-8")
    return str(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _launched_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _wait_registered(gdbus_path: str | None, address: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _inkscape_registered(gdbus_path, address):
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False


def ensure_inkscape(
    gdbus_path: str | None,
    address: str,
    inkscape_executable: str,
    open_path: str | None = None,
) -> bool:
    """Ensure Inkscape is running on ``address``; launch it under the bus if not.

    Returns True if Inkscape is registered (already-running or freshly launched and
    registered within the timeout). Launch is detached so closing the MCP server does
    not kill the user's editor. When ``open_path`` is omitted, a default blank canvas is
    opened so a live document window exists.
    """
    if is_macos():
        # Must be written before Inkscape starts: it reads preferences at launch.
        platform_support.set_inkscape_python_preference()

    if _inkscape_registered(gdbus_path, address):
        return True

    if not inkscape_executable or not Path(inkscape_executable).exists():
        raise BusError(f"Inkscape executable not found: {inkscape_executable!r}")

    # Never pile up instances. On macOS a wedged app (e.g. a modal dialog or the
    # dyld_start bug) stays alive but never registers; launching another copy on
    # every live call just exhausts the process table. Give the existing process
    # the registration window instead.
    existing = _launched_pid()
    if existing is not None and _pid_alive(existing):
        if _wait_registered(gdbus_path, address, _INKSCAPE_REGISTER_TIMEOUT_S):
            return True
        log.warning(
            "Inkscape (pid %s) is running but did not register on the session bus; not launching another instance",
            existing,
        )
        return False

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    # An empty LANG (common in IDE terminals) makes GTK abort on UTF-8 conversion.
    if not env.get("LANG"):
        env["LANG"] = "C.UTF-8"
    if not env.get("LC_ALL"):
        env["LC_ALL"] = "C.UTF-8"
    argv = [inkscape_executable, open_path or default_canvas()]

    log.info("Launching Inkscape under MCP session bus: %s", argv)
    popen_kwargs: dict = {"start_new_session": True} if is_macos() else {"creationflags": _DETACHED}
    proc = subprocess.Popen(  # noqa: S603 — inkscape path validated above
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        **popen_kwargs,
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    return _wait_registered(gdbus_path, address, _INKSCAPE_REGISTER_TIMEOUT_S)
