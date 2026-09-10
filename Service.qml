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

  // Override for faster polling while developing; 15 minutes is plenty for a
  // usage meter, and the collector only reads local files.
  readonly property int refreshIntervalSec: {
    var value = Number(Quickshell.env("HERMES_USAGE_REFRESH_SEC"))
    return isFinite(value) && value >= 60 ? value : 900
  }

  property bool warnedFailure: false

  // Also reachable on demand:
  //   omarchy-shell shell call io.github.am423.hermes-usage refresh
  function refresh() {
    if (collector.running) return
    collector.command = ["python3", collectorPath, "--write"]
    collector.running = true
  }

  Process {
    id: collector
    running: false

    onExited: function(exitCode, exitStatus) {
      if (exitCode === 0) {
        root.warnedFailure = false
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
        var text = String(this.text || "").trim()
        if (text !== "") console.info("hermes-usage", text)
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
