// SPDX-License-Identifier: 0BSD
import QtQuick

// The game's thermometer slider (M_THERML / M_THERMM / M_THERMR / M_THERMO),
// drawn from the user's IWAD. Falls back to plain bars when no patches exist.
Item {
  objectName: "thermo"
  id: root

  property var font: null
  property int pixel: 2
  property int cells: 16
  property real value: 0          // 0..1
  property color accent: "#f26a1b"
  property color track: "#3a2a22"
  signal picked(real fraction)
  signal stepped(int dir)          // wheel: one step per notch
  property bool wheelEnabled: true  // false: pass the wheel on (the list scrolls)

  readonly property var p: font && font.patches ? font.patches : ({})
  readonly property bool usable: !!(p.M_THERML && p.M_THERMM && p.M_THERMR && p.M_THERMO)
  readonly property int cellW: usable ? p.M_THERMM.w * pixel : 8 * pixel
  readonly property int capW: usable ? p.M_THERML.w * pixel : 4 * pixel
  readonly property int barH: usable ? p.M_THERMM.h * pixel : 10 * pixel

  implicitWidth: capW * 2 + cellW * cells
  implicitHeight: barH

  function src(name) { return "file://" + font.dir + "/" + p[name].file }

  // The thumb sits on cell round(value * (cells - 1)); a pick is the exact
  // inverse, so pressing on the thumb (how a drag starts) never moves it.
  readonly property int thumbW: usable ? p.M_THERMO.w * pixel : 0
  readonly property int dot: Math.round(Math.max(0, Math.min(1, value)) * (cells - 1))
  function pickAt(x) {
    if (!usable) { picked(Math.max(0, Math.min(1, x / width))); return }
    var cell = Math.round((x - capW - thumbW / 2) / cellW)
    cell = Math.max(0, Math.min(cells - 1, cell))
    if (cell !== dot) picked(cell / (cells - 1))
  }

  Row {
    visible: root.usable
    Image { source: root.usable ? root.src("M_THERML") : ""; width: root.capW; height: root.barH; smooth: false }
    Repeater {
      model: root.usable ? root.cells : 0
      Image { source: root.src("M_THERMM"); width: root.cellW; height: root.barH; smooth: false }
    }
    Image { source: root.usable ? root.src("M_THERMR") : ""; width: root.capW; height: root.barH; smooth: false }
  }
  Image {
    visible: root.usable
    source: root.usable ? root.src("M_THERMO") : ""
    smooth: false
    width: root.usable ? root.p.M_THERMO.w * root.pixel : 0
    height: root.usable ? root.p.M_THERMO.h * root.pixel : 0
    x: root.capW + Math.round(Math.max(0, Math.min(1, root.value)) * (root.cells - 1)) * root.cellW
    y: 0
  }

  Rectangle {
    visible: !root.usable
    anchors.fill: parent
    color: root.track
    radius: 2
    Rectangle {
      width: parent.width * Math.max(0, Math.min(1, root.value))
      height: parent.height
      color: root.accent
      radius: 2
    }
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    property real acc: 0
    onPressed: function(mouse) { root.pickAt(mouse.x) }
    onPositionChanged: function(mouse) { if (pressed) root.pickAt(mouse.x) }
    onWheel: function(wheel) {
      if (Math.abs(wheel.angleDelta.y) <= Math.abs(wheel.angleDelta.x) || !root.wheelEnabled) { acc = 0; wheel.accepted = false; return }
      if (acc !== 0 && (acc > 0) !== (wheel.angleDelta.y > 0)) acc = 0   // direction changed
      acc += wheel.angleDelta.y
      while (Math.abs(acc) >= 120) { root.stepped(acc > 0 ? 1 : -1); acc -= acc > 0 ? 120 : -120 }
    }
  }
}
