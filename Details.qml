pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons

Item {
  id: root
  property var shell: null
  property var manifest: null
  property var service: null
  property var record: ({})
  property bool closingFromHost: false
  property double now: Date.now() / 1000
  readonly property bool opened: window.visible
  readonly property var details: record.details || ({})
  readonly property var totals: details.totals || ({})
  readonly property var accounts: (record.accounts || []).filter(function(a) { return a.expiresAt > root.now && a.fetchedAt <= root.now })
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state")
  readonly property bool canRefresh: service !== null && typeof service.refresh === "function" && service.busy !== true

  function open(payload) {
    closingFromHost = false
    now = Date.now() / 1000
    dataFile.reload()
    window.visible = true
  }
  function close() {
    closingFromHost = true
    window.visible = false
    closingFromHost = false
  }
  function requestClose() {
    if (shell && typeof shell.hide === "function") shell.hide("io.github.r0b0tlab.hermes-usage")
    else close()
  }
  function refreshLocal() { if (canRefresh) service.refresh() }
  function num(v) { return typeof v === "number" && isFinite(v) ? v.toLocaleString(Qt.locale(), 'f', 0) : "Unavailable" }
  function money(v) { return typeof v === "number" && isFinite(v) ? "$" + v.toFixed(4) : "Unavailable" }
  function rows(group) { return Object.keys(group || {}).sort().map(function(k) { return {name: k, value: group[k]} }) }
  function visibleWindows(a) { return (a.windows || []).filter(function(w) { return w.resetAt === null || w.resetAt > root.now }) }
  function resetText(stamp) { return stamp ? "resets in " + Math.max(0, Math.ceil((stamp-root.now)/60)) + " min" : "reset time unavailable" }
  function bucketText(b) {
    var s = b.latestStatusRows || ({})
    return num(b.calls) + " API calls" + (b.unknownCallRows > 0 ? " (partial; " + num(b.unknownCallRows) + " rows unknown)" : "")
      + " · " + num(b.tokens) + " tokens\nEstimated: " + money(b.estimatedUsd) + " · Actual recorded: " + money(b.actualUsd)
      + "\nRows by latest status — estimated " + num(s.estimated) + ", actual " + num(s.actual)
      + ", included " + num(s.included) + ", unknown " + num(s.unknown)
  }
  function accessText(a) {
    if (a.accessStatus === "member-cap-exceeded") return "Member spending cap exceeded — account credit is not spendable by this member."
    if (a.accessStatus === "denied") return "Paid access denied — positive account credit does not imply access."
    if (a.accessStatus === "allowed") return "Paid access allowed at observation; account credit is not a conversation budget."
    return "Member paid access unavailable; account credit may not be spendable."
  }

  component Info: Label {
    width: parent ? parent.width : 0
    color: Color.foreground
    font.pixelSize: 14
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
  }
  component Heading: Info { font.pixelSize: 20; font.bold: true; topPadding: 10 }

  FileView {
    id: dataFile
    path: root.stateHome + "/omarchy/agents/usage/hermes.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var parsed = JSON.parse(text())
        root.record = parsed && parsed.id === "hermes" ? parsed : ({})
      } catch (e) { root.record = ({}) }
    }
    onLoadFailed: root.record = ({})
  }
  Timer { interval: 1000; running: window.visible; repeat: true; onTriggered: root.now = Date.now()/1000 }

  FloatingWindow {
    id: window
    visible: false
    title: "Hermes usage details"
    implicitWidth: 780
    implicitHeight: 860
    minimumSize: Qt.size(420, 360)
    color: Color.background
    onVisibleChanged: {
      if (visible) root.now = Date.now()/1000
      else if (!root.closingFromHost && root.shell && typeof root.shell.hide === "function")
        root.shell.hide("io.github.r0b0tlab.hermes-usage")
    }
    ScrollView {
      id: scrollArea
      anchors.fill: parent
      anchors.margins: 24
      contentWidth: availableWidth
      Column {
        width: scrollArea.availableWidth
        spacing: 10
        Info { text: "Hermes usage"; font.pixelSize: 28; font.bold: true }
        Info { text: "Device activity · account allowances are separate"; color: Color.accent }
        Info { text: "Local record: " + (root.record.updatedAt || "Unavailable") }
        Row {
          spacing: 10
          Button { text: "Refresh local data"; enabled: root.canRefresh; onClicked: root.refreshLocal() }
          Button { text: "Close"; onClicked: root.requestClose() }
        }
        Info { text: "Local refresh never contacts providers. Authenticated export is a separate, explicit Hermes command." }
        Info {
          text: root.details.truncated ? "Partial history — a store failed or a scan limit was reached."
                : (root.record.hasLocalStats ? "Bounded local history (not a complete billing ledger)" : "Local activity unavailable")
          color: root.details.truncated ? Qt.lighter(Color.urgent, 1.5) : Color.foreground
        }
        Info {
          visible: root.record.hasLocalStats === true
          text: "Today: " + root.num(root.record.todayTotalTokens) + " tokens · " + root.num(root.record.todayPrompts)
                + " prompts · " + root.num(root.record.todaySessions) + " sessions"
        }
        Heading { text: "Account allowance" }
        Info {
          visible: root.accounts.length === 0
          text: "Remaining usage unavailable. Explicitly export a fresh account snapshot from Hermes; no allowance is inferred from token history. Exports expire after 10 minutes."
        }
        Repeater {
          model: root.accounts
          Column {
            id: accountRow
            required property var modelData
            width: parent ? parent.width : 0
            spacing: 6
            readonly property var windows: root.visibleWindows(accountRow.modelData)
            Info { text: accountRow.modelData.provider + (accountRow.modelData.plan ? " · " + accountRow.modelData.plan : " · plan unavailable"); font.bold: true }
            Info { text: accountRow.modelData.accountSelection }
            Info { text: "Observed " + Math.max(0, Math.floor((root.now-accountRow.modelData.fetchedAt)/60)) + " min ago · " + accountRow.modelData.source }
            Info { visible: accountRow.modelData.remainingUsd !== undefined; text: "Reported account credit: " + root.money(accountRow.modelData.remainingUsd) }
            Info { visible: accountRow.modelData.provider === "nous"; text: root.accessText(accountRow.modelData); color: accountRow.modelData.accessStatus === "allowed" ? Color.foreground : Qt.lighter(Color.urgent, 1.5) }
            Info { visible: accountRow.windows.length === 0 && accountRow.modelData.remainingUsd === undefined; text: "Allowance unavailable for this observation (no current window)." }
            Repeater {
              model: accountRow.windows
              Column {
                id: windowRow
                required property var modelData
                width: parent ? parent.width : 0
                spacing: 5
                Info { text: windowRow.modelData.label + ": " + Number(windowRow.modelData.remainingPercent).toFixed(1) + "% remaining (bar shows used) · " + root.resetText(windowRow.modelData.resetAt) }
                ProgressBar {
                  id: meter
                  width: parent ? parent.width : 0
                  from: 0; to: 100; value: windowRow.modelData.usedPercent
                  background: Rectangle { implicitHeight: 7; radius: 3; color: Color.foreground; opacity: 0.15 }
                  contentItem: Item {
                    implicitHeight: 7
                    Rectangle { width: meter.visualPosition * parent.width; height: parent.height; radius: 3; color: meter.value >= 90 ? Color.urgent : Color.accent }
                  }
                }
              }
            }
          }
        }
        Heading { text: "Scanned activity" }
        Info { text: root.bucketText(root.totals) }
        Info { text: root.num(root.totals.reasoning) + " reasoning tokens (already included in output) · " + root.num(root.totals.cacheRead) + " cache-read tokens" }
        Info { text: "Estimated and actual are independent cumulative counters, never added into a bill. Latest row status is not priced-call coverage; default zero is not proof of free usage." }
        Heading { text: "Providers · local activity" }
        Info { visible: root.rows(root.details.providers).length === 0; text: "Provider activity unavailable" }
        Repeater {
          model: root.rows(root.details.providers)
          Info { required property var modelData; text: modelData.name + "\n" + root.bucketText(modelData.value) }
        }
        Heading { text: "Tasks · local activity" }
        Info { visible: root.rows(root.details.tasks).length === 0; text: "Task activity unavailable" }
        Repeater {
          model: root.rows(root.details.tasks)
          Info { required property var modelData; text: modelData.name + "\n" + root.bucketText(modelData.value) }
        }
        Info { text: "Daily token attribution is estimated from assistant-message activity or usage-row times. Historical provider mix is not your current plan or quota." }
      }
    }
    Shortcut { sequence: "Escape"; onActivated: root.requestClose() }
  }
}
