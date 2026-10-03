// SPDX-License-Identifier: 0BSD
import QtQuick

// The visual console card of the Live Doom menu. All state and behaviour live in
// Menu.qml (passed in as `m`); this file is pure view so it can also be rendered
// offscreen for design snapshots and smoke tests.
Item {
  id: card
  property var m: null
  property bool compact: true
  property bool framed: compact      // the hellfire frame; a window host has its own border
  property bool shown: true
  property real availW: width
  property real availH: height
  function focusKeys() { keys.forceActiveFocus() }
  // Register with the menu whenever `m` arrives: in production MenuWindow is
  // loaded first and only then given its root, so `m` is still null when this
  // card completes. Take focus if the surface is already showing.
  function register() {
    if (!m) return
    m.cardItem = card
    if (m.surfaceShown) Qt.callLater(card.focusKeys)
  }
  onMChanged: register()
  Component.onCompleted: register()
  onShownChanged: if (shown) Qt.callLater(card.focusKeys)
  implicitWidth: 800
  implicitHeight: column.implicitHeight + footer.height + 84

  // Pointer-driven selection never scrolls the list (a row under the pointer is
  // already visible), and a row only takes the cursor when the pointer really
  // moved, not when content scrolled underneath a resting pointer.
  property bool pointerSelect: false
  property point lastPointer: Qt.point(-1, -1)
  // True while the list is scrolling and for 250 ms after. A notifying property
  // (cleared by a timer), not a Date.now() test, so bindings such as a
  // thermometer's wheelEnabled re-evaluate when the cooldown ends.
  property bool scrolling: false
  Timer { id: scrollQuiet; interval: 250; onTriggered: card.scrolling = false }
  function recentlyScrolled() { return card.scrolling }
  function pointerMoved(area) {
    var p = area.mapToItem(null, area.mouseX, area.mouseY)
    var same = p.x === card.lastPointer.x && p.y === card.lastPointer.y
    card.lastPointer = p
    // a resting pointer gets synthetic moves while content scrolls beneath it
    return !same && !card.recentlyScrolled()
  }
  // value widgets take the wheel only on the row the user is on, never mid-scroll
  function wheelForRow(index) {
    if (!flick.interactive) return true
    return m.cursor === index && !card.recentlyScrolled()
  }
  function pointTo(index, sub) {
    card.pointerSelect = true
    m.cursor = index
    m.sub = sub
    card.pointerSelect = false
  }
  opacity: card.shown ? 1 : 0
  transform: Translate { y: card.shown ? 0 : 12; Behavior on y { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } } }
  Behavior on opacity { NumberAnimation { duration: 140 } }

  // swallow clicks on the card so only the scrim dismisses
  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.AllButtons
    onClicked: function(mouse) { mouse.accepted = true }
    onWheel: function(wheel) { wheel.accepted = false }
  }

  // hellfire frame
  Rectangle {
    anchors.fill: parent
    radius: card.framed ? 4 : 0
    gradient: Gradient {
      orientation: Gradient.Horizontal
      GradientStop { position: 0.0; color: m.cHell }
      GradientStop { position: 0.55; color: m.cEmber }
      GradientStop { position: 1.0; color: m.cAmber }
    }
    visible: card.framed
  }
  Rectangle {
    id: face
    anchors.fill: parent
    anchors.margins: card.framed ? 2 : 0
    radius: card.framed ? 3 : 0
    color: m.cPanel
    opacity: 0.97
  }
  Canvas {     // console scanlines
    anchors.fill: face
    opacity: 0.55
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onPaint: {
      var ctx = getContext("2d")
      ctx.clearRect(0, 0, width, height)
      ctx.fillStyle = "rgba(227, 211, 184, 0.035)"
      for (var y = 0; y < height; y += 3) ctx.fillRect(0, y, width, 1)
    }
  }
  // corner brackets
  Repeater {
    model: card.framed ? 4 : 0
    Item {
      width: 14; height: 14
      x: (index % 2 === 0) ? 10 : card.width - 24
      y: (index < 2) ? 10 : card.height - 24
      Rectangle { width: 14; height: 2; color: m.cBone; opacity: 0.35; y: index < 2 ? 0 : 12 }
      Rectangle { width: 2; height: 14; color: m.cBone; opacity: 0.35; x: index % 2 === 0 ? 0 : 12 }
    }
  }

  Flickable {
    id: flick
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.bottom: footer.top
    anchors.margins: 36
    anchors.bottomMargin: 10
    contentWidth: width
    contentHeight: column.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height
    onContentYChanged: { card.scrolling = true; scrollQuiet.restart() }

    function reveal(item) {
      if (!item) return
      var top = item.mapToItem(column, 0, 0).y
      var bottom = top + item.height
      if (top < contentY) contentY = Math.max(0, top - 40)
      else if (bottom > contentY + height) contentY = Math.min(contentHeight - height, bottom - height + 40)
    }

    Column {
      id: column
      width: flick.width
      spacing: 0

      // header: the IWAD's own logo + live state
      Item {
        width: parent.width
        height: Math.max(logo.height, headText.implicitHeight) + 18
        Image {
          id: logo
          readonly property var patch: m.fontIndex && m.fontIndex.patches ? m.fontIndex.patches.M_DOOM : null
          source: patch ? "file://" + m.fontIndex.dir + "/" + patch.file : ""
          visible: !!patch
          smooth: false
          width: patch ? patch.w * (card.compact ? 2 : 1) : 0
          height: patch ? patch.h * (card.compact ? 2 : 1) : 0
        }
        Column {
          id: headText
          anchors.right: parent.right
          anchors.bottom: logo.visible ? logo.bottom : undefined
          spacing: 6
          Text {
            textFormat: Text.PlainText   // labels, titles and reasons are data, never markup
            anchors.right: parent.right
            text: "LIVE DOOM"
            color: m.cDim
            font.family: m.mono
            font.pixelSize: 12
            font.letterSpacing: 4
          }
          Text {
            textFormat: Text.PlainText
            anchors.right: parent.right
            text: m.headLine(m.settings).toUpperCase()
            color: m.cEmber
            font.family: m.mono
            font.pixelSize: 14
            font.bold: true
            font.letterSpacing: 2
          }
        }
      }
      Rectangle {
        width: parent.width
        height: 2
        gradient: Gradient {
          orientation: Gradient.Horizontal
          GradientStop { position: 0.0; color: m.cHell }
          GradientStop { position: 0.6; color: m.cEmber }
          GradientStop { position: 1.0; color: "transparent" }
        }
      }
      Item { width: 1; height: 10 }

      Repeater {
        model: m.rows
        delegate: Item {
          id: rowItem
          required property var modelData
          required property int index
          readonly property var row: modelData
          readonly property bool selected: m.cursor === index
          // the guide's own rows carry their state (`on`); settings rows read it by key
          readonly property var value: row.on !== undefined ? row.on
                                     : (m.settings ? m.getv(m.settings, row.key || "") : undefined)
          width: column.width
          height: row.kind === "section" ? 40 : (row.kind === "note" ? note.implicitHeight + 10
                  : (row.kind === "banner" ? 64 : (row.kind === "nav" ? 58 : (row.kind === "link" ? 30 : 32))))
          onSelectedChanged: if (selected && !card.pointerSelect) flick.reveal(rowItem)

          // section heading
          Item {
            visible: rowItem.row.kind === "section"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 8
            height: sectionLabel.implicitHeight
            Text {
              textFormat: Text.PlainText
              id: sectionLabel
              text: String(rowItem.row.label || "").toUpperCase()
              color: m.cEmber
              font.family: m.mono
              font.pixelSize: 12
              font.bold: true
              font.letterSpacing: 3
            }
            Rectangle {
              anchors.left: sectionLabel.right
              anchors.leftMargin: 12
              anchors.right: parent.right
              anchors.verticalCenter: sectionLabel.verticalCenter
              height: 1
              color: m.cRust
            }
          }

          // Steam package logo, read from the local Steam cache at runtime (never copied);
          // the package title in IWAD lettering if the image cannot be read
          Image {
            id: bannerImage
            visible: rowItem.row.kind === "banner" && status === Image.Ready
            x: 48
            anchors.verticalCenter: parent.verticalCenter
            readonly property var box: rowItem.row.kind === "banner" ? m.artBoxes[rowItem.row.image] : undefined
            sourceClipRect: box ? Qt.rect(box.x, box.y, box.w, box.h) : Qt.rect(0, 0, 0, 0)
            height: 46
            width: Math.min(column.width - 96, implicitWidth * height / Math.max(1, implicitHeight))
            fillMode: Image.PreserveAspectFit
            horizontalAlignment: Image.AlignLeft
            asynchronous: true
            smooth: true
            mipmap: true
            source: rowItem.row.kind === "banner" && rowItem.row.image ? "file://" + encodeURI(rowItem.row.image) : ""
          }
          DoomText {
            visible: rowItem.row.kind === "banner" && bannerImage.status !== Image.Ready
            x: 48
            anchors.verticalCenter: parent.verticalCenter
            text: rowItem.row.label || ""
            font: m.fontIndex
            pixel: 2
            fallbackFamily: m.mono
          }

          // explanatory note
          Text {
            textFormat: Text.PlainText
            id: note
            visible: rowItem.row.kind === "note"
            x: 44
            width: column.width - 44
            wrapMode: Text.WordWrap
            text: rowItem.row.text || ""
            color: m.cDim
            font.family: m.mono
            font.pixelSize: 12
          }

          // selection wash + skull cursor
          Rectangle {
            visible: rowItem.selected && ["section", "note", "nav", "link"].indexOf(rowItem.row.kind) === -1
            anchors.fill: parent
            anchors.leftMargin: 36
            color: m.cRust
            opacity: 0.35
            radius: 2
          }
          Image {
            readonly property var p: m.fontIndex && m.fontIndex.patches
              ? m.fontIndex.patches[m.skullFrame ? "M_SKULL2" : "M_SKULL1"] : null
            visible: rowItem.selected && !!p
            source: p ? "file://" + m.fontIndex.dir + "/" + p.file : ""
            smooth: false
            width: p ? p.w * 1.5 : 0
            height: p ? p.h * 1.5 : 0
            anchors.verticalCenter: parent.verticalCenter
            x: 0
          }
          Text {
            textFormat: Text.PlainText
            visible: rowItem.selected && !(m.fontIndex && m.fontIndex.patches && m.fontIndex.patches.M_SKULL1)
            text: "▶"
            color: m.cHell
            font.pixelSize: 16
            anchors.verticalCenter: parent.verticalCenter
            x: 8
          }

          // label
          DoomText {
            visible: ["toggle", "choice", "slider", "action", "gameoff", "option", "fact"].indexOf(rowItem.row.kind) !== -1
            x: 48
            anchors.verticalCenter: parent.verticalCenter
            text: rowItem.row.label || ""
            font: m.fontIndex
            pixel: 2
            opacity: rowItem.row.kind === "gameoff" ? 0.3 : (rowItem.selected ? 1.0 : 0.8)
            fallbackFamily: m.mono
          }
          // the guide's quiet way out: small, never where the cursor starts
          Text {
            textFormat: Text.PlainText
            visible: rowItem.row.kind === "link"
            x: 48
            anchors.verticalCenter: parent.verticalCenter
            text: rowItem.row.label || ""
            color: rowItem.selected ? m.cBone : m.cMuted
            font.family: m.mono
            font.pixelSize: 12
            font.underline: rowItem.selected
          }
          // detail (mono) for IWAD and PWAD rows
          Text {
            textFormat: Text.PlainText
            visible: !!rowItem.row.detail
            x: rowItem.row.kind === "pwad" ? 84 : 140
            width: column.width - x - 140
            elide: Text.ElideMiddle
            anchors.verticalCenter: parent.verticalCenter
            text: rowItem.row.detail || ""
            color: rowItem.selected ? m.cBone : m.cDim
            font.family: m.mono
            font.pixelSize: 13
          }
          DoomText {
            visible: rowItem.row.kind === "pwad"
            x: 52
            anchors.verticalCenter: parent.verticalCenter
            text: rowItem.row.label || ""
            font: m.fontIndex
            pixel: 2
            opacity: 0.6
            fallbackFamily: m.mono
          }

          // value column; above the row's MouseArea (z) so the slider and the
          // inline button get their own clicks, while plain text lets clicks fall through
          Row {
            id: valueRow
            z: 2
            anchors.right: parent.right
            anchors.rightMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            spacing: 12

            DoomText {   // toggle
              visible: rowItem.row.kind === "toggle"
              anchors.verticalCenter: parent.verticalCenter
              text: rowItem.value ? "On" : "Off"
              font: m.fontIndex
              pixel: 2
              opacity: rowItem.value ? 1.0 : 0.45
              fallbackFamily: m.mono
              // with an inline button, mark which of the two Enter will act on
              Rectangle {
                visible: !!rowItem.row.extra && rowItem.selected && m.sub === 0
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.bottom
                anchors.topMargin: 3
                height: 2
                color: m.cEmber
              }
            }
            Rectangle {  // inline button (Live screensaver: [x] [PREVIEW])
              visible: !!rowItem.row.extra
              readonly property bool focused: rowItem.selected && m.sub === 1
              width: extraLabel.implicitWidth + 20
              height: extraLabel.implicitHeight + 10
              anchors.verticalCenter: parent.verticalCenter
              color: focused ? m.cRust : "transparent"
              border.width: 1
              border.color: focused ? m.cEmber : m.cRust
              radius: 2
              DoomText {
                id: extraLabel
                anchors.centerIn: parent
                text: rowItem.row.extra ? rowItem.row.extra.label : ""
                font: m.fontIndex
                pixel: 2
                opacity: parent.focused ? 1.0 : 0.6
                fallbackFamily: m.mono
              }
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onPositionChanged: if (card.pointerMoved(this)) card.pointTo(rowItem.index, 1)
                onClicked: { card.pointTo(rowItem.index, 1); m.activate(rowItem.row) }
              }
            }
            DoomText {   // choice
              visible: rowItem.row.kind === "choice"
              text: {
                var label = ""
                var opts = rowItem.row.options || []
                for (var i = 0; i < opts.length; i++) if (opts[i][0] === rowItem.value) label = opts[i][1]
                return rowItem.selected ? "< " + label + " >" : label
              }
              font: m.fontIndex
              pixel: 2
              fallbackFamily: m.mono
              // wheel over the value cycles it, one step per notch
              MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.NoButton
                property real acc: 0
                onWheel: function(wheel) {
                  if (Math.abs(wheel.angleDelta.y) <= Math.abs(wheel.angleDelta.x) || !card.wheelForRow(rowItem.index)) { acc = 0; wheel.accepted = false; return }
                  if (acc !== 0 && (acc > 0) !== (wheel.angleDelta.y > 0)) acc = 0   // direction changed
                  acc += wheel.angleDelta.y
                  while (Math.abs(acc) >= 120) { m.adjust(rowItem.row, acc > 0 ? 1 : -1); acc -= acc > 0 ? 120 : -120 }
                }
              }
            }
            Thermo {     // slider
              visible: rowItem.row.kind === "slider"
              anchors.verticalCenter: parent.verticalCenter
              font: m.fontIndex
              pixel: 2
              cells: 12
              value: rowItem.row.kind === "slider" ? (Number(rowItem.value) - rowItem.row.min) / (rowItem.row.max - rowItem.row.min) : 0
              onPicked: function(f) { card.pointTo(rowItem.index, 0); m.sliderPick(rowItem.row, f) }
              wheelEnabled: card.wheelForRow(rowItem.index)
              onStepped: function(dir) { card.pointTo(rowItem.index, 0); m.adjust(rowItem.row, dir) }
            }
            Item {       // slider value, right-aligned so it lines up with toggle values
              visible: rowItem.row.kind === "slider"
              width: 72
              height: sliderValue.implicitHeight
              anchors.verticalCenter: parent.verticalCenter
              DoomText {
                id: sliderValue
                anchors.right: parent.right
                text: rowItem.row.kind === "slider" ? Number(rowItem.value).toFixed(rowItem.row.digits) + rowItem.row.suffix : ""
                font: m.fontIndex
                pixel: 2
                fallbackFamily: m.mono
              }
            }
            DoomText {   // action (and an unsupported game's N/A), a guide option's own value, a summary fact
              visible: rowItem.row.kind === "action" || rowItem.row.kind === "gameoff" || rowItem.row.kind === "fact"
                       || (rowItem.row.kind === "option" && !!rowItem.row.value)
              text: rowItem.row.value || ""
              font: m.fontIndex
              pixel: 2
              opacity: rowItem.row.kind === "gameoff" ? 0.25
                     : (rowItem.selected || rowItem.row.kind === "fact" || rowItem.row.value === "Playing" ? 1.0 : 0.55)
              fallbackFamily: m.mono
            }
            Rectangle {  // a guide option: one of a set, filled when chosen
              visible: rowItem.row.kind === "option" && !rowItem.row.value
              width: 18; height: 18
              anchors.verticalCenter: parent.verticalCenter
              color: "transparent"
              border.width: 2
              border.color: rowItem.value ? m.cEmber : (rowItem.selected ? m.cDim : m.cRust)
              radius: 2
              Rectangle {
                anchors.fill: parent
                anchors.margins: 4
                color: m.cEmber
                visible: !!rowItem.value
              }
            }
            Text {       // pwad hints
              textFormat: Text.PlainText
              visible: rowItem.row.kind === "pwad" && rowItem.selected
              text: "←→ order   del remove"
              color: m.cMuted
              font.family: m.mono
              font.pixelSize: 11
              anchors.verticalCenter: parent.verticalCenter
            }
          }

          // the guide's Back / Next: Next on the right is the default (sub 0), Back on the left (sub 1)
          Repeater {
            model: rowItem.row.kind === "nav" ? (rowItem.row.back ? [1, 0] : [0]) : []
            delegate: Rectangle {
              required property int modelData
              readonly property bool isBack: modelData === 1
              readonly property bool focused: rowItem.selected && m.sub === modelData
              z: 3
              x: isBack ? 48 : rowItem.width - width - 10
              anchors.verticalCenter: parent.verticalCenter
              width: Math.max(120, navLabel.implicitWidth + 36)
              height: navLabel.implicitHeight + 16
              radius: 2
              color: focused ? m.cRust : (isBack ? "transparent" : Qt.rgba(0.29, 0.14, 0.10, 0.45))
              border.width: isBack ? 1 : 2
              border.color: focused || !isBack ? m.cEmber : m.cRust
              DoomText {
                id: navLabel
                anchors.centerIn: parent
                text: parent.isBack ? "Back" : (rowItem.row.next || "Next")
                font: m.fontIndex
                pixel: 2
                opacity: parent.focused || !parent.isBack ? 1.0 : 0.5
                fallbackFamily: m.mono
              }
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onPositionChanged: if (card.pointerMoved(this)) card.pointTo(rowItem.index, parent.modelData)
                onClicked: { card.pointTo(rowItem.index, parent.modelData); m.activate(rowItem.row) }
              }
            }
          }

          MouseArea {
            anchors.fill: parent
            visible: m.selectable(rowItem.index) && rowItem.row.kind !== "nav"   // nav: only its buttons
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            cursorShape: Qt.PointingHandCursor
            onPositionChanged: if (card.pointerMoved(this)) card.pointTo(rowItem.index, 0)
            onClicked: function(mouse) {
              card.pointTo(rowItem.index, 0)
              if (rowItem.row.kind === "pwad" && mouse.button === Qt.RightButton) m.removePwad(rowItem.row.index)
              else if (rowItem.row.kind !== "slider") m.activate(rowItem.row)   // sliders: thermometer, keys, wheel
            }
            onWheel: function(wheel) { wheel.accepted = false }   // the list scrolls; values change over their widgets
          }
        }
      }

    }
  }

  // console footer: last action + key hints, pinned so errors are never scrolled away
  Column {
    id: footer
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    anchors.leftMargin: 36
    anchors.rightMargin: 36
    anchors.bottomMargin: 30
    spacing: 0

    Rectangle { width: parent.width; height: 1; color: m.cRust }
    Item { width: 1; height: 12 }
    Item {
      width: parent.width
      height: 22
      // status line: shown only when there is something to say (no idle prompt or cursor,
      // which read as a terminal waiting for typing)
      Row {
        spacing: 6
        visible: m.statusLine !== ""
        anchors.verticalCenter: parent.verticalCenter
        Text {
          textFormat: Text.PlainText
          text: m.statusError ? "!" : ">"
          color: m.statusError ? m.cHell : m.cEmber
          font.family: m.mono
          font.pixelSize: 13
          font.bold: true
        }
        Text {
          textFormat: Text.PlainText
          text: m.statusLine
          color: m.statusError ? m.cHell : m.cBone
          font.family: m.mono
          font.pixelSize: 13
          maximumLineCount: 1
          wrapMode: Text.NoWrap
          elide: Text.ElideRight
          width: Math.min(implicitWidth, footer.width - 360)
        }
      }
      Text {
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: m.hints
        color: m.cMuted
        font.family: m.mono
        font.pixelSize: 11
      }
    }
  }

  // keyboard (exclusive while shown)
  Item {
    id: keys
    focus: true
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function(event) { if (card.handleKey(event.key, event.modifiers)) event.accepted = true }
  }

  // Returns true when the key was handled (kept separate so tests can drive it).
  function handleKey(key, modifiers) {
    var row = m.rows[m.cursor]
    var shift = modifiers & Qt.ShiftModifier
    if (key === Qt.Key_Escape || key === Qt.Key_F12) {
      m.dismiss()
    } else if (key === Qt.Key_Backtab || (key === Qt.Key_Tab && shift)) {
      m.move(-1)                                   // Shift+Tab is navigation, never a reorder
    } else if (key === Qt.Key_Tab) {
      m.move(1)
    } else if (key === Qt.Key_Down || key === Qt.Key_J) {
      if (shift && row && row.kind === "pwad") m.movePwad(row.index, 1); else m.move(1)
    } else if (key === Qt.Key_Up || key === Qt.Key_K) {
      if (shift && row && row.kind === "pwad") m.movePwad(row.index, -1); else m.move(-1)
    } else if (key === Qt.Key_Left || key === Qt.Key_H) {
      m.adjust(row, -1)
    } else if (key === Qt.Key_Right || key === Qt.Key_L) {
      m.adjust(row, 1)
    } else if (key === Qt.Key_Return || key === Qt.Key_Enter || key === Qt.Key_Space) {
      m.activate(row)
    } else if ((key === Qt.Key_Delete || key === Qt.Key_X || key === Qt.Key_Backspace) && row && row.kind === "pwad") {
      m.removePwad(row.index)
    } else {
      return false
    }
    return true
  }
}
