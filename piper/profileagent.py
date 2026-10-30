# Copyright 2024 Piper contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.
#
# Background agent for per-application button profiles.
#
# It owns the SESSION-bus name org.piper.AppProfiles.  The KWin script calls its
# WindowActivated() method on every focus change; the agent decides which rule
# applies and, when that differs from what is currently on the device, rewrites
# the button mappings via ratbagd (SYSTEM bus) and commits once.
#
# Run standalone:  python3 -m piper.profileagent

import os
import sys

from gi.repository import Gio, GLib

from . import appprofiles
from .ratbagd import Ratbagd, RatbagdUnavailableError, RatbagdIncompatibleError

BUS_NAME = "org.piper.AppProfiles"
OBJECT_PATH = "/org/piper/AppProfiles"

# The KWin focus-reporter script; we reload it once we own our bus name so it
# re-reports the window in focus (see _retrigger_kwin_focus).
KWIN_SCRIPT_ID = "piper-appprofiles"

INTROSPECTION_XML = """
<node>
  <interface name='org.piper.AppProfiles'>
    <method name='WindowActivated'>
      <arg type='s' name='resource_class' direction='in'/>
      <arg type='s' name='caption' direction='in'/>
    </method>
    <method name='Reload'/>
    <method name='Current'>
      <arg type='s' name='name' direction='out'/>
    </method>
    <method name='SeenClasses'>
      <arg type='a(ss)' name='classes' direction='out'/>
    </method>
  </interface>
</node>
"""

# Sentinel rule name used when no rule matches (the "default" binding set).
DEFAULT = "__default__"


def log(*args):
    print("[piper-profile-agent]", *args, file=sys.stderr, flush=True)


class ProfileAgent:
    def __init__(self, api_version: int = 2):
        self._api_version = api_version
        self._ratbag = None
        self._config = appprofiles.AppProfilesConfig()
        self._config_mtime = None
        self._current = None  # name of the rule currently applied, or None
        self._debounce_source = None
        self._pending = None  # (name, bindings)
        self._last_rc = None  # for discovery logging
        self._seen = []  # recent [(resource_class, caption)], newest first
        self._session_bus = None  # set once we own our name; used on resume
        self._reload_config(force=True)

    def seen_classes(self):
        return list(self._seen)

    # -- ratbagd ----------------------------------------------------------
    def _ratbagd(self):
        if self._ratbag is None:
            self._ratbag = Ratbagd(self._api_version)
        return self._ratbag

    def _device(self):
        ratbag = self._ratbagd()
        devices = ratbag.devices
        if not devices:
            return None
        model = self._config.device_model
        if model:
            for d in devices:
                if d.model == model:
                    return d
        return devices[0]

    # -- config -----------------------------------------------------------
    def _reload_config(self, force: bool = False):
        path = appprofiles.config_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if force or mtime != self._config_mtime:
            self._config = appprofiles.AppProfilesConfig.load(path)
            self._config_mtime = mtime
            log(f"loaded config: {len(self._config.rules)} rule(s)")

    # -- switching --------------------------------------------------------
    def window_activated(self, resource_class: str, caption: str):
        self._reload_config()
        # Log each new window class once, so you can discover what e.g. Tibia
        # reports and copy it into a rule's "resource_class" match.
        if resource_class != self._last_rc:
            self._last_rc = resource_class
            log(f"focus: resource_class={resource_class!r} caption={caption!r}")
            # Keep a short, deduped MRU list for the GUI's app picker.
            self._seen = [(resource_class, caption)] + [
                e for e in self._seen if e[0] != resource_class
            ]
            del self._seen[12:]
        rule = self._config.rule_for(resource_class, caption)
        if rule is None:
            name, bindings = DEFAULT, self._config.default_bindings
        else:
            name, bindings = rule.name, rule.bindings

        if name == self._current:
            # Already applied. Also cancel any debounced switch to a *different*
            # profile that is now stale (e.g. focus B then quickly refocus A),
            # otherwise that timer would wrongly apply B's bindings to A.
            self._pending = None
            if self._debounce_source is not None:
                GLib.source_remove(self._debounce_source)
                self._debounce_source = None
            return
        if name == DEFAULT and not bindings:
            # No default set configured and we've never applied anything: leave
            # the device exactly as the user last set it in piper.
            if self._current is None:
                return
        self._pending = (name, bindings)
        self._arm_debounce()

    def _arm_debounce(self):
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
        delay = max(0, int(self._config.debounce_ms))
        self._debounce_source = GLib.timeout_add(delay, self._apply_pending)

    def _apply_pending(self):
        self._debounce_source = None
        if self._pending is None:
            return False
        name, bindings = self._pending
        self._pending = None
        self._apply(name, bindings)
        return False  # one-shot

    def _apply(self, name: str, bindings: dict):
        # ratbagd may idle-exit; if our cached client has gone stale the first
        # attempt fails, so we drop it and reconnect once before giving up.
        last_error = None
        for attempt in (1, 2):
            try:
                device = self._device()
                if device is None:
                    raise RuntimeError("no device")
                profile = device.active_profile
                if profile is None:
                    raise RuntimeError("no active profile")
                changed = appprofiles.apply_binding_set(profile, bindings)
                if changed:
                    device.commit()  # skip the device write when nothing changed
                self._current = name
                log(f"applied '{name}' ({len(bindings)} button(s))")
                return
            except Exception as e:  # never let a bad rule kill the agent
                last_error = e
                self._ratbag = None  # force a fresh connection next time
        log(f"failed to apply '{name}': {last_error!r}")

    def reload(self):
        # Re-read the config the GUI just saved.  Deliberately do NOT re-apply
        # to the device here: editing always happens with the piper window
        # focused, so re-applying the focused window's (no-rule) default profile
        # would overwrite the bindings the GUI just committed and look like the
        # save rolled back.  Invalidating _current is enough — the next genuine
        # focus change applies the freshly saved bindings.
        self._reload_config(force=True)
        self._current = None

    def resume(self):
        # Coming back from suspend, ratbagd may have exited and the device
        # re-enumerated, leaving our cached connection and _current marker
        # stale.  Drop them and re-report the window in focus so the right
        # profile is re-applied on a fresh connection; without this the device
        # can stay stuck on whatever was applied before sleep, because a focus
        # event matching _current is skipped.
        log("resume from sleep: re-applying profile")
        self._ratbag = None
        self._current = None
        if self._session_bus is not None:
            _retrigger_kwin_focus(self._session_bus)


# ---------------------------------------------------------------------------
# D-Bus glue
# ---------------------------------------------------------------------------
def _kwin_script_path():
    """Locate the installed KWin focus-reporter script across the XDG data dirs."""
    rel = os.path.join(
        "kwin", "scripts", KWIN_SCRIPT_ID, "contents", "code", "main.js"
    )
    dirs = [os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for d in dirs:
        path = os.path.join(d, rel)
        if os.path.isfile(path):
            return path
    return None


def _retrigger_kwin_focus(connection):
    """Reload the KWin focus script so it re-reports the window in focus.

    This closes the login-time race: the script's initial report can fire
    before we own our bus name and be lost, leaving the device on its default
    bindings until the next focus change.  Reloading once we are ready forces a
    fresh report.  If KWin is not up yet (we started first) the call fails
    harmlessly and KWin's own auto-load delivers the first report instead.
    """
    path = _kwin_script_path()
    if path is None:
        return

    def call(method, params):
        connection.call_sync(
            "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
            method, params, None, Gio.DBusCallFlags.NONE, 1000, None,
        )

    try:
        call("unloadScript", GLib.Variant("(s)", (KWIN_SCRIPT_ID,)))
        call("loadScript", GLib.Variant("(ss)", (path, KWIN_SCRIPT_ID)))
        call("start", None)
        log("re-triggered KWin focus report")
    except GLib.Error as e:
        log(f"KWin focus re-trigger skipped ({e.message}); relying on its auto-load")


def _handle_method_call(
    connection, sender, object_path, interface_name, method_name, parameters, invocation, agent
):
    if method_name == "WindowActivated":
        rc, caption = parameters.unpack()
        agent.window_activated(rc, caption)
        invocation.return_value(None)
    elif method_name == "Reload":
        agent.reload()
        invocation.return_value(None)
    elif method_name == "Current":
        invocation.return_value(GLib.Variant("(s)", (agent._current or "",)))
    elif method_name == "SeenClasses":
        invocation.return_value(GLib.Variant("(a(ss))", (agent.seen_classes(),)))
    else:
        invocation.return_error_literal(
            Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method_name
        )


def main():
    agent = ProfileAgent()

    node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    interface_info = node_info.interfaces[0]
    loop = GLib.MainLoop()

    def on_bus_acquired(connection, name):
        try:
            connection.register_object(
                OBJECT_PATH,
                interface_info,
                lambda c, s, o, i, m, p, inv: _handle_method_call(
                    c, s, o, i, m, p, inv, agent
                ),
                None,
                None,
            )
            log(f"registered {OBJECT_PATH}")
        except Exception as e:
            log(f"failed to register object: {e!r}")
            loop.quit()

    def on_name_acquired(connection, name):
        # Now that the name is ours (and the object is registered), remember the
        # session bus for resume handling and nudge KWin to re-report the
        # focused window so a boot/login race can't leave us idle on the default
        # bindings.
        agent._session_bus = connection
        _retrigger_kwin_focus(connection)

    def on_name_lost(connection, name):
        log(f"lost bus name {name!r} (another agent running?)")
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )

    # Re-apply the right profile after suspend: logind emits PrepareForSleep
    # with start=False on resume, when ratbagd/the device may have gone stale.
    def on_prepare_for_sleep(conn, sender, path, iface, signal, params, _user):
        (going_to_sleep,) = params.unpack()
        if not going_to_sleep:
            agent.resume()

    try:
        system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        system_bus.signal_subscribe(
            "org.freedesktop.login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "/org/freedesktop/login1",
            None,
            Gio.DBusSignalFlags.NONE,
            on_prepare_for_sleep,
            None,
        )
    except GLib.Error as e:
        log(f"login1 resume signal unavailable ({e.message}); resume re-apply off")

    try:
        agent._ratbagd()
        log("connected to ratbagd")
    except (RatbagdUnavailableError, RatbagdIncompatibleError) as e:
        log(f"warning: ratbagd not ready ({e!r}); will retry on first event")

    log("running")
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
