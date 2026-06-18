"""
Phase 5 — AT-SPI accessibility tree access.

AT-SPI2 (Assistive Technology Service Provider Interface) exposes the full GUI
widget tree of any running application.  On DGX Spark, GNOME is the desktop; all
GNOME apps, Chromium, and GTK/Qt apps publish an AT-SPI tree over D-Bus.

Dependency: python3-gi (gobject-introspection) is installed as a system package
on Ubuntu 24.04 but is NOT available in Miniconda Python.  This module bridges
the gap by running queries through /usr/bin/python3 via subprocess.  The caller's
API is clean; the gi dependency is isolated in the subprocess.

Capability gate: call at_spi_available() before any query.

Key operations:
  list_applications()              → list of app names visible to AT-SPI
  find_application(name)           → first app whose name matches (substring)
  get_text(app_name, role, label)  → read text from a widget
  activate(app_name, role, label)  → click/invoke a widget
  get_focused_text()               → text of the currently focused widget
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

_SYSTEM_PYTHON = "/usr/bin/python3"

# AT-SPI query bootstrap — injected into every subprocess call
_ATSPI_BOOTSTRAP = """
import gi, json, sys
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi
Atspi.init()
desktop = Atspi.get_desktop(0)

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


def _run(code: str, timeout: int = 10) -> Any:
    """Run AT-SPI query via system Python, return parsed JSON output."""
    full = _ATSPI_BOOTSTRAP + "\n" + code
    result = subprocess.run(
        [_SYSTEM_PYTHON, "-c", full],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
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
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
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
    code = f"""
target = next((a for a in iter_apps() if {app_name!r}.lower() in (a.get_name() or '').lower()), None)
if target is None:
    print(json.dumps([]))
else:
    nodes = [node_info(target)]
    for child in iter_children(target, {max_depth}):
        info = node_info(child)
        if info:
            nodes.append(info)
    print(json.dumps([n for n in nodes if n]))
"""
    return _run(code)


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
    code = f"""
target = next((a for a in iter_apps() if {app_name!r}.lower() in (a.get_name() or '').lower()), None)
found = False
if target:
    for child in iter_children(target, 8):
        try:
            role_ok = not {role!r} or ({role!r}.lower() in (child.get_role_name() or '').lower())
            name_ok = not {label!r} or ({label!r}.lower() in (child.get_name() or '').lower())
            if role_ok and name_ok:
                action = child.query_action()
                if action and action.get_n_actions() > 0:
                    action.do_action(0)
                    found = True
                    break
        except Exception:
            continue
print(json.dumps(found))
"""
    return _run(code)


def get_focused_text() -> str:
    """Return the text of the currently keyboard-focused widget, or empty string."""
    return _run("""
focused = Atspi.get_desktop(0)
# Walk to find focused node
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
