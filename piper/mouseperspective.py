# SPDX-License-Identifier: GPL-2.0-or-later

from gettext import gettext as _
from typing import Optional

from . import appprofiles
from .advancedpage import AdvancedPage
from .buttonspage import ButtonsPage
from .ledspage import LedsPage
from .ratbagd import RatbagdDevice, RatbagdProfile
from .resolutionspage import ResolutionsPage
from .softwareprofiles import AgentProxy, SoftwareProfileRow, SoftwareProfileStore
from .util.gobject import connect_signal_with_weak_ref

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk  # noqa


@Gtk.Template(resource_path="/org/freedesktop/Piper/ui/MousePerspective.ui")
class MousePerspective(Gtk.Overlay):
    """The perspective to configure a mouse.

    Profiles here are *software* profiles (named binding sets stored in the
    config the focus agent reads), surfaced through piper's native profile
    selector. The device's single hardware profile is the editing canvas: the
    selected software profile's bindings are loaded onto it so the normal
    Buttons / Resolutions / Advanced pages can edit them, and Apply snapshots
    the canvas back into the profile and reloads the agent.
    """

    __gtype_name__ = "MousePerspective"

    _titlebar: Gtk.HeaderBar = Gtk.Template.Child()  # type: ignore
    add_profile_button: Gtk.Button = Gtk.Template.Child()  # type: ignore
    button_commit: Gtk.Button = Gtk.Template.Child()  # type: ignore
    button_profile: Gtk.Button = Gtk.Template.Child()  # type: ignore
    label_profile: Gtk.Label = Gtk.Template.Child()  # type: ignore
    listbox_profiles: Gtk.ListBox = Gtk.Template.Child()  # type: ignore
    notification_error: Gtk.Revealer = Gtk.Template.Child()  # type: ignore
    stack: Gtk.Stack = Gtk.Template.Child()  # type: ignore

    def __init__(self, *args, **kwargs) -> None:
        Gtk.Overlay.__init__(self, *args, **kwargs)
        self._device: Optional[RatbagdDevice] = None
        self._profile: Optional[RatbagdProfile] = None  # the hardware canvas
        self._notification_error_timeout_id = 0
        self._store: Optional[SoftwareProfileStore] = None
        self._baseline: dict = {}  # device state at open; fallback for unset buttons
        self._active_sw = None  # the currently loaded SoftwareProfile
        self._agent = AgentProxy()
        self._match_entry: Optional[Gtk.Entry] = None
        self._app_combo: Optional[Gtk.ComboBoxText] = None
        self._suppress_match_update = False

    @GObject.Property
    def name(self) -> str:
        return "mouse_perspective"

    @GObject.Property
    def titlebar(self) -> Gtk.Widget:
        return self._titlebar

    @GObject.Property
    def can_go_back(self) -> bool:
        return True

    @GObject.Property
    def can_shutdown(self) -> bool:
        # Software profiles persist to their own config on Apply; pending
        # (uncommitted) device state is discarded harmlessly on close and the
        # agent restores the right profile on the next focus change.
        return True

    @GObject.Property
    def device(self) -> RatbagdDevice:
        assert self._device is not None
        return self._device

    # -- setup -------------------------------------------------------------
    def set_device(self, device: RatbagdDevice) -> None:
        self._device = device
        connect_signal_with_weak_ref(
            self, device, "resync", lambda _: self._show_notification_error()
        )

        active_profile = device.active_profile
        assert active_profile is not None
        self._profile = active_profile
        # Full snapshot of the device as opened; used so that loading a profile
        # which doesn't mention a button reverts it to this baseline rather than
        # keeping the previously loaded profile's mapping.
        self._baseline = appprofiles.snapshot_bindings(active_profile)

        self._store = SoftwareProfileStore()
        self._build_assignment_ui()
        self.button_profile.set_visible(True)

        self._rebuild_pages()
        self._populate_profile_list()
        self._load_profile(self._store.profiles[0])

    def _rebuild_pages(self) -> None:
        assert self._device is not None and self._profile is not None
        profile = self._profile
        self.stack.foreach(Gtk.Widget.destroy)
        if profile.resolutions:
            self.stack.add_titled(
                ResolutionsPage(self._device, profile), "resolutions", _("Resolutions")
            )
        if profile.buttons:
            self.stack.add_titled(
                ButtonsPage(self._device, profile), "buttons", _("Buttons")
            )
        if profile.leds:
            self.stack.add_titled(LedsPage(self._device, profile), "leds", _("LEDs"))
        are_report_rates_supported = (
            profile.report_rate != 0 and len(profile.report_rates) != 0
        )
        if (
            profile.angle_snapping != -1
            or profile.debounces
            or are_report_rates_supported
        ):
            self.stack.add_titled(
                AdvancedPage(self._device, profile), "advanced", _("Advanced")
            )

    # -- profile list ------------------------------------------------------
    def _populate_profile_list(self) -> None:
        assert self._store is not None
        self.listbox_profiles.foreach(Gtk.Widget.destroy)
        for profile in self._store.profiles:
            self.listbox_profiles.add(
                SoftwareProfileRow(profile, self._on_delete_profile)
            )
        self.listbox_profiles.show_all()
        self._select_row_for(self._active_sw)

    def _select_row_for(self, sw_profile) -> None:
        if sw_profile is None:
            return
        for row in self.listbox_profiles.get_children():
            if getattr(row, "profile", None) is sw_profile:
                self.listbox_profiles.select_row(row)
                break

    def _load_profile(self, sw_profile) -> None:
        """Load a software profile onto the device canvas and refresh the UI."""
        assert self._profile is not None
        self._active_sw = sw_profile
        # Apply as a complete set: baseline for unmentioned buttons, then the
        # profile's own bindings on top. Guarantees no stale mappings leak in
        # from the previously loaded profile.
        merged = dict(self._baseline)
        merged.update(sw_profile.bindings)
        appprofiles.apply_binding_set(self._profile, merged)
        self._rebuild_pages()
        self.label_profile.set_label(sw_profile.name)
        self._select_row_for(sw_profile)

        self._suppress_match_update = True
        if self._match_entry is not None:
            self._match_entry.set_text(sw_profile.match_text)
            self._match_entry.set_sensitive(not sw_profile.is_default)
        self._suppress_match_update = False

    # -- app assignment UI (added into the profile popover) ---------------
    def _build_assignment_ui(self) -> None:
        popover = self.button_profile.get_popover()
        if popover is None:
            return
        box = popover.get_child()
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        section.set_margin_top(4)
        section.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        section.add(
            Gtk.Label(label=_("Apply when focused app matches:"), xalign=0)
        )
        self._match_entry = Gtk.Entry()
        self._match_entry.set_placeholder_text("com.tibia.client")
        self._match_entry.connect("changed", self._on_match_changed)
        section.add(self._match_entry)

        picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._app_combo = Gtk.ComboBoxText()
        for rc, _caption in self._agent.seen_classes():
            self._app_combo.append(rc, rc)
        use = Gtk.Button(label=_("Use focused app"))
        use.connect("clicked", self._on_use_app)
        picker.pack_start(self._app_combo, True, True, 0)
        picker.pack_start(use, False, False, 0)
        section.add(picker)
        section.show_all()
        box.pack_start(section, False, False, 0)

    def _on_match_changed(self, entry: Gtk.Entry) -> None:
        if self._suppress_match_update or self._active_sw is None:
            return
        self._active_sw.set_match_text(entry.get_text())

    def _on_use_app(self, _button: Gtk.Button) -> None:
        if self._app_combo is None or self._match_entry is None:
            return
        rc = self._app_combo.get_active_id()
        if rc:
            self._match_entry.set_text(rc)

    # -- notifications -----------------------------------------------------
    def _hide_notification_error(self) -> None:
        if self._notification_error_timeout_id != 0:
            GLib.source_remove(self._notification_error_timeout_id)
            self._notification_error_timeout_id = 0
        self.notification_error.set_reveal_child(False)

    def _show_notification_error(self) -> None:
        self.notification_error.set_reveal_child(True)
        self._notification_error_timeout_id = GLib.timeout_add_seconds(
            5, self._on_notification_error_timeout
        )

    def _on_notification_error_timeout(self) -> bool:
        self._hide_notification_error()
        return False

    # -- callbacks ---------------------------------------------------------
    @Gtk.Template.Callback("_on_save_button_clicked")
    def _on_save_button_clicked(self, _button: Gtk.Button) -> None:
        assert self._device is not None and self._store is not None
        # Capture what the user edited on the mouse back into the active profile.
        if self._active_sw is not None and self._profile is not None:
            self._active_sw.bindings = appprofiles.snapshot_bindings(self._profile)
        self._store.save()
        self._agent.reload()
        self._device.commit()

    @Gtk.Template.Callback("_on_notification_error_close_clicked")
    def _on_notification_error_close_clicked(self, button: Gtk.Button) -> None:
        self._hide_notification_error()

    @Gtk.Template.Callback("_on_profile_row_activated")
    def _on_profile_row_activated(self, listbox: Gtk.ListBox, row) -> None:
        if getattr(row, "profile", None) is not None:
            self._load_profile(row.profile)
            popover = self.button_profile.get_popover()
            if popover is not None:
                popover.popdown()

    @Gtk.Template.Callback("_on_add_profile_button_clicked")
    def _on_add_profile_button_clicked(self, button: Gtk.Button) -> None:
        assert self._store is not None and self._profile is not None
        # A new profile starts as a snapshot of what's currently on the mouse.
        bindings = appprofiles.snapshot_bindings(self._profile)
        profile = self._store.add(_("New profile"), bindings)
        self._populate_profile_list()
        self._load_profile(profile)

    def _on_delete_profile(self, sw_profile) -> None:
        assert self._store is not None
        self._store.remove(sw_profile)
        if self._active_sw is sw_profile:
            self._active_sw = self._store.profiles[0]
        self._populate_profile_list()
        self._load_profile(self._active_sw)
