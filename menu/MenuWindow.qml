// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// Layer-shell host for MenuCard: a fullscreen overlay on the right-clicked output
// with a scrim; mapped only while the menu is showing.
PanelWindow {
  id: panel
  property var root: null

  function screenNamed(name) {
    for (var i = 0; i < Quickshell.screens.length; i++)
      if (Quickshell.screens[i].name === name) return Quickshell.screens[i]
    return null
  }
  function chosenScreen() {
    var s = root ? panel.screenNamed(root.targetOutput) : null
    if (s) return s
    var m = Hyprland.focusedMonitor
    return m ? panel.screenNamed(String(m.name || "")) : null
  }

  // Mapped fresh on each open (fullscreen on the chosen output, exclusive
  // keyboard) and unmapped on close. Parking a 1x1 surface and growing it would
  // need a Hyprland no_anim layer rule for "doom-menu" (Omarchy only ships one for
  // its own omarchy-* overlays); a fresh map gets the stock quick fade instead,
  // and the card fades itself in from transparent, which hides a first frame.
  property bool shown: !!root && root.surfaceShown
  property var target: null
  property bool mapped: false
  onShownChanged: {
    if (shown) {
      target = chosenScreen() || target    // pick the output before mapping
      mapped = true
    } else {
      mapped = false
    }
  }
  // Re-summoned from another output while already showing: follow it there.
  Connections {
    target: panel.root
    ignoreUnknownSignals: true
    function onTargetOutputChanged() { if (panel.shown) panel.target = panel.chosenScreen() || panel.target }
  }

  visible: mapped
  screen: target
  color: "transparent"
  anchors { top: true; left: true; bottom: true; right: true }
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "doom-menu"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

  readonly property bool compact: width >= 1280 && height >= 800

  // Scrim: the viewer blurs and holds the game behind us while the lease lives.
  Rectangle {
    anchors.fill: parent
    color: "#0a0807"
    opacity: 0.38
  }
  MouseArea {
    anchors.fill: parent
    onClicked: if (root) root.dismiss()
  }

  MenuCard {
    id: card
    m: root
    compact: panel.compact
    shown: panel.shown
    availW: panel.width
    availH: panel.height
    width: compact ? Math.min(800, panel.width - 160) : panel.width
    height: compact ? Math.min(implicitHeight, panel.height - 120) : panel.height
    anchors.centerIn: parent
  }
}
