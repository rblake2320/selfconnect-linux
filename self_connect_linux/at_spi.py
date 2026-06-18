"""
Phase 5 — AT-SPI accessibility tree access.

AT-SPI2 (Assistive Technology Service Provider Interface) exposes the full GUI
widget tree of any running application.  On DGX Spark, GNOME is the desktop; all
GNOME apps, Chromium, and GTK/Qt apps publish an AT-SPI tree over D-Bus.

Dependency: python3-gi (gobject-introspection) is installed as a system package
on Ubuntu 24.04 but is NOT available in Miniconda Python.  This module bridges
the gap by running queries through /usr/bin/python3 via subprocess.  The caller's
API is clean; the gi dependency is isolated in the subprocess.

Security: parameters are NEVER interpolated into subprocess code strings.  They
are passed as JSON via stdin and read inside the subprocess as _P["key"].  This
prevents code injection regardless of the parameter values.

Capability gate: call at_spi_available() before any query.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

_SYSTEM_PYTHON = "/usr/bin/python3"

# Environment keys forwarded to AT-SPI subprocess — nothing else, no secrets.
_ENV_KEYS = (
    "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "HOME", "USER", "PATH", "XAUTHORITY",
)


def _minimal_env() -> dict[str, str]:
    return {k: v for k in _ENV_KEYS if (v := os.environ.get(k)) is not None}


# AT-SPI query bootstrap — injected into every subprocess call.
# Parameters arrive from stdin as JSON: _P = json.load(sys.stdin).
# Nothing from the caller is interpolated into this code string.
_ATSPI_BOOTSTRAP = """
import gi, json, sys
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi
Atspi.init()
desktop = Atspi.get_desktop(0)
_P = json.load(sys.stdin)

def iter_apps():
    for i in range(desktop.get_child_count()):
        yield desktop.get_child_at_index(i)

def iter_children(node, max_depth=8):
    if max_depth == 0:
        return
    try:
        count = node.get_child_count()
    except Exception:
        return
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
            yield child
            yield from iter_children(child, max_depth - 1)
        except Exception:
            continue

def node_info(n):
    try:
        name = n.get_name() or ''
        role = n.get_role_name() or ''
        try:
            ti = n.query_text()
            text = ti.get_text(0, -1) if ti else ''
        except Exception:
            text = ''
        return {'name': name, 'role': role, 'text': text[:2000]}
    except Exception:
        return None
"""


def _run(code: str, params: dict | None = None, timeout: int = 10) -> Any:
    """Run AT-SPI query via system Python with params passed through stdin."""
    full = _ATSPI_BOOTSTRAP + "\n" + code
    input_data = json.dumps(params or {}).encode()
    result = subprocess.run(
        [_SYSTEM_PYTHON, "-c", full],
        input=input_data,
        capture_output=True,
        timeout=timeout,
        env=_minimal_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"AT-SPI query failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def at_spi_available() -> bool:
    """True if AT-SPI is accessible via /usr/bin/python3 + python3-gi."""
    if not os.path.exists(_SYSTEM_PYTHON):
        return False
    if not os.environ.get("DISPLAY"):
        return False
    try:
        result = subprocess.run(
            [_SYSTEM_PYTHON, "-c",
             "import gi; gi.require_version('Atspi','2.0'); "
             "from gi.repository import Atspi; Atspi.init(); "
             "print('ok')"],
            capture_output=True, text=True, timeout=5,
            env=_minimal_env(),
        )
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_applications() -> list[str]:
    """Return names of all applications currently registered with AT-SPI."""
    return _run("""
apps = []
for app in iter_apps():
    try:
        apps.append(app.get_name() or '')
    except Exception:
        pass
print(json.dumps(apps))
""")


def find_application(name: str) -> str | None:
    """Return the AT-SPI application name for the first app matching *name* (substring)."""
    for app_name in list_applications():
        if name.lower() in app_name.lower():
            return app_name
    return None


def get_application_widgets(app_name: str, max_depth: int = 6) -> list[dict]:
    """
    Return a flat list of widget info dicts for all nodes in *app_name*'s tree.
    Each dict has keys: name, role, text.
    """
    if not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    return _run("""
target = next((a for a in iter_apps() if _P["app_name"].lower() in (a.get_name() or '').lower()), None)
if target is None:
    print(json.dumps([]))
else:
    nodes = [node_info(target)]
    for child in iter_children(target, _P["max_depth"]):
        info = node_info(child)
        if info:
            nodes.append(info)
    print(json.dumps([n for n in nodes if n]))
""", {"app_name": app_name, "max_depth": max_depth})


def get_text(app_name: str, role: str | None = None, label: str | None = None) -> str:
    """
    Read the text content of a widget in *app_name*.

    *role*  — AT-SPI role name to match, e.g. "terminal", "text", "entry"
    *label* — widget name/label substring to match

    Returns the first match's text, or empty string if not found.
    """
    widgets = get_application_widgets(app_name)
    for w in widgets:
        if role and role.lower() not in w.get("role", "").lower():
            continue
        if label and label.lower() not in w.get("name", "").lower():
            continue
        text = w.get("text", "")
        if text:
            return text
    return ""


def activate(app_name: str, role: str | None = None, label: str | None = None) -> bool:
    """
    Invoke (click/activate) a widget in *app_name*.

    Returns True if a matching widget was found and activated.
    """
    return _run("""
target = next((a for a in iter_apps() if _P["app_name"].lower() in (a.get_name() or '').lower()), None)
found = False
if target:
    for child in iter_children(target, 8):
        try:
            role_ok = not _P["role"] or (_P["role"].lower() in (child.get_role_name() or '').lower())
            name_ok = not _P["label"] or (_P["label"].lower() in (child.get_name() or '').lower())
            if role_ok and name_ok:
                action = child.query_action()
                if action and action.get_n_actions() > 0:
                    action.do_action(0)
                    found = True
                    break
        except Exception:
            continue
print(json.dumps(found))
""", {"app_name": app_name, "role": role, "label": label})


def get_focused_text() -> str:
    """Return the text of the currently keyboard-focused widget, or empty string."""
    return _run("""
result = ''
for app in iter_apps():
    for child in iter_children(app, 8):
        try:
            state = child.get_state_set()
            if state.contains(Atspi.StateType.FOCUSED):
                ti = child.query_text()
                result = ti.get_text(0, -1) if ti else ''
                break
        except Exception:
            continue
    if result:
        break
print(json.dumps(result[:4000]))
""")
