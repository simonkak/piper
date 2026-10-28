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

INTROSPECTION_XML = """
<node>
  <interface name='org.piper.AppProfiles'>
    <method name='WindowActivated'>
      <arg type='s' name='resource_class' direction='in'/>
      <arg type='s' name='caption' direction='in'/>
    </method>
    <method name='ApplyRule'>
      <arg type='s' name='name' direction='in'/>
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
            return  # already applied; no device write
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
                appprofiles.apply_binding_set(profile, bindings)
                device.commit()
                self._current = name
                log(f"applied '{name}' ({len(bindings)} button(s))")
                return
            except Exception as e:  # never let a bad rule kill the agent
                last_error = e
                self._ratbag = None  # force a fresh connection next time
        log(f"failed to apply '{name}': {last_error!r}")

    def apply_rule_by_name(self, name: str):
        self._reload_config(force=True)
        if name == DEFAULT:
            self._apply(DEFAULT, self._config.default_bindings)
            return
        for rule in self._config.rules:
            if rule.name == name:
                self._apply(rule.name, rule.bindings)
                return
        log(f"no such rule: {name!r}")

    def reload(self):
        self._reload_config(force=True)
        # Force the next activation to re-apply, since bindings may have changed.
        self._current = None


# ---------------------------------------------------------------------------
# D-Bus glue
# ---------------------------------------------------------------------------
def _handle_method_call(
    connection, sender, object_path, interface_name, method_name, parameters, invocation, agent
):
    if method_name == "WindowActivated":
        rc, caption = parameters.unpack()
        agent.window_activated(rc, caption)
        invocation.return_value(None)
    elif method_name == "ApplyRule":
        (name,) = parameters.unpack()
        agent.apply_rule_by_name(name)
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


def main(argv=None):
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

    def on_name_lost(connection, name):
        log(f"lost bus name {name!r} (another agent running?)")
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        None,
        on_name_lost,
    )

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
