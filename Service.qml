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
// Process boundary: the only executable spawned is the fixed
// /usr/bin/python3 running collector/launch.py, which validates the
// HERMES_USAGE_PYTHON override, closes the environment to an allowlist, and
// re-executes the collector in its own session. The watchdog signals the whole
// process group (SIGTERM, then SIGKILL after a grace period); stderr is
// consumed as a stream with hard caps, not accumulated.
//
// The collector is plain stdlib Python reading Hermes' own SQLite session
// store. Nothing is uploaded and no provider endpoint is contacted.

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  // Qt.resolvedUrl() resolves against THIS file, so plugin files are found
  // wherever the plugin landed — no install path or plugin id hardcoded.
  function pluginFile(relative) {
    var url = String(Qt.resolvedUrl(relative))
    if (url.indexOf("file://") === 0) url = url.slice(7)
    try { url = decodeURIComponent(url) } catch (e) {}
    return url
  }

  readonly property string launcherPath: pluginFile("collector/launch.py")
  readonly property string reaperPath: pluginFile("collector/reap.py")

  // The only executable this service ever spawns, by absolute fixed path.
  // launch.py re-checks everything else, including any interpreter override.
  readonly property string bootstrapPython: "/usr/bin/python3"

  // Closed environment: clearEnvironment removes everything first; a null
  // value passes the variable through from the shell's own environment when
  // it is set, and leaves it absent otherwise. PYTHONPATH, PYTHONHOME,
  // LD_PRELOAD and friends can never reach the collector.
  readonly property var childEnvironment: ({
    "HOME": null,
    "XDG_STATE_HOME": null,
    "HERMES_HOME": null,
    "TZ": null,
    "HERMES_USAGE_PYTHON": null
  })

  readonly property int collectorTimeoutSec: 60
  readonly property int killGraceMs: 3000
  readonly property int stderrKeepChars: 4096
  readonly property int stderrFloodChars: 65536

  // Override for faster polling while developing; 15 minutes is plenty for a
  // usage meter, and the collector only reads local files.
  readonly property int refreshIntervalSec: {
    var value = Number(Quickshell.env("HERMES_USAGE_REFRESH_SEC"))
    return isFinite(value) && value >= 60 ? value : 900
  }

  property bool collecting: false
  property bool terminating: false
  property bool warnedFailure: false
  property int stderrChars: 0
  property string stderrText: ""

  function refresh() {
    if (collector.running || root.terminating) return
    root.stderrChars = 0
    root.stderrText = ""
    collector.command = [root.bootstrapPython, "-I", root.launcherPath, "--write"]
    collector.running = true
    root.collecting = true
    watchdog.restart()
  }

  // The launcher put the collector in its own session (pid == pgid), so the
  // reaper can signal every member of that group and nothing else; it
  // re-checks pgid == pid at signal time, so this can never reach a foreign
  // process group even if the pid went stale.
  function signalGroup(signalName) {
    var pid = collector.processId
    if (pid === null || pid === undefined || pid <= 0) return
    killProcess.command = [root.bootstrapPython, "-I", root.reaperPath, signalName, String(pid)]
    killProcess.running = true
  }

  function terminateCollector(reason) {
    if (root.terminating || !collector.running) return
    root.terminating = true
    console.warn("hermes-usage", reason + "; sending SIGTERM to the process group")
    root.signalGroup("TERM")
    killGrace.restart()
  }

  Process {
    id: collector
    running: false
    environment: root.childEnvironment
    clearEnvironment: true

    // Streamed, capped stderr: raw read chunks (empty split marker), at most
    // stderrKeepChars retained for the log; a run flooding past
    // stderrFloodChars is terminated instead of accumulated.
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        root.stderrChars += chunk.length
        var room = root.stderrKeepChars - root.stderrText.length - 1
        if (room > 0) root.stderrText += chunk.slice(0, room) + "\n"
        if (root.stderrChars > root.stderrFloodChars)
          root.terminateCollector("collector stderr exceeded the live cap")
      }
    }

    onExited: function(exitCode) {
      root.collecting = false
      watchdog.stop()
      killGrace.stop()
      var text = root.stderrText.trim().slice(0, 2000)
      if (text !== "") console.info("hermes-usage", text)
      if (root.terminating) {
        root.terminating = false
        return
      }
      if (exitCode === 0) {
        root.warnedFailure = false
        return
      }
      if (!root.warnedFailure) {
        root.warnedFailure = true
        console.warn("hermes-usage", "collector exited with", exitCode)
      }
    }
  }

  Process {
    id: killProcess
    running: false
    environment: root.childEnvironment
    clearEnvironment: true
  }

  Timer {
    id: watchdog
    interval: root.collectorTimeoutSec * 1000
    repeat: false
    onTriggered: root.terminateCollector("collector exceeded its deadline")
  }

  Timer {
    id: killGrace
    interval: root.killGraceMs
    repeat: false
    onTriggered: {
      if (!collector.running) return
      console.warn("hermes-usage", "collector survived SIGTERM; sending SIGKILL to the process group")
      root.signalGroup("KILL")
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
