import QtQuick
import Quickshell.Io

Item {
    id: root
    property int lastExitCode: -1
    function status(): string { return worker.running ? "running" : "last exit: " + lastExitCode }
    IpcHandler {
        target: "steam-game-icons"
        function status(): string { return root.status() }
        function sync(): void { root.sync() }
    }
    function sync() {
        if (!worker.running) worker.running = true
    }
    Component.onCompleted: sync()
    Timer {
        interval: 60000
        running: true
        repeat: true
        onTriggered: root.sync()
    }
    Process {
        id: worker
        onExited: function(exitCode, exitStatus) { root.lastExitCode = exitCode }
        command: ["python3", decodeURIComponent(Qt.resolvedUrl("steam_icons.py").toString().replace(/^file:\/\//, ""))]
        stderr: StdioCollector { onStreamFinished: if (text.trim()) console.warn("Steam Game Icons: " + text.trim()) }
    }
}
