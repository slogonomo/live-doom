// SPDX-License-Identifier: 0BSD
import QtQuick

// Text set in the game's own STCFN font, read at runtime from the user's IWAD
// (see wadfont.py). Falls back to the mono UI font when no glyphs are cached.
Item {
  id: root

  property string text: ""
  property var font: null          // index from wadfont.py export, or null
  property int pixel: 2            // integer pixel scale keeps glyphs crisp
  property color fallbackColor: "#e5452f"
  property string fallbackFamily: "monospace"

  readonly property bool usable: !!(font && font.glyphs && Object.keys(font.glyphs).length > 0)
  readonly property string upper: String(text || "").toUpperCase()

  implicitWidth: usable ? glyphRow.implicitWidth : fallback.implicitWidth
  implicitHeight: usable ? (font.lineHeight || 8) * pixel : fallback.implicitHeight

  Row {
    id: glyphRow
    visible: root.usable
    spacing: 0          // the game advances by glyph width alone
    Repeater {
      model: root.usable ? root.upper.length : 0
      delegate: Item {
        readonly property string ch: root.upper.charAt(index)
        readonly property var glyph: root.font.glyphs[ch]
        width: glyph ? glyph.w * root.pixel : (root.font.spaceWidth || 4) * root.pixel
        height: (root.font.lineHeight || 8) * root.pixel
        Image {
          visible: !!parent.glyph
          source: parent.glyph ? "file://" + root.font.dir + "/" + parent.glyph.file : ""
          width: parent.glyph ? parent.glyph.w * root.pixel : 0
          height: parent.glyph ? parent.glyph.h * root.pixel : 0
          smooth: false
          mipmap: false
          y: parent.glyph ? Math.max(0, (root.font.lineHeight - parent.glyph.h)) * root.pixel : 0
        }
      }
    }
  }

  Text {
    textFormat: Text.PlainText   // labels, titles and reasons are data, never markup
    id: fallback
    visible: !root.usable
    text: root.upper
    color: root.fallbackColor
    font.family: root.fallbackFamily
    font.pixelSize: 8 * root.pixel + 2
    font.bold: true
    font.letterSpacing: root.pixel
  }
}
