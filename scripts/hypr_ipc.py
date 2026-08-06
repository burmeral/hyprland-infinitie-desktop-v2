#!/usr/bin/env python3
"""Hyprland 0.56.1 positional-dispatch compatibility for Sarod's helpers."""

import json
import subprocess
import time


def _run(args, timeout=2):
    return subprocess.run(
        ["hyprctl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def hyprctl_json(args, timeout=2):
    result = _run([*args, "-j"], timeout=timeout)
    return json.loads(result.stdout) if result.stdout.strip() else None


def dispatch(lua_call, timeout=2):
    """dispatch a single Lua dispatcher call string, e.g. 'hl.dsp.window.move({...})'."""
    return _run(["dispatch", lua_call], timeout=timeout)


def dispatch_async(lua_call):
    subprocess.Popen(
        ["hyprctl", "dispatch", lua_call],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def batch(lua_calls, timeout=5):
    """lua_calls: list of Lua call strings."""
    command = " ; ".join(
        "dispatch " + call
        for call in lua_calls
    )
    return _run(["--batch", command], timeout=timeout)


def batch_async(lua_calls):
    if not lua_calls:
        return
    command = " ; ".join(
        "dispatch " + call
        for call in lua_calls
    )
    subprocess.Popen(
        ["hyprctl", "--batch", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _exact_move_positions(lua_calls):
    """
    Extract {address: (x, y)} from move Lua call strings for wait_for_positions.
    Recognises: hl.dsp.window.move({ x=X, y=Y, window="address:ADDR" })
    """
    positions = {}
    for call in lua_calls:
        # Must be a window.move call with explicit x/y coords
        if "hl.dsp.window.move" not in call:
            continue
        try:
            x_part = _lua_field(call, "x")
            y_part = _lua_field(call, "y")
            addr = _lua_window_address(call)
            if x_part is not None and y_part is not None and addr:
                positions[addr] = (int(x_part), int(y_part))
        except (ValueError, AttributeError):
            continue
    return positions


def _lua_field(call, field):
    """Extract a numeric field value from a Lua table literal string."""
    import re
    m = re.search(rf'\b{field}\s*=\s*(-?\d+)', call)
    return m.group(1) if m else None


def _lua_window_address(call):
    """Extract the hex address from window="address:0x..." in a Lua call string."""
    import re
    m = re.search(r'window\s*=\s*"address:(0x[0-9a-fA-F]+)"', call)
    return m.group(1) if m else None


def wait_for_positions(positions, timeout=1):
    if not positions:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            clients = hyprctl_json(["clients"], timeout=min(timeout, 0.2)) or []
        except (json.JSONDecodeError, subprocess.TimeoutExpired):
            clients = []
        current = {
            client.get("address"): tuple(client.get("at", ()))
            for client in clients
        }
        if all(current.get(address) == position for address, position in positions.items()):
            return True
        time.sleep(0.01)
    return False


def batch_wait(lua_calls, timeout=2):
    result = batch(lua_calls, timeout=timeout)
    if not wait_for_positions(_exact_move_positions(lua_calls), timeout=timeout):
        raise TimeoutError("Hyprland did not apply the requested move batch")
    return result


# ---------------------------------------------------------------------------
# Lua call builders — return a string ready to pass to dispatch() or batch()
# ---------------------------------------------------------------------------

def toggle_floating_lua(address=None):
    if address:
        return f'hl.dsp.window.float({{ window = "address:{address}" }})'
    return 'hl.dsp.window.float({})'


def toggle_floating(address=None):
    return dispatch(toggle_floating_lua(address))


def focus_window_lua(address):
    return f'hl.dsp.focus({{ window = "address:{address}" }})'


def focus_window(address):
    return dispatch(focus_window_lua(address))


def move_focus_lua(direction):
    return f'hl.dsp.focus({{ direction = "{direction}" }})'


def move_focus(direction):
    return dispatch(move_focus_lua(direction))


def move_window_tiled_lua(direction):
    return f'hl.dsp.window.move({{ direction = "{direction}" }})'


def move_window_tiled(direction):
    return dispatch(move_window_tiled_lua(direction))


def exec_cmd_lua(command):
    # hl.dsp.exec_cmd is not dispatched via hyprctl dispatch —
    # it's called directly as a top-level hyprctl command.
    # Callers that previously passed this to batch() must switch to
    # exec_cmd_direct() or exec_cmd_async().
    return f'hl.dsp.exec_cmd({json.dumps(command)})'


def exec_cmd_direct(command, timeout=2):
    return _run(["hl.dsp.exec_cmd", command], timeout=timeout)


def exec_cmd_async(command):
    subprocess.Popen(
        ["hyprctl", "hl.dsp.exec_cmd", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def move_window_exact_lua(x, y, address):
    return (
        f'hl.dsp.window.move({{ x = {int(x)}, y = {int(y)},'
        f' window = "address:{address}" }})'
    )


def move_window_exact(x, y, address, timeout=2):
    return dispatch(move_window_exact_lua(x, y, address), timeout=timeout)


def move_window_exact_wait(x, y, address, timeout=2):
    call = move_window_exact_lua(x, y, address)
    result = dispatch(call, timeout=timeout)
    if not wait_for_positions(_exact_move_positions([call]), timeout=timeout):
        raise TimeoutError(f"Hyprland did not move {address} to ({int(x)}, {int(y)})")
    return result


def move_window_exact_async(x, y, address):
    dispatch_async(move_window_exact_lua(x, y, address))


def resize_window_exact_lua(width, height, address):
    return (
        f'hl.dsp.window.resize({{ x = {int(width)}, y = {int(height)},'
        f' window = "address:{address}" }})'
    )


def resize_window_exact(width, height, address, timeout=2):
    return dispatch(
        resize_window_exact_lua(width, height, address),
        timeout=timeout,
    )
