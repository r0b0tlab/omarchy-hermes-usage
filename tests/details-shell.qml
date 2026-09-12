import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
  id: fixture
  property int hides: 0
  property int refreshes: 0
  QtObject { id: host; function hide(id) { fixture.hides++; panel.close() } }
  QtObject { id: localService; property bool busy: false; function refresh() { fixture.refreshes++ } }
  Details { id: panel; shell: host; service: localService }
  IpcHandler {
    target: "usage-fixture"
    function ping(): string { return "ready" }
    function open(): string { panel.open("{}"); return "opened" }
    function hide(): string { panel.close(); return "hidden" }
    function refresh(): string { panel.refreshLocal(); return String(fixture.refreshes) }
    function busy(): string { localService.busy = true; panel.refreshLocal(); localService.busy = false; return String(fixture.refreshes) }
    function status(): string {
      return JSON.stringify({updatedAt:panel.record.updatedAt,opened:panel.opened,hides:fixture.hides,refreshes:fixture.refreshes,
        accounts:panel.accounts.length,windows:panel.accounts.map(function(a){return panel.visibleWindows(a).length}),
        emptyMoney:panel.money(null),totals:panel.bucketText(panel.totals)})
    }
    function expiry(): string { panel.now += 601; return String(panel.accounts.length) }
  }
}
