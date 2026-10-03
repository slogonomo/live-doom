// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// Live Doom companion service (kind "service" of io.github.slogonomo.live-doom).
//
// Omarchy's own idle service keeps the clock: lock timing, stay-awake, lock guards and
// wake stay first-party and untouched. While the Live Doom screensaver is enabled this
// service only swaps which screensaver shows:
//   - it claims Omarchy's screensaver-off toggle (`doomctl saver-claim`) so the stock
//     terminal saver stays down. The claim never replaces a toggle the user set ("blocked":
//     the user wants no screensaver, so Live Doom's stays off too). It is released when the
//     Doom saver is switched off and, inline, when this plugin is disabled or removed;
//   - an IdleMonitor at the stock screensaver timeout asks the stock service whether its
//     cycle reached the screensaver step, and only then starts the Doom saver.
// It also starts the Live Doom user service when the plugin is enabled.
// Every command runs by absolute path with a closed environment and a deadline.
Item {
  id: root

  // Injected by the Omarchy shell plugin host.
  property var shell: null
  property var manifest: null

  readonly property string serviceDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, ""))
  readonly property string pluginRoot: root.serviceDir.replace(/[^\/]+\/$/, "")
  // Fixed absolute commands; no executable is ever taken from the environment. The offscreen
  // test harness replaces them (and stands in for the compositor's idle signal) by passing
  // initial properties when it loads this component.
  property string doomctl: root.pluginRoot + "scripts/doomctl"
  property string omarchyShell: "/usr/bin/omarchy-shell"
  property string systemctl: "/usr/bin/systemctl"
  property var extraEnv: ({})            // harness only: the fakes' own settings
  // Live Doom's settings file, watched so a change made anywhere (menu, doomctl, the
  // backend itself) reaches the claim within a second. The harness points it at its fixture.
  property string configPath: (Quickshell.env("XDG_CONFIG_HOME") || ((Quickshell.env("HOME") || "") + "/.config"))
                              + "/live-doom/config.json"
  property bool testIdle: false
  readonly property bool isIdle: idleMonitor.isIdle || root.testIdle
  readonly property string unit: "doom-desktop.service"
  readonly property var env: {
    var keep = ["HOME", "USER", "LOGNAME", "LANG", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_STATE_HOME", "XDG_CACHE_HOME", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
                "DBUS_SESSION_BUS_ADDRESS"]
    var e = { "PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy" }   // omarchy-shell needs it
    for (var i = 0; i < keep.length; i++) {
      var v = Quickshell.env(keep[i])
      if (v) e[keep[i]] = String(v)
    }
    for (var k in root.extraEnv) e[k] = String(root.extraEnv[k])
    return e
  }

  // ---------------------------------------------------------------- state
  property bool saverEnabled: false        // settings.screensaver.enabled
  property bool settingsKnown: false
  property var claim: ({})                 // {owned, blocked, revoked, token, toggle, marker}
  property bool claimTried: false          // one claim per enabled period, never every poll
  property bool syncAgain: false           // a sync arrived while doomctl was busy: run it after
  property int saverSeconds: 150           // the stock idle.screensaver timeout
  property bool stockEnabled: false        // stock idle on and not stay-awake
  property string lastEvent: "starting"
  readonly property bool armed: root.saverEnabled && root.claim.owned === true && root.claim.blocked !== true
                                && root.claim.revoked !== true && root.stockEnabled

  function log(event) {
    root.lastEvent = event
    console.log("live-doom service: " + event)
  }

  // ---------------------------------------------------------------- commands
  // One deadline-guarded process per purpose; output is JSON from doomctl or omarchy-shell.
  component Command: Process {
    id: cmd
    property var done: null
    property bool hit: false
    environment: root.env
    clearEnvironment: true
    stdout: StdioCollector { id: out }
    stderr: StdioCollector { id: err }
    function start(argv, then) {
      if (cmd.running) return false
      cmd.done = then || null
      cmd.hit = false
      cmd.command = argv
      cmd.running = true
      deadline.restart()
      return true
    }
    onExited: function(code) {
      deadline.stop()
      var f = cmd.done
      cmd.done = null
      if (f) {
        try { f(cmd.hit ? -1 : code, out.text || "", cmd.hit ? "timed out" : (err.text || "")) }
        catch (e) { console.warn("live-doom service:", e) }
      }
    }
    property Timer deadline: Timer {
      interval: 15000
      onTriggered: if (cmd.running) { cmd.hit = true; cmd.running = false }
    }
  }
  Command { id: ctlProc }      // doomctl settings / saver-claim / saver-release
  Command { id: statusProc }   // omarchy-shell idle status
  Command { id: launchProc }   // doomctl screensaver
  Command { id: unitProc }     // systemctl --user start

  function parse(text) {
    try { return JSON.parse(text) } catch (e) { return null }
  }

  // ---------------------------------------------------------------- claim
  function syncSettings() {
    var started = ctlProc.start([root.doomctl, "settings"], function(code, text) {
      var s = code === 0 ? root.parse(text) : null
      if (!s || !s.screensaver) return
      var enabled = s.screensaver.enabled === true
      var wasOwned = root.claim.owned === true
      if (s.screensaver.claim) root.claim = s.screensaver.claim
      // A claim we held that comes back plainly free (the game service restarted and released
      // it) may be taken again; a user's toggle (blocked) or revocation never is.
      if (!enabled || (wasOwned && root.claim.owned !== true && root.claim.blocked !== true
                       && root.claim.revoked !== true)) root.claimTried = false
      root.saverEnabled = enabled
      root.settingsKnown = true
      if (enabled && !root.claimTried && root.claim.owned !== true && root.claim.blocked !== true
          && root.claim.revoked !== true)
        Qt.callLater(root.claimToggle)
      else if (!enabled && root.claim.owned === true)
        Qt.callLater(root.releaseToggle)
    })
    if (!started) root.syncAgain = true
  }
  function claimToggle() {
    root.claimTried = true
    var started = ctlProc.start([root.doomctl, "saver-claim"], function(code, text, error) {
      var c = code === 0 ? root.parse(text) : null
      if (c) root.claim = c
      root.log(c ? (c.owned ? "claimed the screensaver toggle" : (c.blocked ? "screensaver toggle is the user's: standing down" : "claim refused"))
                 : "saver-claim failed: " + String(error).trim())
    })
    if (!started) {             // doomctl was busy: decide again once it is free
      root.claimTried = false
      root.syncAgain = true
    }
  }
  function releaseToggle() {
    var started = ctlProc.start([root.doomctl, "saver-release"], function(code, text, error) {
      var c = code === 0 ? root.parse(text) : null
      if (c) root.claim = c
      root.log(c && c.released ? "released the screensaver toggle" : "saver-release: " + String(error || text).trim())
    })
    if (!started) root.syncAgain = true
  }
  Connections {
    target: ctlProc
    function onRunningChanged() {
      if (!ctlProc.running && root.syncAgain) {
        root.syncAgain = false
        Qt.callLater(root.syncSettings)
      }
    }
  }

  // ---------------------------------------------------------------- stock idle
  function readStock(then) {
    statusProc.start([root.omarchyShell, "idle", "status"], function(code, text) {
      var s = code === 0 ? root.parse(text) : null
      if (s) {
        root.stockEnabled = s.enabled === true && s.stayAwake !== true
        if (Number(s.screensaver) > 0) root.saverSeconds = Number(s.screensaver)
      }
      if (then) then(s)
    })
  }

  // Our monitor fires near the stock screensaver step; the stock service may get there a
  // moment later, so ask a few times before giving up. Launch only when the stock cycle is
  // armed and at its screensaver step: lock timing then stays exactly the stock one.
  property int gateTries: 0
  function gateStart() {
    root.gateTries = 0
    gate.restart()
  }
  Timer {
    id: gate
    interval: 250
    repeat: true
    onTriggered: {
      if (!root.isIdle || !root.armed || ++root.gateTries > 16) { gate.stop(); return }
      if (statusProc.running) return
      root.readStock(function(s) {
        if (!gate.running || !s) return
        if (s.enabled === true && s.stayAwake !== true && s.inIdleCycle === true && s.screensaverStarted === true) {
          gate.stop()
          root.launch()
        }
      })
    }
  }
  function launch() {
    root.log("idle: starting the Doom screensaver")
    launchProc.start([root.doomctl, "screensaver"], function(code, text, error) {
      if (code !== 0) root.log("screensaver did not start: " + String(error || text).trim())
    })
  }

  IdleMonitor {
    id: idleMonitor
    enabled: root.armed
    timeout: root.saverSeconds
    respectInhibitors: true
    onIsIdleChanged: if (isIdle) root.gateStart()
  }

  // Enabling the screensaver should take over within a second, not at the next poll: any
  // write to the settings file (replaced atomically by the backend) triggers a sync, shortly
  // after, so a burst of writes costs one. Watch only: the file is never read here (no
  // preload, no reload on change); doomctl stays the one reader of settings.
  FileView {
    id: configWatch
    path: root.configPath
    preload: false
    watchChanges: true
    printErrors: false
    onFileChanged: settingsSoon.restart()
  }
  Timer { id: settingsSoon; interval: 300; onTriggered: root.syncSettings() }

  // Settings and the stock timeout also change rarely enough to poll as a backstop.
  Timer {
    interval: 30000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      configWatch.reload()     // without preload this only (re)arms the watch, e.g. once the folder exists
      root.syncSettings()
      if (!gate.running) root.readStock(null)
    }
  }

  Component.onCompleted: {
    // Enabling the plugin (or a shell start) brings the game service up; it decides itself
    // whether a game needs loading.
    unitProc.start([root.systemctl, "--user", "start", root.unit], function(code, text, error) {
      root.log(code === 0 ? "started " + root.unit : "could not start " + root.unit + ": " + String(error).trim())
    })
  }

  // Disable or removal: `omarchy plugin remove` deletes this folder right after disabling,
  // so the release cannot call doomctl. It runs inline from the saved token and paths and
  // removes the toggle only if both files are still ours and still hold this token.
  // Tested by the root (src/saver_ownership.py INLINE_RELEASE): every path component is opened
  // without following symlinks, both files must be our regular files holding the token
  // (read bounded) with unchanged identity, and only those two files are unlinked.
  readonly property string releaseScript: "import os,stat,sys\nfrom pathlib import Path\ndef read(p):\n    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)\n    try:\n        for part in p.parts[1:-1]:\n            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=fd)\n            os.close(fd);fd=child\n        f=os.open(p.name,os.O_RDONLY|os.O_CLOEXEC|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=fd)\n        try:\n            s=os.fstat(f)\n            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid(): raise ValueError()\n            return os.read(f,129),s,os.dup(fd)\n        finally: os.close(f)\n    finally: os.close(fd)\ndef identity(s): return s.st_dev,s.st_ino,s.st_mtime_ns,s.st_ctime_ns\nfds=[]\ntry:\n    toggle,marker=map(Path,sys.argv[1:3]);token=sys.argv[3].encode('ascii')\n    if len(token)!=64 or any(c not in b'0123456789abcdef' for c in token): raise ValueError()\n    a,sa,fa=read(toggle);fds.append(fa)\n    b,sb,fb=read(marker);fds.append(fb)\n    if a==b==token and identity(sa)==identity(os.stat(toggle.name,dir_fd=fa,follow_symlinks=False)) and identity(sb)==identity(os.stat(marker.name,dir_fd=fb,follow_symlinks=False)):\n        os.unlink(toggle.name,dir_fd=fa);os.unlink(marker.name,dir_fd=fb)\nexcept (OSError,ValueError,IndexError,UnicodeError): pass\nfinally:\n    for fd in fds: os.close(fd)"
  Component.onDestruction: {
    var c = root.claim || ({})
    if (c.owned !== true || !c.token || !c.toggle || !c.marker) return
    Quickshell.execDetached({
      command: ["/usr/bin/python3", "-I", "-c", root.releaseScript, String(c.toggle), String(c.marker), String(c.token)],
      environment: { "PATH": "/usr/bin:/bin", "HOME": root.env.HOME || "" },
      clearEnvironment: true
    })
  }

  // `omarchy-shell shell call io.github.slogonomo.live-doom serviceStatus ''` for debugging.
  function serviceStatus() {
    return JSON.stringify({ armed: root.armed, saverEnabled: root.saverEnabled, claim: root.claim,
                            stockEnabled: root.stockEnabled, saverSeconds: root.saverSeconds,
                            idle: root.isIdle, lastEvent: root.lastEvent })
  }
}
