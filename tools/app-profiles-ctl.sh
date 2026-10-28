#!/usr/bin/env bash
# Enable/disable the Piper per-application profile feature on a running KDE/KWin
# session, straight from this source tree (no system install required).
#
#   tools/app-profiles-ctl.sh install     # install + enable KWin script + agent
#   tools/app-profiles-ctl.sh uninstall    # reverse everything
#   tools/app-profiles-ctl.sh run          # run the agent in the foreground (debug)
#   tools/app-profiles-ctl.sh logs         # follow the agent's journal
#   tools/app-profiles-ctl.sh discover     # print window classes as you focus them
#
# Everything it touches is per-user and reversible; nothing needs root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_ID="piper-appprofiles"
KWIN_DST="${XDG_DATA_HOME:-$HOME/.local/share}/kwin/scripts/$SCRIPT_ID"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="piper-profile-agent.service"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/piper/appprofiles.json"

reconfigure_kwin() {
    # Ask KWin to re-read its plugin config so the script loads/unloads now.
    qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null \
        || busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure 2>/dev/null \
        || echo "note: could not poke KWin; log out/in to apply."
}

cmd_install() {
    echo "Installing KWin script -> $KWIN_DST"
    mkdir -p "$KWIN_DST"
    cp -r "$REPO/data/kwin/$SCRIPT_ID/." "$KWIN_DST/"
    kwriteconfig6 --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" true
    reconfigure_kwin

    if [ ! -f "$CFG" ]; then
        echo "Seeding config -> $CFG (from example)"
        mkdir -p "$(dirname "$CFG")"
        cp "$REPO/data/appprofiles.example.json" "$CFG"
    else
        echo "Config already exists -> $CFG (left as-is)"
    fi

    echo "Installing user service -> $UNIT_DIR/$UNIT"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=Piper per-application mouse profile agent
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
Environment=PYTHONPATH=$REPO
ExecStart=$(command -v python3) -m piper.profileagent
WorkingDirectory=$REPO
Restart=on-failure

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT"
    echo "Done. Focus a window and check:  $0 logs"
}

cmd_uninstall() {
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    kwriteconfig6 --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" false
    rm -rf "$KWIN_DST"
    reconfigure_kwin
    echo "Removed KWin script and agent service. Config kept at $CFG"
}

cmd_run() {
    cd "$REPO"
    PYTHONPATH="$REPO" exec python3 -m piper.profileagent
}

cmd_logs() { journalctl --user -u "$UNIT" -f; }

cmd_discover() {
    echo "Focus windows to see their resource_class. Ctrl-C to stop."
    journalctl --user -u "$UNIT" -f | grep --line-buffered "focus:"
}

case "${1:-}" in
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    run) cmd_run ;;
    logs) cmd_logs ;;
    discover) cmd_discover ;;
    *) echo "usage: $0 {install|uninstall|run|logs|discover}" >&2; exit 2 ;;
esac
