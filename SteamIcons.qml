import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root
    property int lastExitCode: -1
    property int completedScans: 0
    property bool pending: false
    property var seen: ({})
    // Binding observes both model membership and individual entry properties.
    readonly property string entriesSnapshot: JSON.stringify(
        Array.from(DesktopEntries.applications.values || []).map(function(app) {
            return [app.id, app.execString, app.icon]
        }))
    onEntriesSnapshotChanged: entriesChanged()
    Component.onCompleted: entriesChanged()

    function entriesChanged() {
        var next = {}
        var changed = false
        var entries = JSON.parse(entriesSnapshot)
        for (var i = 0; i < entries.length; i++) {
            var app = entries[i]
            if (!/steam:\/\/(?:rungameid|run)\/[0-9]+(?:$|[\s/"])/.test(app[1])) continue
            var key = app[0]
            var signature = JSON.stringify([app[1], app[2]])
            next[key] = signature
            // Do not react to our own repair or entries already using our icons.
            if (seen[key] !== signature && String(app[2]).indexOf("/icons/omarchy-steam-icons/") === -1)
                changed = true
        }
        seen = next
        if (changed) sync()
    }
    function status(): string {
        return (worker.running ? "running" : "watching entries; last exit: " + lastExitCode)
            + "; completed scans: " + completedScans
    }
    IpcHandler {
        target: "steam-game-icons"
        function status(): string { return root.status() }
        function sync(): void { root.sync() }
    }
    function sync() {
        pending = true
        debounce.restart()
    }
    // One-shot debounce coalesces bursts of launcher changes, never polls.
    Timer {
        id: debounce
        interval: 500
        repeat: false
        onTriggered: {
            if (worker.running || !root.pending) return
            root.pending = false
            worker.running = true
        }
    }
    Process {
        id: worker
        onExited: function(exitCode, exitStatus) {
            root.lastExitCode = exitCode
            root.completedScans++
            if (root.pending) debounce.restart()
        }
        command: ["/usr/bin/python3", "-I", decodeURIComponent(Qt.resolvedUrl("steam_icons.py").toString().replace(/^file:\/\//, ""))]
        stderr: StdioCollector { onStreamFinished: if (text.trim()) console.warn("Steam Game Icons: " + text.trim()) }
    }
}
