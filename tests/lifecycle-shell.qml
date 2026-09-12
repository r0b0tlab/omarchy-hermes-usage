import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
    id: root
    property var service: null
    function start() {
        if (!service) {
            service = Qt.createComponent("Service.qml").createObject(root)
            if (!service) throw new Error("service creation failed")
            service.refresh()
        }
    }
    function stop() { if (service) { service.destroy(); service = null } }
    IpcHandler {
        target: "fixture"
        function ping(): string { return "ready" }
        function start(): string { root.start(); return "started" }
        function stop(): string { root.stop(); return "stopped" }
        function immediate(): string { root.start(); root.stop(); return "destroyed" }
        function reload(): string { Quickshell.reload(true); return "reload" }
        function quit(): string { Qt.quit(); return "quit" }
    }
}
