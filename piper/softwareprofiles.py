# SPDX-License-Identifier: GPL-2.0-or-later
#
# Software profiles: the unified model.
#
# A device like the G305 exposes a single hardware profile, so piper's native
# profile selector is normally hidden. Here we populate that same selector with
# *software* profiles: named binding sets stored in ~/.config/piper/appprofiles.json.
# Selecting one loads its bindings onto the device so the normal Buttons /
# Resolutions / Advanced pages edit it; "Apply" snapshots the device back into
# the selected profile and the focus agent switches between them per window.

from gettext import gettext as _

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from . import appprofiles  # noqa: E402

DEFAULT_NAME = _("Default (no app)")

_AGENT_NAME = "org.piper.AppProfiles"
_AGENT_PATH = "/org/piper/AppProfiles"


class AgentProxy:
    """Talks to the background focus agent on the session bus; no-ops if it is
    not running."""

    def __init__(self):
        self._proxy = None
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                _AGENT_NAME,
                _AGENT_PATH,
                _AGENT_NAME,
                None,
            )
        except GLib.Error:
            self._proxy = None

    @property
    def available(self):
        return self._proxy is not None and self._proxy.get_name_owner() is not None

    def _call(self, method, params=None):
        if not self.available:
            return None
        try:
            return self._proxy.call_sync(
                method, params, Gio.DBusCallFlags.NONE, 1000, None
            )
        except GLib.Error:
            return None

    def reload(self):
        self._call("Reload")

    def apply_rule(self, name):
        self._call("ApplyRule", GLib.Variant("(s)", (name,)))

    def seen_classes(self):
        res = self._call("SeenClasses")
        return list(res.unpack()[0]) if res is not None else []


class SoftwareProfile:
    """One named binding set, optionally matched to an application."""

    def __init__(self, name, match=None, bindings=None, is_default=False):
        self.name = name
        self.match = match or {}
        self.bindings = bindings or {}
        self.is_default = is_default

    @property
    def match_text(self):
        parts = self.match.get("resource_class", []) + self.match.get("caption", [])
        return ", ".join(parts)

    def set_match_text(self, text):
        needles = [n.strip() for n in text.split(",") if n.strip()]
        self.match = {"resource_class": needles}

    # The sentinel the agent uses for the no-match fallback.
    @property
    def agent_name(self):
        return "__default__" if self.is_default else self.name


class SoftwareProfileStore:
    """Loads/saves the list of software profiles via AppProfilesConfig."""

    def __init__(self):
        self.config = appprofiles.AppProfilesConfig.load()
        self.profiles = []
        self._load()

    def _load(self):
        self.profiles = [
            SoftwareProfile(DEFAULT_NAME, {}, self.config.default_bindings, is_default=True)
        ]
        for rule in self.config.rules:
            self.profiles.append(SoftwareProfile(rule.name, rule.match, rule.bindings))

    def add(self, name, bindings):
        profile = SoftwareProfile(name, {"resource_class": []}, dict(bindings))
        self.profiles.append(profile)
        return profile

    def remove(self, profile):
        if not profile.is_default and profile in self.profiles:
            self.profiles.remove(profile)

    def save(self):
        """Serialise the profiles back into the config file."""
        default = self.profiles[0]
        self.config.default_bindings = default.bindings
        self.config.rules = [
            appprofiles.Rule(p.name, p.match, p.bindings) for p in self.profiles[1:]
        ]
        self.config.save()


class SoftwareProfileRow(Gtk.ListBoxRow):
    """A row in piper's profile popover, backed by a SoftwareProfile."""

    __gtype_name__ = "SoftwareProfileRow"

    def __init__(self, profile, on_delete):
        Gtk.ListBoxRow.__init__(self)
        self.profile = profile
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        label = Gtk.Label(label=profile.name, xalign=0)
        box.pack_start(label, True, True, 0)
        if not profile.is_default:
            delete = Gtk.Button.new_from_icon_name(
                "edit-delete-symbolic", Gtk.IconSize.BUTTON
            )
            delete.set_relief(Gtk.ReliefStyle.NONE)
            delete.set_tooltip_text(_("Delete this profile"))
            delete.connect("clicked", lambda _b: on_delete(profile))
            box.pack_start(delete, False, False, 0)
        self.add(box)
        self.show_all()

    @GObject.Property
    def name(self):
        return self.profile.name
