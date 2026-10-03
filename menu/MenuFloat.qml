// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Hyprland

// Window host for MenuCard, used when the menu is opened from the Apps launcher or by the
// first-run guide: a real toplevel window, so it behaves like any app (Super+W closes it,
// it ends Omarchy's "Launching…" toast, and it never sits over other windows unseen).
// Hyprland floats and centres a window whose minimum and maximum sizes are equal, so the
// size is fixed before each map and the card scrolls inside. A right-click on the live
// background keeps the overlay (MenuWindow.qml), which belongs to the desktop.
FloatingWindow {
  id: win
  property var root: null

  readonly property bool shown: !!root && root.surfaceShown
  property bool mapped: false
  property int fixedH: 860
  function pickHeight() {
    var m = Hyprland.focusedMonitor, h = 0
    for (var i = 0; i < Quickshell.screens.length; i++)
      if (m && Quickshell.screens[i].name === String(m.name || "")) h = Quickshell.screens[i].height
    win.fixedH = h > 0 ? Math.max(520, Math.min(900, h - 160)) : 860
  }
  onShownChanged: {
    if (shown) {
      pickHeight()      // the size decides floating, so it is set before the window maps
      mapped = true
    } else {
      mapped = false
    }
  }
  // Closed by the compositor (Super+W, or the window's close) while the menu wanted it up:
  // that is the user closing the menu. An unmap the menu asked for is not.
  onVisibleChanged: if (!visible && root && root.opened && root.surfaceShown) root.dismiss()
  onClosed: if (root && root.opened && root.surfaceShown) root.dismiss()

  title: "Live Doom"
  visible: mapped
  color: root ? root.cPanel : "#0d0a09"
  implicitWidth: 800
  implicitHeight: fixedH
  minimumSize: Qt.size(800, fixedH)
  maximumSize: Qt.size(800, fixedH)

  MenuCard {
    m: win.root
    anchors.fill: parent
    compact: true          // the IWAD logo at 2x, as in the overlay card
    framed: false          // the window has Hyprland's own border
    shown: win.shown
    availW: win.width
    availH: win.height
  }
}
