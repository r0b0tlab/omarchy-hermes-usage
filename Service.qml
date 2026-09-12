import QtQuick
import Quickshell
import Quickshell.Io
Item {
  id: root
  property var shell: null
  property var manifest: null
  readonly property string launcherPath: decodeURIComponent(String(Qt.resolvedUrl("collector/launch.py")).replace(/^file:\/\//, ""))
  readonly property int refreshMs: {
    var n = Number(Quickshell.env("HERMES_USAGE_REFRESH_SEC"))
    return isFinite(n) && n >= 60 ? Math.min(n, 86400) * 1000 : 900000
  }
  function refresh() { if (!worker.running) worker.running = true }
  Process {
    id: worker
    command: ["/usr/bin/python3", "-I", root.launcherPath, "--write"]
    clearEnvironment: true
    environment: ({"HOME": null, "XDG_STATE_HOME": null, "HERMES_HOME": null,
                   "TZ": null, "HERMES_USAGE_PYTHON": null})
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(text) { console.info("hermes-usage", text.slice(0, 2100)) }
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
  // RELEASE BLOCKER: Quickshell 0.3.1 Process destruction SIGKILLs the
  // supervisor immediately. This TERM is best-effort, NOT an unload guarantee.
  // Host must retain Process until onExited before destroying this service.
  Component.onDestruction: { if (worker.running) worker.signal(15) }
}
