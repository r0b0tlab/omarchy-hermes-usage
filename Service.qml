import QtQuick
import Quickshell
import Quickshell.Io
Item {
  id: root
  property var shell: null
  property var manifest: null
  readonly property string launcherPath: decodeURIComponent(String(Qt.resolvedUrl("collector/bootstrap.py")).replace(/^file:\/\//, ""))
  readonly property int refreshMs: {
    var n = Number(Quickshell.env("HERMES_USAGE_REFRESH_SEC"))
    return isFinite(n) && n >= 60 ? Math.min(n, 86400) * 1000 : 900000
  }
  readonly property var childEnvironment: ({"HOME": null, "XDG_STATE_HOME": null,
      "HERMES_HOME": null, "TZ": null, "HERMES_USAGE_PYTHON": null})
  property int outUnits: 0
  property int errUnits: 0
  function refresh() { if (!worker.running) worker.running = true }
  Process {
    id: worker
    command: ["/usr/bin/python3", "-I", root.launcherPath, "--write"]
    clearEnvironment: true
    environment: root.childEnvironment
    // Host-owned upstream lease: EOF covers even shell loss before entry.
    stdinEnabled: true
    onRunningChanged: { if (running) { root.outUnits = 0; root.errUnits = 0 } }
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(text) {
        root.outUnits = Math.min(262145, root.outUnits + text.length)
        if (root.outUnits > 262144) worker.running = false
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(text) {
        var room = Math.max(0, 2100 - root.errUnits)
        root.errUnits = Math.min(8193, root.errUnits + text.length)
        if (room > 0) console.info("hermes-usage", text.slice(0, room))
        if (root.errUnits > 8192) worker.running = false
      }
    }
    onExited: function(code) {
      if (code !== 0 && code !== 1) console.warn("hermes-usage", "refresh failed", code)
    }
  }
  IpcHandler {
    target: "io.github.r0b0tlab.hermes-usage"
    function refresh(): string { root.refresh(); return "requested" }
  }
  Timer { interval: root.refreshMs; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }
  // Destruction SIGKILLs only the disposable bootstrap, closing its lease.
  // Never signal(int): the startup PID can be zero (kill(0, ...)).
}
