# Copyright 2024 Piper contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.
#
# Per-application button profiles.
#
# This module is the single source of truth shared by the GUI (which authors the
# rules) and the background agent (which applies them).  It knows how to:
#   * load/save the JSON config at ~/.config/piper/appprofiles.json,
#   * translate a serialisable "binding" into a write on a RatbagdButton, and
#   * pick the rule that matches a focused window.
#
# A binding set is a dict {button_index(str): binding(dict)}.  Only the buttons
# named in a set are touched; anything else on the profile is left as-is, so a
# rule can override just the two or three buttons it cares about.

import json
import os
from typing import Dict, List, Optional

from evdev import ecodes

from .ratbagd import RatbagdButton, RatbagdMacro


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "piper", "appprofiles.json")


# ---------------------------------------------------------------------------
# keycode helpers (name <-> evdev code), so the JSON stays human-readable
# ---------------------------------------------------------------------------
def key_to_code(key) -> int:
    """Accept an int evdev code or a 'KEY_*' name and return the int code."""
    if isinstance(key, int):
        return key
    code = ecodes.ecodes.get(str(key))
    if code is None:
        raise ValueError(f"Unknown key name: {key!r}")
    return code


def code_to_key(code: int) -> str:
    try:
        value = ecodes.keys[code]
    except KeyError:
        return str(code)
    return value[0] if isinstance(value, list) else value


# ---------------------------------------------------------------------------
# binding -> device write
# ---------------------------------------------------------------------------
def _macro_from_ops(ops: List[list]) -> RatbagdMacro:
    """Build a RatbagdMacro from a list of ["op", arg] pairs.

    Ops: ["tap", key], ["press", key], ["release", key], ["wait", ms].
    'tap' expands to a press + release of the same key, which is what you want
    for e.g. Alt+3.
    """
    pairs = []
    for op, arg in ops:
        if op == "tap":
            code = key_to_code(arg)
            pairs.append((RatbagdButton.Macro.KEY_PRESS, code))
            pairs.append((RatbagdButton.Macro.KEY_RELEASE, code))
        elif op == "press":
            pairs.append((RatbagdButton.Macro.KEY_PRESS, key_to_code(arg)))
        elif op == "release":
            pairs.append((RatbagdButton.Macro.KEY_RELEASE, key_to_code(arg)))
        elif op == "wait":
            pairs.append((RatbagdButton.Macro.WAIT, int(arg)))
        else:
            raise ValueError(f"Unknown macro op: {op!r}")
    return RatbagdMacro.from_ratbag(pairs)


def apply_binding(button: RatbagdButton, binding: Dict) -> None:
    """Write a single serialisable binding onto a RatbagdButton.

    Nothing is committed here; the caller commits the device once after the
    whole set has been applied.
    """
    action = binding.get("action", "none")
    if action == "button":
        button.mapping = int(binding["value"])
    elif action == "key":
        button.key = key_to_code(binding["value"])
    elif action == "special":
        value = binding["value"]
        if isinstance(value, str):
            value = RatbagdButton.ActionSpecial[value]
        button.special = int(value)
    elif action == "macro":
        button.macro = _macro_from_ops(binding["value"])
    elif action == "none":
        button.disable()
    else:
        raise ValueError(f"Unknown binding action: {action!r}")


def snapshot_binding(button) -> Dict:
    """Read a single live RatbagdButton back into a serialisable binding.

    Inverse of apply_binding(); used to capture what the user edited on the
    device (via the normal Buttons UI) into a software profile.
    """
    action = button.action_type
    if action == RatbagdButton.ActionType.BUTTON:
        return {"action": "button", "value": button.mapping}
    if action == RatbagdButton.ActionType.KEY:
        return {"action": "key", "value": code_to_key(button.key)}
    if action == RatbagdButton.ActionType.SPECIAL:
        return {"action": "special", "value": RatbagdButton.ActionSpecial(button.special).name}
    if action == RatbagdButton.ActionType.MACRO:
        ops = []
        for kind, value in button.macro.keys:
            if kind == RatbagdButton.Macro.KEY_PRESS:
                ops.append(["press", code_to_key(value)])
            elif kind == RatbagdButton.Macro.KEY_RELEASE:
                ops.append(["release", code_to_key(value)])
            elif kind == RatbagdButton.Macro.WAIT:
                ops.append(["wait", int(value)])
        return {"action": "macro", "value": ops}
    return {"action": "none"}


def snapshot_bindings(profile) -> Dict[str, Dict]:
    """Capture every button of a live profile into a binding set."""
    return {str(b.index): snapshot_binding(b) for b in profile.buttons}


def apply_binding_set(profile, bindings: Dict[str, Dict]) -> bool:
    """Apply every binding in *bindings* to *profile*'s buttons.

    Returns True if any button was changed.  The device must be committed by
    the caller afterwards.
    """
    changed = False
    by_index = {b.index: b for b in profile.buttons}
    for index_str, binding in bindings.items():
        button = by_index.get(int(index_str))
        if button is None:
            continue
        apply_binding(button, binding)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# config model
# ---------------------------------------------------------------------------
class Rule:
    """One app -> binding-set association."""

    def __init__(self, name: str, match: Dict, bindings: Dict[str, Dict]):
        self.name = name
        self.match = match  # e.g. {"resource_class": ["tibia"]}
        self.bindings = bindings

    def matches(self, resource_class: str, caption: str) -> bool:
        rc = (resource_class or "").lower()
        cap = (caption or "").lower()
        for needle in self.match.get("resource_class", []):
            if needle.lower() in rc:
                return True
        for needle in self.match.get("caption", []):
            if needle.lower() in cap:
                return True
        return False

    def to_dict(self) -> Dict:
        return {"name": self.name, "match": self.match, "bindings": self.bindings}

    @staticmethod
    def from_dict(d: Dict) -> "Rule":
        return Rule(d["name"], d.get("match", {}), d.get("bindings", {}))


class AppProfilesConfig:
    def __init__(
        self,
        default_bindings: Optional[Dict[str, Dict]] = None,
        rules: Optional[List[Rule]] = None,
        device_model: Optional[str] = None,
        debounce_ms: int = 250,
    ):
        self.default_bindings = default_bindings or {}
        self.rules = rules or []
        self.device_model = device_model  # None = first device
        self.debounce_ms = debounce_ms

    def rule_for(self, resource_class: str, caption: str) -> Optional[Rule]:
        for rule in self.rules:
            if rule.matches(resource_class, caption):
                return rule
        return None

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> Dict:
        return {
            "version": 1,
            "device_model": self.device_model,
            "debounce_ms": self.debounce_ms,
            "default_bindings": self.default_bindings,
            "rules": [r.to_dict() for r in self.rules],
        }

    def save(self, path: Optional[str] = None) -> None:
        path = path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)  # atomic

    @staticmethod
    def from_dict(d: Dict) -> "AppProfilesConfig":
        return AppProfilesConfig(
            default_bindings=d.get("default_bindings", {}),
            rules=[Rule.from_dict(r) for r in d.get("rules", [])],
            device_model=d.get("device_model"),
            debounce_ms=int(d.get("debounce_ms", 250)),
        )

    @staticmethod
    def load(path: Optional[str] = None) -> "AppProfilesConfig":
        path = path or config_path()
        try:
            with open(path) as f:
                return AppProfilesConfig.from_dict(json.load(f))
        except FileNotFoundError:
            return AppProfilesConfig()
