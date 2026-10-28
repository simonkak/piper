// Piper App Profiles - KWin focus reporter.
//
// On every window activation we hand the focused window's resourceClass (the
// lowercased WM_CLASS / app id) and caption to the Piper agent over the session
// bus. The agent, not KWin, decides what to do with it -- KWin's callDBus can
// only reach the session bus, which is exactly where the agent lives.
//
// Works on KWin 6 (windowActivated / activeWindow) with a fallback to the KWin 5
// names (clientActivated / activeClient) so the same script is portable.

function report(window) {
    if (!window) {
        return;
    }
    var resourceClass = window.resourceClass ? String(window.resourceClass) : "";
    var caption = window.caption ? String(window.caption) : "";
    callDBus(
        "org.piper.AppProfiles",
        "/org/piper/AppProfiles",
        "org.piper.AppProfiles",
        "WindowActivated",
        resourceClass,
        caption
    );
}

if (workspace.windowActivated) {
    // KWin 6
    workspace.windowActivated.connect(report);
    report(workspace.activeWindow);
} else if (workspace.clientActivated) {
    // KWin 5
    workspace.clientActivated.connect(report);
    report(workspace.activeClient);
}
