import QtQuick
import Quickshell
import Quickshell.Io

// Hermes Agent usage for the Omarchy agents panel.
//
// The panel reads one JSON record per agent out of the usage directory and
// draws whatever it finds there, so there is no UI to write here: this service
// runs the bundled collector on a timer, and the collector writes
// ~/.local/state/omarchy/agents/usage/hermes.json atomically. The built-in
// Agents bar widget then grows a Hermes tab next to Claude Code and Codex
// without either plugin being modified.
//
// The collector is plain stdlib Python reading Hermes' own SQLite session
// store. Nothing is uploaded and no provider endpoint is contacted.

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  // Qt.resolvedUrl() resolves against THIS file, so the collector is found
  // wherever the plugin landed — no install path or plugin id hardcoded.
  readonly property string collectorPath: {
    var url = String(Qt.resolvedUrl("collector/hermes-usage.py"))
    if (url.indexOf("file://") === 0) url = url.slice(7)
    try { url = decodeURIComponent(url) } catch (e) {}
    return url
  }

  // Trusted interpreter identity: absolute path, overridable for debugging.
  // Ambient-PATH lookup is deliberately not used (supply-chain review).
  readonly property string pythonBinary: {
    var override = Quickshell.env("HERMES_USAGE_PYTHON")
    if (override && override.length > 0) return override
    return "/usr/bin/python3"
  }

  readonly property int collectorTimeoutSec: 60
  property bool collecting: false
  property bool warnedMissingPython: false

  // Override for faster polling while developing; 15 minutes is plenty for a
  // usage meter, and the collector only reads local files.
  readonly property int refreshIntervalSec: {
    var value = Number(Quickshell.env("HERMES_USAGE_REFRESH_SEC"))
    return isFinite(value) && value >= 60 ? value : 900
  }

  property bool warnedFailure: false

  // Also reachable on demand by running the collector directly:
  //   /usr/bin/python3 <plugin dir>/collector/hermes-usage.py --write
  function refresh() {
    if (collector.running) return
    collector.command = [root.pythonBinary, root.collectorPath, "--write"]
    collector.running = true
    root.collecting = true
    watchdog.restart()
  }

  Process {
    id: collector
    running: false

    onExited: function(exitCode) {
      root.collecting = false
      watchdog.stop()
      if (exitCode === 0) {
        root.warnedFailure = false
        return
      }
      if ((exitCode === 126 || exitCode === 127) && !root.warnedMissingPython) {
        root.warnedMissingPython = true
        console.warn("hermes-usage", "python not found at", root.pythonBinary, "- set HERMES_USAGE_PYTHON to a working interpreter")
        return
      }
      if (!root.warnedFailure) {
        root.warnedFailure = true
        console.warn("hermes-usage", "collector exited with", exitCode)
      }
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var text = String(this.text || "").trim().slice(0, 2000)
        if (text !== "") console.info("hermes-usage", text)
      }
    }
  }

  Timer {
    id: watchdog
    interval: root.collectorTimeoutSec * 1000
    repeat: false
    onTriggered: {
      if (root.collecting) {
        console.warn("hermes-usage", "collector exceeded deadline, killing")
        collector.running = false
      }
    }
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}
