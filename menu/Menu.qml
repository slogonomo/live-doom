// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Io

// Live Doom settings console (Omarchy shell overlay plugin "io.github.slogonomo.live-doom",
// entry point menu/Menu.qml of the plugin's root manifest).
//
// Summoned by the Live Doom controller:
//   omarchy-shell shell summon io.github.slogonomo.live-doom '{"output":"DP-2"}'
// Two hosts. A right-click on the live background (no origin) gets the overlay in
// MenuWindow.qml, which belongs to the desktop and holds a pause lease. The Apps launcher
// (origin "launcher") and the first-run guide (origin "onboarding") get a real floating
// window, MenuFloat.qml, which behaves like any app and takes no lease.
// Talks to the backend only through `doomctl` (see docs/design/COORDINATION.md):
//   menu opened (once, from an explicit overlay open), menu renew (every 2 s while the overlay
//   is open; any renew failure closes it, fail closed), menu closed, settings, set <key> <json>,
//   preview-screensaver, hint-bot, choose-iwad, add-wads, use-live-wallpaper,
//   remove-live-wallpaper, use-game, onboarding step|finish|skip, download-shareware.
// Settings apply immediately; the first-run guide's background and screensaver choices
// wait for Finish. Typography and widgets come from the user's own IWAD
// (STCFN font, skull cursor, thermometer slider) via wadfont.py; nothing is redistributed.
// Steam package logos are shown from Steam's own cache (artbox.py trims their padding).
Item {
  id: root

  // Omarchy plugin host contract
  property var shell: null
  property var manifest: null
  property bool opened: false

  // ------------------------------------------------------------------ state
  property bool surfaceShown: false      // false while a file chooser owns the screen
  property bool choosing: false
  property string targetOutput: ""
  property var settings: null
  property var fontIndex: null
  property string fontIwad: ""
  property int cursor: -1
  property int sub: 0                  // 0 = the row's value, 1 = its inline button
  onCursorChanged: root.sub = 0
  property string statusLine: ""
  property bool statusError: false
  property int skullFrame: 0

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, ""))
  // The directory this code was compiled from. The installer deploys each version
  // to its own impl-<hash>/ directory (Qt caches compiled QML by URL), so this
  // proves which version the running shell actually loaded.
  function loadedVersion() { return root.pluginDir }
  readonly property string cacheBase: (Quickshell.env("XDG_CACHE_HOME") || (root.home + "/.cache")) + "/live-doom/menu-font"
  // The backend ships in the same plugin checkout: <plugin>/scripts/doomctl next to <plugin>/menu/.
  // No executable is ever taken from the environment. Offscreen test harnesses replace the
  // commands by setting these properties when they load the component.
  readonly property string pluginRoot: root.pluginDir.replace(/[^\/]+\/$/, "")
  property string doomctl: root.pluginRoot + "scripts/doomctl"
  property var themeCommand: null      // harness only: [program, args...] standing in for Omarchy's theme commands
  readonly property string python: "/usr/bin/python3"
  property Item cardItem: null              // set by MenuCard when it is created
  // DOOM_MENU_TEST=1: no layer-shell window (tests host MenuCard in a plain window)
  readonly property bool testMode: Quickshell.env("DOOM_MENU_TEST") === "1"

  // Phobos palette (the menu is the game's, so it keeps these under any theme)
  readonly property color cPanel: "#0d0a09"
  readonly property color cBone: "#e3d3b8"
  readonly property color cDim: "#8f7a68"
  readonly property color cMuted: "#6f5b4d"
  readonly property color cRust: "#4a2419"
  readonly property color cEmber: "#f26a1b"
  readonly property color cAmber: "#ffa21a"
  readonly property color cHell: "#ff3b1f"
  readonly property string mono: "JetBrainsMono Nerd Font"

  readonly property var rows: buildRows(settings)

  // ------------------------------------------------------- plugin lifecycle
  property int session: 0               // bumped per open; tags lease ops
  property bool awaitingLease: false    // first `menu opened` of this session not yet answered
  property bool leased: false           // this session holds the pause lease (overlay host only)
  property string host: "overlay"       // "overlay" (right-click) or "window" (Apps, first run)
  readonly property bool windowed: root.host === "window"

  function open(payloadJson) {
    var payload = ({})
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }
    root.targetOutput = String(payload.output || "")
    root.resetLaunchToast()
    if (root.opened) {
      if (payload.origin === "launcher") root.armLaunchToast()
      if (root.windowed && !root.choosing) {
        // summoned again: map the window afresh, so it comes up on this workspace with focus
        root.surfaceShown = false
        Qt.callLater(root.showSurface)
      } else if (!root.choosing && !root.awaitingLease) root.showSurface()
      root.refresh()
      return
    }
    root.session += 1
    root.opened = true
    root.host = payload.origin === "launcher" || payload.origin === "onboarding" ? "window" : "overlay"
    if (!root.testMode) windowLoader.use(root.host)
    if (payload.origin === "launcher") root.armLaunchToast()
    root.statusError = false
    if (root.windowed) {
      // A window takes no lease: like any window it makes its workspace busy (with Show on
      // Empty desktops, the game pauses there), and it can sit on any workspace without
      // freezing the others.
      root.statusLine = ""
      root.showSurface()
    } else {
      root.leased = true
      root.awaitingLease = true
      root.statusLine = "connecting to the game"
      // Hand the game back to the bot and release held keys before we take the
      // keyboard; the surface appears when this first lease call has answered.
      root.lifecycle("opened")
      showFallback.restart()
      heartbeat.start()
    }
    root.refresh()
  }

  // Opened from the app launcher: Omarchy shows "Launching …" two seconds after a launch
  // until a new toplevel window appears, which this layer-shell overlay never is. Close
  // that toast (the same IPC Omarchy's launcher uses) so it neither covers the footer
  // nor reads as the game still starting.
  property var osdCloseCommand: ["/usr/bin/omarchy-shell", "osd", "close"]   // harness replaces it
  property int launchToastTries: 0
  property int launchToastSession: -1
  function resetLaunchToast() {
    launchToast.stop()
    launchToast.interval = 2200
    root.launchToastTries = 0
    root.launchToastSession = -1
  }
  function armLaunchToast() {
    root.resetLaunchToast()
    root.launchToastSession = root.session
    launchToast.start()
  }
  Timer {
    id: launchToast
    interval: 2200
    onTriggered: {
      // only for the launcher open that armed it: a closed or newer session never acts
      if (!root.opened || root.launchToastSession !== root.session) return root.resetLaunchToast()
      if (!osdProc.running) {
        osdProc.command = root.osdCloseCommand
        osdDeadline.arm()
        osdProc.running = true
      }
      if (++root.launchToastTries < 2) { launchToast.interval = 800; launchToast.start() }
      else root.resetLaunchToast()
    }
  }
  Process {
    id: osdProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    onExited: osdDeadline.stop()
  }
  Deadline { id: osdDeadline; proc: osdProc; interval: 5000 }

  function close() {
    if (!root.opened) return
    root.resetLaunchToast()
    root.opened = false
    root.awaitingLease = false
    root.surfaceShown = false
    root.choosing = false
    root.chooserPending = false
    root.termsOpen = false     // unanswered terms are declined
    if (chooserProc.running) chooserProc.running = false   // our picker goes with the menu
    heartbeat.stop()
    showFallback.stop()
    root.flushPending()        // a quick Esc must not drop a debounced change
    if (root.leased) root.lifecycle("closed")
    root.leased = false
    root.wizStep = ""          // the guide's place is saved; the next open resumes it from settings
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "io.github.slogonomo.live-doom")
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function showSurface() {
    if (!root.opened || root.choosing) return
    root.surfaceShown = true
    Qt.callLater(function() { if (root.cardItem) root.cardItem.focusKeys() })
  }

  // ---------------------------------------------------- lease lifecycle queue
  // `menu opened` / `menu closed` / `preview-screensaver` run on their own serial
  // queue (never behind slow settings or WAD jobs), because the controller acts on
  // arrival order: a renewal landing after `closed` would pause the game for the
  // 6 s lease, and one landing after a preview would stop the screensaver. So
  // renewals coalesce, a close or preview drops renewals that have not started,
  // and a preview waits for the close queued before it. doomctl itself times out
  // these requests after ~2 s, so the queue cannot wedge.
  property var lifeQueue: []
  property var lifeCurrent: null

  // `opened` is the first lease call of a session; heartbeats send `renew`, which
  // only extends a lease that is still held and never stops a running screensaver
  // (an idle saver must not be killed by a forgotten menu, or the lock is skipped).
  // A failed renew always closes the menu: never fall back to `opened`.

  function lifecycle(op) {
    var q = root.lifeQueue.slice()
    if (op === "opened" || op === "renew") {
      var busy = function(item) { return item && (item.op === "opened" || item.op === "renew") && item.session === root.session }
      if (op === "renew" && busy(root.lifeCurrent)) return
      for (var i = 0; i < q.length; i++) if (busy(q[i])) return
      // a new session must not inherit a preview queued by the one before it
      if (op === "opened") q = q.filter(function(item) { return item.op !== "preview" })
    } else {
      q = q.filter(function(item) { return item.op !== "opened" && item.op !== "renew" })
    }
    q.push({ op: op, session: root.session })
    root.lifeQueue = q
    Qt.callLater(root.lifePump)
  }

  function lifePump() {
    if (root.lifeCurrent || lifeProc.running || root.lifeQueue.length === 0) return
    var q = root.lifeQueue.slice()
    var next = q.shift()
    root.lifeQueue = q
    root.lifeCurrent = next
    lifeProc.command = next.op === "preview"
      ? [root.doomctl, "preview-screensaver"]
      : [root.doomctl, "menu", next.op]
    lifeProc.didStart = false
    lifeProc.launching = true
    lifeDeadline.arm()
    lifeProc.running = true
  }

  function lifeFinished(exitCode, errText) {
    var done = root.lifeCurrent
    if (!done) return
    root.lifeCurrent = null
    if (done.op === "opened" && done.session === root.session && root.awaitingLease && root.opened) {
      root.awaitingLease = false
      showFallback.stop()
      if (exitCode !== 0) root.say((errText || "the game did not answer").trim(), true)
      root.showSurface()
    } else if (done.op === "preview" && exitCode !== 0) {
      console.warn("doom-menu: preview-screensaver failed:", (errText || "").trim())
      if (root.opened && !root.leased) root.say((errText || "the preview did not start").trim(), true)
    } else if (done.op === "renew" && exitCode !== 0 && done.session === root.session && root.opened) {
      // Fail closed: lease gone (screensaver or preview took over), a backend
      // without `menu renew`, or doomctl unavailable all close the menu.
      console.warn("doom-menu: lease renewal failed, closing:", (errText || "").trim())
      root.dismiss()
    }
    Qt.callLater(root.lifePump)
  }

  // Every process the menu starts has a deadline: past it the process is stopped and the
  // job fails like any other error, so a hung command can never stall the queues. Bounds sit
  // above doomctl's own client budgets: 130 s for ordinary commands, 300 s for download-shareware
  // (a renew can wait out such an operation, so lifecycle gets 310 s); a theme install, run by
  // this process with Omarchy's own command, gets 190 s.
  component Deadline: Timer {
    property var proc: null
    property bool hit: false
    function arm() { hit = false; restart() }
    onTriggered: if (proc && proc.running) { hit = true; proc.running = false }
  }
  readonly property int testLimit: root.testMode && Number(Quickshell.env("DOOM_MENU_DEADLINE_MS")) > 0
                                   ? Number(Quickshell.env("DOOM_MENU_DEADLINE_MS")) : 0
  function jobLimit(args) {
    if (root.testLimit) return root.testLimit
    var verb = args && args.length ? args[0] : ""
    return verb === "download-shareware" ? 310000 : 140000
  }
  Deadline { id: lifeDeadline; proc: lifeProc; interval: root.testLimit || 310000 }
  Deadline { id: jobDeadline; proc: jobProc; interval: 140000 }
  Deadline { id: chooserDeadline; proc: chooserProc; interval: 1800000 }   // a person is choosing files
  Deadline { id: fontDeadline; proc: fontProc; interval: 60000 }
  Deadline { id: artDeadline; proc: artProc; interval: 15000 }

  // Quickshell 0.3.1: a process that fails to start (doomctl missing or not
  // executable) clears `running` without `started` or `exited`. Those launches
  // complete here; ordinary runs complete only through onExited.
  Process {
    id: lifeProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    property bool didStart: false
    property bool launching: false
    stderr: StdioCollector { id: lifeErr }
    onStarted: { lifeProc.didStart = true; lifeProc.launching = false }
    onRunningChanged: if (!running && lifeProc.launching && !lifeProc.didStart) {
      lifeProc.launching = false
      lifeDeadline.stop()
      root.lifeFinished(-1, "could not run " + root.doomctl)
    }
    onExited: function(exitCode) {
      lifeDeadline.stop()
      root.lifeFinished(exitCode, lifeDeadline.hit ? "the game did not answer in time" : (lifeErr.text || ""))
    }
  }
  Timer {
    id: heartbeat      // the controller's lease expires 6 s after the last renewal
    interval: 2000
    repeat: true
    // renew-only: the acquire verb (`opened`) belongs to an explicit open() alone
    onTriggered: if (root.opened && !root.awaitingLease) root.lifecycle("renew")
  }
  Timer {
    id: showFallback   // backend unresponsive: show anyway rather than strand the user
    interval: 1500
    onTriggered: {
      if (!root.opened || !root.awaitingLease) return
      root.awaitingLease = false
      root.say("the game is not answering", true)
      root.showSurface()
    }
  }

  // ------------------------------------------------------------ doomctl jobs
  // One serial queue: the daemon applies each `set` as its own transaction, and
  // ordering keeps optimistic UI state and the backend from crossing.
  property var jobs: []
  property var currentJob: null

  function run(args, done) {
    root.jobs.push({ args: args, done: done })
    Qt.callLater(root.pump)
  }

  // Run fn once every edit made so far has reached the backend: flush debounced
  // values, then wait behind the queued `set` jobs. Preview and the file chooser
  // use this so a late edit can neither stop a fresh preview nor overwrite WADs
  // the user just chose.
  function afterSettings(fn) {
    root.flushPending()
    root.jobs.push({ barrier: fn })
    Qt.callLater(root.pump)
  }

  function pump() {
    if (root.currentJob || jobProc.running) return
    while (root.jobs.length > 0 && root.jobs[0].barrier) {
      var b = root.jobs.shift()
      try { b.barrier() } catch (e) { console.warn("doom-menu barrier:", e) }
    }
    if (root.jobs.length === 0) return
    root.currentJob = root.jobs.shift()
    jobProc.outDone = false
    jobProc.exitDone = false
    jobProc.failedStart = false
    jobProc.didStart = false
    jobProc.launching = true
    jobProc.command = [root.doomctl].concat(root.currentJob.args)
    jobDeadline.interval = root.jobLimit(root.currentJob.args)
    jobDeadline.arm()
    jobProc.running = true
  }

  function finishJob() {
    if (!jobProc.outDone || !jobProc.exitDone || !root.currentJob) return
    var job = root.currentJob
    root.currentJob = null
    jobDeadline.stop()
    var out = jobProc.failedStart || jobDeadline.hit ? "" : (jobOut.text || "")
    var err = jobProc.failedStart ? "could not run " + root.doomctl
            : (jobDeadline.hit ? "the game did not answer in time" : (jobErr.text || ""))
    try { if (job.done) job.done(jobProc.code, out, err) } catch (e) { console.warn("doom-menu job callback:", e) }
    Qt.callLater(root.pump)
  }

  Process {
    id: jobProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    property bool outDone: false
    property bool exitDone: false
    property bool failedStart: false
    property bool didStart: false
    property bool launching: false
    property int code: 0
    stdout: StdioCollector { id: jobOut; onStreamFinished: { jobProc.outDone = true; root.finishJob() } }
    stderr: StdioCollector { id: jobErr }
    onStarted: { jobProc.didStart = true; jobProc.launching = false }
    onRunningChanged: if (!running && jobProc.launching && !jobProc.didStart) {   // failed to start
      jobProc.launching = false
      jobProc.failedStart = true
      jobProc.code = -1
      jobProc.outDone = true
      jobProc.exitDone = true
      root.finishJob()
    }
    onExited: function(exitCode) {
      jobProc.code = jobDeadline.hit ? -1 : exitCode
      jobProc.exitDone = true
      root.finishJob()
    }
  }

  // Native file chooser: give it the screen and the keyboard, come back after.
  property bool chooserPending: false   // waiting for queued edits before the chooser starts
  property string chooserBefore: ""
  property int setFailures: 0           // counts rejected sets (a chooser aborts if one lands first)
  function gameKey(s) {
    var g = s && s.game ? s.game : ({})
    return JSON.stringify([g.iwad || "", g.pwads || []])
  }
  function chooserFinished(session, exitCode, errText) {   // session is used by the report below
    if (session !== root.session || !root.opened) return     // stale: a later session owns the menu
    root.choosing = false
    root.showSurface()
    if (exitCode !== 0) {
      root.say((errText || "the file chooser failed").trim(), true)
      root.refresh()
      return
    }
    // A cancelled dialog also exits 0: compare raw backend snapshots, before and after.
    var before = root.chooserBefore
    root.run(["settings"], function(code, out, err) {
      if (session !== root.session || !root.opened) return
      if (code !== 0) { root.say((err || out || "the game is not running").trim(), true); return }
      var after = ""
      try { after = root.gameKey(JSON.parse(out)) } catch (e) { after = "" }
      root.applySettings(out)
      root.say(after && after === before ? "no change" : "game files updated", false)
    })
  }
  Process {
    id: chooserProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    property int session: -1
    property bool didStart: false
    property bool launching: false
    stderr: StdioCollector { id: chooserErr }
    onStarted: { chooserProc.didStart = true; chooserProc.launching = false }
    onRunningChanged: if (!running && chooserProc.launching && !chooserProc.didStart) {   // failed to start
      chooserProc.launching = false
      chooserDeadline.stop()
      root.chooserFinished(chooserProc.session, -1, "could not run " + root.doomctl)
    }
    onExited: function(exitCode) {
      chooserDeadline.stop()
      root.chooserFinished(chooserProc.session, chooserDeadline.hit ? -1 : exitCode,
                           chooserDeadline.hit ? "the file chooser was closed after 30 minutes" : (chooserErr.text || ""))
    }
  }
  function choose(command) {
    if (root.chooserPending || root.choosing || chooserProc.running) {   // one chooser at a time
      root.say("a file chooser is already open", false)
      return
    }
    var session = root.session
    root.flushPending()                // their status echoes land now, not over the message below
    var failuresBefore = root.setFailures
    root.chooserPending = true         // the menu stays up (read-only) until the edits land
    root.say("applying changes…", false)
    root.afterSettings(function() {
      if (!root.opened || root.session !== session) return   // closed meanwhile; close() reset state
      if (root.setFailures !== failuresBefore) {               // an edit was rejected: keep its error up
        root.chooserPending = false
        return
      }
      // Snapshot what the backend really has (not optimistic local state) for the
      // "no change" check, then hand the screen and keyboard to the chooser.
      root.run(["settings"], function(code, out, err) {
        if (!root.opened || root.session !== session || !root.chooserPending) return
        root.chooserPending = false
        if (code !== 0) { root.say((err || out || "the game is not running").trim(), true); return }
        try { root.chooserBefore = root.gameKey(JSON.parse(out)) } catch (e) { root.chooserBefore = "" }
        root.applySettings(out)
        root.choosing = true
        root.surfaceShown = false
        root.say(command === "choose-iwad" ? "choosing a game file" : "adding levels and mods", false)
        chooserProc.session = session
        chooserProc.didStart = false
        chooserProc.launching = true
        chooserProc.command = [root.doomctl, command]
        chooserDeadline.arm()
        chooserProc.running = true
      })
    })
  }

  // Glyphs/patches from the configured IWAD
  property string fontTried: ""
  Process {
    id: fontProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    property string iwad: ""
    onExited: {
      fontDeadline.stop()
      var want = root.settings && root.settings.game ? root.settings.game.iwad : ""
      if (want && want !== root.fontTried) Qt.callLater(function() { root.loadFont(want) })
    }
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var idx = JSON.parse(text)
          var ok = idx && idx.glyphs && Object.keys(idx.glyphs).length > 0
          root.fontIndex = ok ? idx : null      // e.g. an IWAD without STCFN: mono fallback
          root.fontIwad = fontProc.iwad
        } catch (e) { /* keep the mono fallback */ }
      }
    }
  }
  function loadFont(iwad) {
    if (!iwad || iwad === root.fontTried || fontProc.running) return
    root.fontTried = iwad
    fontProc.iwad = iwad
    fontProc.command = [root.python, root.pluginDir + "wadfont.py", "export", iwad, root.cacheBase]
    fontDeadline.arm()
    fontProc.running = true
  }

  // Visible-pixel box of each Steam logo (artbox.py reads the cached file in place), so
  // the banner can clip away the logo's transparent padding. One lookup per path.
  property var artBoxes: ({})
  property var artTried: ({})
  Process {
    id: artProc
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    property string path: ""
    onExited: { artDeadline.stop(); Qt.callLater(root.loadArt) }
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var b = JSON.parse(text)
          if (b && b.w > 0 && b.h > 0) {
            var next = Object.assign({}, root.artBoxes)
            next[artProc.path] = b
            root.artBoxes = next
          }
        } catch (e) { /* show the whole image */ }
      }
    }
  }
  function loadArt() {
    if (artProc.running) return
    for (var i = 0; i < root.rows.length; i++) {
      var img = root.rows[i].kind === "banner" ? root.rows[i].image : ""
      if (img && !root.artTried[img]) {
        root.artTried[img] = true
        artProc.path = img
        artProc.command = [root.python, root.pluginDir + "artbox.py", img]
        artDeadline.arm()
        artProc.running = true
        return
      }
    }
  }
  onRowsChanged: Qt.callLater(root.loadArt)

  // --------------------------------------------------------------- settings
  function refresh(then) {
    root.run(["settings"], function(code, out, err) {
      if (code === 0) {
        root.applySettings(out)
        if (root.statusLine === "connecting to the game") root.say("", false)
        if (then) then()
      } else {
        root.say((err || out || "the game is not running").trim(), true)
      }
    })
  }

  function applySettings(json) {
    try {
      var s = JSON.parse(json)
      if (!s || typeof s !== "object" || !s.sound) return
      // A snapshot can predate edits still queued or settling; keep those.
      for (var k in root.inflight) root.assign(s, k, root.inflight[k].value)
      for (var q in root.pending) root.assign(s, q, root.pending[q])
      root.settings = s
      if (s.game && s.game.iwad) root.loadFont(s.game.iwad)
      var ob = s.onboarding && s.onboarding.show ? s.onboarding : null
      if (ob && root.wizStep === "") {            // first settings of this open: resume the guide
        root.wizSync(ob)
        root.wizCursor()
      } else if (ob && root.wizSteps(ob).indexOf(root.wizStep) === -1) {
        root.wizStep = root.wizSteps(ob).indexOf("screensaver") !== -1 && root.wizStep === "game"
                     ? "screensaver" : root.wizSteps(ob)[0]     // a step that no longer applies
        root.wizCursor()
      }
      root.clampCursor()
    } catch (e) { root.say("unreadable settings from doomctl", true) }
  }

  function getv(obj, key) {
    var parts = key.split(".")
    var v = obj
    for (var i = 0; i < parts.length; i++) { if (v === null || v === undefined) return undefined; v = v[parts[i]] }
    return v
  }

  function assign(obj, key, value) {
    var parts = key.split(".")
    var o = obj
    for (var i = 0; i < parts.length - 1; i++) { if (!o[parts[i]]) o[parts[i]] = ({}); o = o[parts[i]] }
    o[parts[parts.length - 1]] = value
  }
  function setLocal(key, value) {
    var copy = JSON.parse(JSON.stringify(root.settings || {}))
    root.assign(copy, key, value)
    root.settings = copy
  }

  // Toggles and choices go straight out; sliders and the WAD list settle first.
  property var pending: ({})
  function setValue(key, value, settle) {
    root.setLocal(key, value)
    if (settle) {
      var p = root.pending
      p[key] = value
      root.pending = p
      settleTimer.interval = key === "game.pwads" ? 450 : 220
      settleTimer.restart()
      return
    }
    root.send(key, value)
  }
  property var inflight: ({})      // key -> {value, count}: sets queued or running
  function send(key, value) {
    if (!root.chooserPending) root.say(key + " = " + JSON.stringify(value), false)
    var f = root.inflight
    var entry = f[key] || { value: value, count: 0 }
    entry.value = value
    entry.count += 1
    f[key] = entry
    root.inflight = f
    root.run(["set", key, JSON.stringify(value)], function(code, out, err) {
      var g = root.inflight
      if (g[key] && --g[key].count <= 0) delete g[key]
      root.inflight = g
      if (code === 0) {
        root.applySettings(out)
      } else {
        root.setFailures += 1
        root.say((err || out || "rejected").trim(), true)
        root.refresh()
      }
    })
  }
  function flushPending() {
    settleTimer.stop()
    var p = root.pending
    root.pending = ({})
    for (var k in p) root.send(k, p[k])
  }
  Timer {
    id: settleTimer
    interval: 220
    onTriggered: root.flushPending()
  }

  function say(text, isError) {
    // one line: a traceback or multi-line stderr shows its last meaningful line
    var lines = String(text).split(/\r?\n/).filter(function(l) { return l.trim() !== "" })
    // the backend's "ERR " prefix is protocol, not message
    root.statusLine = (lines.length ? lines[lines.length - 1].trim() : "").replace(/^err\s+/i, "").toLowerCase()
    root.statusError = !!isError
    // errors also go to the shell log (bounded), so a failure stays diagnosable after the menu closes
    if (isError && lines.length) console.warn("live-doom menu: " + lines[lines.length - 1].trim().slice(0, 300))
  }

  // ------------------------------------------------------------------- rows
  function basename(p) { var s = String(p || ""); return s.substring(s.lastIndexOf("/") + 1) }

  function buildRows(s) {
    var r = []
    if (!s) return r
    function section(t) { r.push({ kind: "section", label: t }) }
    function toggle(key, label) { r.push({ kind: "toggle", key: key, label: label }) }

    // first run: the guide is the whole menu until it is finished or skipped
    if (s.onboarding && s.onboarding.show) { root.wizardRows(r, s, s.onboarding); return r }

    section("Sound")
    toggle("sound.playing", "While playing")
    toggle("sound.screensaver", "Screensaver")
    toggle("sound.idle", "Idle desktop")

    section("Live background")
    r.push({ kind: "choice", key: "wallpaper.mode", label: "Show on",
             options: [["empty", "Empty desktops"], ["all", "All desktops"]] })
    var wp = s.wallpaper || ({})
    // behind windows the game redraws lightly by default; only matters when it keeps playing there
    if (wp.mode === "all" && typeof wp.behind_windows === "string")
      r.push({ kind: "choice", key: "wallpaper.behind_windows", label: "Behind windows",
               options: [["light", "Light"], ["full", "Full speed"]] })
    if (!wp.active) r.push({ kind: "action", id: "uselive", label: "Use Doom with this theme", value: "Go" })
    if (wp.can_remove) r.push({ kind: "action", id: "removelive", label: "Remove Doom from this theme", value: "Remove" })
    var offer = root.phobosOffer(s.onboarding)
    if (offer) r.push({ kind: "action", id: "phobos", label: offer === "install" ? "Install our Phobos theme" : "Use our Phobos theme",
                        value: offer === "install" ? "Install" : "Go" })

    section("Live screensaver")
    r.push({ kind: "toggle", key: "screensaver.enabled", label: "Enabled", extra: { id: "preview", label: "Preview" } })

    section("View and controls")
    r.push({ kind: "slider", key: "blur.percent", label: "Blur when paused", min: 0, max: 100, step: 5, digits: 0, suffix: "%" })
    r.push({ kind: "slider", key: "mouse.sensitivity", label: "Mouse speed", min: 0.1, max: 4, step: 0.1, digits: 1, suffix: "" })
    if (!s.mouse || s.mouse.mouselook_supported !== false) toggle("mouse.mouselook", "Full mouselook")

    var g = s.game || ({})
    if (g.catalog) root.officialRows(r, g.catalog, g.selected_package || null)
    root.freeRows(r, s)

    section("Game")
    r.push({ kind: "action", id: "iwad", label: "IWAD", value: "Change",
             detail: root.basename(g.iwad) + (g.iwad_title ? "  ·  " + g.iwad_title : "") })
    var pw = g.pwads || []
    for (var i = 0; i < pw.length; i++)
      r.push({ kind: "pwad", index: i, label: String(i + 1), detail: root.basename(pw[i]), path: pw[i] })
    r.push({ kind: "action", id: "addwads", label: "Levels and mods", value: "Add" })
    var ag = s.agent
    if (ag && ag.available && ag.available.length) {   // external agents (docs/design/EXTERNAL-AGENT.md)
      var opts = [["autodoom", "AutoDoom"]], name = ""
      for (var j = 0; j < ag.available.length; j++) {
        opts.push([ag.available[j].id, ag.available[j].name || ag.available[j].id])
        if (ag.available[j].id === ag.selected) name = opts[opts.length - 1][1]
      }
      r.push({ kind: "choice", key: "agent.selected", label: "Driver", options: opts })
      if (ag.selected && ag.selected !== "autodoom")
        r.push({ kind: "note", text: root.agentNote(ag, name || ag.selected) })
    }
    if (!s.runtime || s.runtime.loaded !== false)   // nothing to reroute while Doom is closed
      r.push({ kind: "action", id: "reroute", label: "Reroute bot", value: "Go" })
    return r
  }

  // ------------------------------------------------------------ first-run guide
  // Live Background, Choose your DOOM (only when no official game was found), Live
  // Screensaver, then a summary. The background and screensaver choices are inert until
  // Finish (the backend saves them with the step, `onboarding step`), so Back never undoes
  // anything. The game step acts at once: a download happens where id's terms are accepted.
  property bool termsOpen: false        // the shareware terms are on screen, awaiting Accept
  property real termsShownAt: 0         // an Accept sooner than this after the terms opened is not a reading
  onTermsOpenChanged: if (termsOpen) root.termsShownAt = Date.now()
  property string wizStep: ""           // "" until this open has read the saved step
  property string wizBackground: "current"
  property bool wizScreensaver: false

  function wizSteps(ob) {
    var all = ["background", "game", "screensaver", "summary"]
    var st = ob && ob.steps && ob.steps.length ? ob.steps : all
    st = st.filter(function(x) { return all.indexOf(x) !== -1 })
    return st.length ? st : all
  }
  function wizSync(ob) {
    var c = ob.choices || ({})
    root.wizBackground = ["current", "phobos", "skip"].indexOf(c.background) !== -1 ? c.background : "current"
    root.wizScreensaver = c.screensaver === true
    var steps = root.wizSteps(ob)
    root.wizStep = steps.indexOf(ob.step) !== -1 ? ob.step : steps[0]
  }
  // Phobos can only be chosen while it can be offered; otherwise the choice reads as the current theme.
  function wizBackgroundChoice(ob) {
    return root.wizBackground === "phobos" && !root.phobosOffer(ob) ? "current" : root.wizBackground
  }
  function wizCursor() {               // each step opens on Next, so Enter takes the easy path
    root.cursorTo(function(r) { return r.kind === "nav" })
    root.sub = 0
  }
  function wizSave() {                 // the step and the inert choices, for a resume after a restart
    if (root.wizStep === "") return
    root.run(["onboarding", "step", root.wizStep, "--background", root.wizBackground,
              "--screensaver", root.wizScreensaver ? "on" : "off"], function(code, out, err) {
      if (code !== 0) console.warn("live-doom menu: could not save the guide's place: " + String(err || out).trim().slice(0, 200))
    })
  }
  function wizGo(dir) {
    var ob = root.settings && root.settings.onboarding
    if (!ob || !ob.show || root.wizStep === "") return
    if (root.switching) return root.say("still applying the previous change", false)
    var steps = root.wizSteps(ob)
    var i = Math.max(0, steps.indexOf(root.wizStep))
    if (dir > 0 && steps[i] === "summary") return root.wizFinish()
    var j = Math.max(0, Math.min(steps.length - 1, i + dir))
    if (j === i) return
    root.termsOpen = false
    root.wizStep = steps[j]
    root.say("", false)
    root.wizSave()
    root.wizCursor()
  }
  function wizSkip() {
    root.switchJob(["onboarding", "skip"], "", "could not save that", function(s) {
      root.termsOpen = false
      root.cursor = -1
      root.clampCursor()
      return ["setup skipped. every setting is here", false]
    })
  }
  // Display name of an Omarchy theme directory ("osaka-jade" -> "Osaka Jade").
  function themeTitle(s) {
    var n = String(s && s.wallpaper && s.wallpaper.theme || "")
    if (!n) return "your current theme"
    return n.split(/[-_]+/).map(function(w) { return w ? w.charAt(0).toUpperCase() + w.slice(1) : w }).join(" ")
  }
  // "freedoom" or "shareware" when exactly one of the bundled free games is selected (the
  // backend says so in selected_package; with mods or another IWAD it is null), else "".
  function freeSelected(s) {
    var g = s && s.game || ({})
    return g.selected_package === "freedoom" || g.selected_package === "shareware" ? g.selected_package : ""
  }
  // What the summary calls the selected game.
  function gameTitle(s, ob) {
    if (ob.official_found) return "Steam installation detected"
    var free = root.freeSelected(s)
    if (free) return free === "shareware" ? "DOOM shareware" : "Freedoom"
    var g = s.game || ({})
    return String(g.iwad_title || root.basename(g.iwad) || "Your own game")
  }
  // id's terms, then the only way to accept them: a button where the reading ends
  function termsRow(r, ob) {
    var t = ob && ob.shareware_terms
    if (!root.termsOpen || !t) return
    r.push({ kind: "note", text: String(t.text || "").slice(0, 6000)
      + (t.url ? "\n\nFrom " + t.url : "") + (t.sha256 ? "\nsha256 " + t.sha256 : "")
      + "\n\nEsc closes without downloading." })
    r.push({ kind: "action", id: "acceptTerms", label: "Accept and download", value: "Accept" })
  }
  function sharewareValue(ob) {
    return ob.shareware === "downloading" ? "Wait" : (root.termsOpen ? "Terms below" : "Free")
  }

  function wizardRows(r, s, ob) {
    if (root.wizStep === "") {         // reopened: wait for this open's settings before offering choices
      r.push({ kind: "section", label: "Welcome to Live Doom" })
      r.push({ kind: "note", text: "One moment…" })
      return
    }
    var steps = root.wizSteps(ob)
    var step = steps.indexOf(root.wizStep) !== -1 ? root.wizStep : steps[0]
    var bg = root.wizBackgroundChoice(ob)
    if (step === "background") {
      r.push({ kind: "section", label: "Live Background" })
      r.push({ kind: "note", text: "A bot plays Doom on your empty desktops. Left click to jump in any time." })
      r.push({ kind: "option", id: "wizBg", choice: "current", label: "Add to current theme", on: bg === "current" })
      r.push({ kind: "note", text: "Adds and selects the live background on " + root.themeTitle(s)
        + ". SUPER CTRL + SPACE to switch backgrounds and turn it off." })
      var offer = root.phobosOffer(ob)
      if (offer) {
        r.push({ kind: "option", id: "wizBg", choice: "phobos", on: bg === "phobos",
                 label: offer === "install" ? "Install our Phobos theme" : "Use our Phobos theme" })
        r.push({ kind: "note", text: (offer === "install" ? "Installs" : "Switches to")
          + " our DOOM-inspired theme with the live background pre-installed and selected." })
      }
      r.push({ kind: "option", id: "wizBg", choice: "skip", label: "Skip live background", on: bg === "skip" })
      r.push({ kind: "note", text: "Add it later by running Live Doom from your Apps menu. Live takeover unavailable." })
    } else if (step === "game") {
      var free = root.freeSelected(s)
      r.push({ kind: "section", label: "Choose your DOOM" })
      if (!ob.freedoom || ob.freedoom.available !== false)
        r.push({ kind: "option", id: "useFreedoom", label: "Freedoom", on: free === "freedoom" })
      if (ob.shareware === "present")
        r.push({ kind: "option", id: "useShareware", label: "DOOM shareware", on: free === "shareware" })
      else {
        r.push({ kind: "option", id: "shareware", label: "Download DOOM shareware", value: root.sharewareValue(ob) })
        root.termsRow(r, ob)
      }
      r.push({ kind: "note", text: "Add .wads from the menu at any time." })
    } else if (step === "screensaver") {
      r.push({ kind: "section", label: "Live Screensaver" })
      r.push({ kind: "note", text: "DOOM replaces your screensaver, and the bot continues the game." })
      r.push({ kind: "toggle", id: "wizSaver", label: "Enable", on: root.wizScreensaver,
               extra: { id: "wizPreview", label: "Preview" } })
    } else {
      // an already-running live background stays on when Skip is chosen (Finish never removes it)
      var live = bg !== "skip" || !!(s.wallpaper && s.wallpaper.active)
      r.push({ kind: "section", label: "Your setup" })
      r.push({ kind: "fact", label: "Live Background", value: live ? "Enabled" : "Disabled" })
      r.push({ kind: "fact", label: "DOOM", value: root.gameTitle(s, ob) })
      r.push({ kind: "fact", label: "Screensaver", value: root.wizScreensaver ? "Enabled" : "Disabled" })
      if (live)
        r.push({ kind: "note", text: "Switch to an empty desktop to play.\nLeft-click to take over. F12 to hand it back. Right-click for menu." })
      r.push({ kind: "note", text: "Open Live Doom from Apps to change settings at any time." })
    }
    r.push({ kind: "nav", id: "wizNav", back: steps.indexOf(step) > 0, next: step === "summary" ? "Finish" : "Next" })
    if (step !== "summary") r.push({ kind: "link", id: "wizSkip", label: "Skip setup and show all settings" })
  }

  // Finish applies the saved choices with the ordinary verbs, in order, then marks the guide
  // done. Any failure stops there and stays on the summary with the reason.
  function wizFinish() {
    var ob = root.settings && root.settings.onboarding
    if (!ob || !ob.show) return
    if (root.switching || themeProc.running) return root.say("still applying the previous change", false)
    var bg = root.wizBackgroundChoice(ob), saver = root.wizScreensaver
    root.switching = true
    var fail = function(text) {
      root.switching = false
      root.say(String(text || "could not finish setup").trim(), true)
      root.refresh()
    }
    var chain = []
    if (bg === "phobos") chain.push(function(next) {
      root.say(root.phobosOffer(ob) === "install" ? "installing phobos" : "switching to phobos", false)
      root.runTheme(root.phobosOffer(ob), ob.theme, function(code, error) {
        if (code === 0) next(); else fail(error || "could not install phobos")
      })
    })
    if (bg !== "skip") chain.push(function(next) {
      root.say("turning on the live background", false)
      root.run(["use-live-wallpaper"], function(code, out, err) {
        if (code === 0) next(); else fail(err || out || "could not turn on the live background")
      })
    })
    chain.push(function(next) {
      var now = !!(root.settings && root.settings.screensaver && root.settings.screensaver.enabled)
      if (now === saver) return next()
      root.run(["set", "screensaver.enabled", JSON.stringify(saver)], function(code, out, err) {
        if (code === 0) next(); else fail(err || out || "could not change the screensaver")
      })
    })
    chain.push(function() {
      root.run(["onboarding", "finish"], function(code, out, err) {
        if (code !== 0) return fail(err || out || "could not save that")
        root.switching = false
        root.termsOpen = false
        root.refresh(function() {
          root.cursor = -1
          root.clampCursor()
          var w = root.settings && root.settings.wallpaper || ({})
          if (bg === "skip") root.say("all set. open live doom from apps any time", false)
          else if (w.active) root.say("live doom is on", false)
          else root.say("selected; waiting for the background to switch", true)
        })
      })
    })
    var i = 0
    var next = function() { if (i < chain.length) chain[i++](next) }
    root.say("finishing setup", false)
    root.afterSettings(next)
  }

  // What the Phobos step can do: "set" (installed, not active), "install" (a trusted https
  // GitHub URL from the backend), or "" (nothing to offer; Use Doom with this theme remains).
  function phobosOffer(ob) {
    var th = ob && ob.theme
    if (!th || th.active === true || !/^[a-z0-9][a-z0-9._+-]*$/.test(String(th.name || ""))) return ""
    if (th.installed === true) return "set"
    return /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\.git)?$/.test(String(th.url || "")) ? "install" : ""
  }

  // Every process the menu starts gets this closed session environment: a fixed PATH and only
  // the variables the session needs (offscreen test mode keeps the harness's fake knobs).
  // Theme install/switch runs from this session process too (Omarchy's own commands need the
  // desktop session; the game service never runs them), then use-live-wallpaper, because a
  // remembered static background could otherwise win the switch.
  readonly property var sessionEnv: {
    var keep = ["HOME", "USER", "LOGNAME", "LANG", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "XDG_CACHE_HOME", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SESSION_BUS_ADDRESS"]
    var e = { "PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy", "GIT_TERMINAL_PROMPT": "0" }
    for (var i = 0; i < keep.length; i++) { var v = Quickshell.env(keep[i]); if (v) e[keep[i]] = String(v) }
    return e
  }
  Process {
    id: themeProc
    property var done: null
    property bool didStart: false
    property bool launching: false
    environment: root.sessionEnv
    clearEnvironment: !root.testMode
    stderr: StdioCollector { id: themeErr }
    onStarted: { themeProc.didStart = true; themeProc.launching = false }
    onRunningChanged: if (!running && themeProc.launching && !themeProc.didStart) {   // failed to start
      themeProc.launching = false
      themeDeadline.stop()
      var f = themeProc.done
      themeProc.done = null
      if (f) f(-1, "could not run " + String(themeProc.command[0]))
    }
    onExited: function(code) {
      themeDeadline.stop()
      var f = themeProc.done
      themeProc.done = null
      if (f) f(themeDeadline.hit ? -1 : code, themeDeadline.hit ? "the theme install did not finish in time" : (themeErr.text || ""))
    }
  }
  Deadline { id: themeDeadline; proc: themeProc; interval: root.testLimit || 190000 }
  // From the full menu: install (or switch to) Phobos now, then select its live background.
  function applyPhobos() {
    var ob = root.settings && root.settings.onboarding
    var offer = root.phobosOffer(ob)
    if (!offer) return
    if (root.switching || themeProc.running) return root.say("still applying the previous change", false)
    root.switching = true
    root.say(offer === "install" ? "installing phobos" : "switching to phobos", false)
    root.runTheme(offer, ob.theme, function(code, error) {
      root.switching = false
      if (code !== 0) return root.say((error || "could not install phobos").trim(), true)
      root.liveChoice("phobos", "use-live-wallpaper", "turning on the live background", function(w) {
        return w.active ? ["phobos is on, with live doom", false] : ["phobos is on; pick live doom in the background picker", true]
      })
    })
  }
  // Install or switch to Phobos with Omarchy's own theme commands; done(code, error).
  function runTheme(offer, th, done) {
    if (!offer || !th || themeProc.running) return done(-1, "the phobos theme is not available")
    var fake = root.themeCommand
    themeProc.command = offer === "set"
      ? (fake ? fake.concat(["set", th.name]) : ["/usr/bin/omarchy-theme-set", th.name])
      : (fake ? fake.concat(["install", th.url]) : ["/usr/bin/omarchy-theme-install", th.url])
    themeProc.done = done
    themeProc.didStart = false
    themeProc.launching = true
    themeDeadline.arm()
    themeProc.running = true
  }

  // Official releases found in Steam libraries (backend catalog), grouped by Steam package
  // under its locally cached logo. Playable ones are actions; detected-but-unsupported ones
  // stay visible, dimmed and unselectable, marked "(not supported)".
  function officialRows(r, cat, selected) {
    r.push({ kind: "section", label: "Official games" })
    var pk = cat.packages || [], groups = [], byKey = {}
    for (var i = 0; i < pk.length; i++) {
      var src = pk[i].source && typeof pk[i].source === "object" ? pk[i].source
              : { title: pk[i].source_title || "Steam", appid: pk[i].appid }
      var key = String(src.appid || src.title)
      if (!byKey[key]) { byKey[key] = { source: src, items: [] }; groups.push(byKey[key]) }
      byKey[key].items.push(pk[i])
    }
    for (var gi = 0; gi < groups.length; gi++) {
      var gsrc = groups[gi].source
      var logo = gsrc.artwork && gsrc.artwork.logo
      if (logo) r.push({ kind: "banner", image: logo, label: gsrc.title || "" })
      else if (groups.length > 1) r.push({ kind: "note", text: gsrc.title || "" })
      for (var j = 0; j < groups[gi].items.length; j++) {
        var p = groups[gi].items[j]
        if (p.compatible)
          r.push({ kind: "action", id: "game", pkg: p.id, label: p.title,
                   value: p.id === selected ? "Playing" : "Play" })
        else
          r.push({ kind: "gameoff", pkg: p.id, label: p.title + " (not supported)", value: "N/A" })
      }
    }
    // DOOM + DOOM II (appid 2280) contains every official release: nothing left to find or buy.
    var bundle = false
    for (var b = 0; b < pk.length; b++)
      if (Number(pk[b].source && typeof pk[b].source === "object" ? pk[b].source.appid : pk[b].appid) === 2280) bundle = true
    if (bundle) return
    if (!pk.length)
      r.push({ kind: "note", text: (cat.steam_found ? "No official DOOM games were found in your Steam libraries. " : "")
        + "The official id Software releases are sold as DOOM + DOOM II on Steam. Installed, they show up here and play from Steam's own folder; nothing is copied." })
    r.push({ kind: "action", id: "scan", label: "Find Steam games", value: "Scan" })
    r.push({ kind: "action", id: "store", label: "Get DOOM + DOOM II on Steam", value: "Store" })
  }

  // The two free games: Freedoom (installed with Live Doom) and id's DOOM shareware episode,
  // downloaded only after its terms are accepted here. Also the way back to Freedoom.
  function freeRows(r, s) {
    var ob = s.onboarding || ({})
    var free = root.freeSelected(s)
    r.push({ kind: "section", label: "Free games" })
    if (!ob.freedoom || ob.freedoom.available !== false)
      r.push({ kind: "action", id: "useFreedoom", label: "Freedoom", value: free === "freedoom" ? "Playing" : "Play" })
    if (ob.shareware === "present")
      r.push({ kind: "action", id: "useShareware", label: "DOOM shareware", value: free === "shareware" ? "Playing" : "Play" })
    else if (ob.shareware) {
      r.push({ kind: "action", id: "shareware", label: "Get the DOOM shareware episode", value: root.sharewareValue(ob) })
      root.termsRow(r, ob)
    }
  }

  function agentNote(ag, name) {
    if (!ag.connected)
      return ag.last_error ? name + " is not connected: " + ag.last_error
                           : "Starting " + name + ". AutoDoom drives until it connects."
    if (ag.owner === "agent") return name + " is driving. AutoDoom takes over if it stops."
    if (ag.owner === "human") return name + " is connected and waits while you play."
    if (ag.owner === "autodoom") return name + " is connected and letting AutoDoom drive."
    return name + " is connected and waiting for its turn."
  }

  function selectable(i) {
    var k = root.rows[i] ? root.rows[i].kind : ""
    return ["toggle", "choice", "slider", "action", "pwad", "option", "nav", "link"].indexOf(k) !== -1
  }
  function clampCursor() {
    if (root.cursor >= 0 && root.cursor < root.rows.length && root.selectable(root.cursor)) return
    for (var i = 0; i < root.rows.length; i++) if (root.selectable(i)) { root.cursor = i; return }
    root.cursor = -1
  }
  function move(dir) {
    var n = root.rows.length
    if (n === 0) return
    var i = root.cursor
    for (var step = 0; step < n; step++) {
      i = (i + dir + n) % n
      if (root.selectable(i)) { root.cursor = i; return }
    }
  }

  readonly property bool busy: root.chooserPending || root.choosing

  property bool switching: false     // one synchronous switch at a time: live background, game, scan
  property string openedUrl: ""      // test mode records the store link instead of opening it
  readonly property string storeUrl: "https://store.steampowered.com/app/2280/DOOM__DOOM_II/"
  function busyNote() { if (root.chooserPending) root.say("applying changes…", false) }

  function adjust(row, dir) {
    if (root.busy) return root.busyNote()
    if (!row || !root.settings) return
    if (row.extra) {                    // ←/→ move between the value and its button
      root.sub = dir > 0 ? 1 : 0
      return
    }
    if (row.kind === "nav") {           // ← Back (when there is one), → Next
      root.sub = dir < 0 && row.back ? 1 : 0
      return
    }
    if (row.kind === "option" || row.kind === "link") return
    if (row.kind === "toggle") {
      root.setValue(row.key, !root.getv(root.settings, row.key), false)
    } else if (row.kind === "choice") {
      var cur = root.getv(root.settings, row.key)
      var idx = 0
      for (var i = 0; i < row.options.length; i++) if (row.options[i][0] === cur) idx = i
      idx = (idx + dir + row.options.length) % row.options.length
      root.setValue(row.key, row.options[idx][0], false)
    } else if (row.kind === "slider") {
      var v = Number(root.getv(root.settings, row.key) || row.min)
      v = Math.max(row.min, Math.min(row.max, v + dir * row.step))
      root.setValue(row.key, Number(v.toFixed(row.digits)), true)
    } else if (row.kind === "pwad") {
      root.movePwad(row.index, dir)
    }
  }

  function sliderPick(row, fraction) {
    if (root.busy) return
    var v = row.min + fraction * (row.max - row.min)
    v = Math.round(v / row.step) * row.step
    root.setValue(row.key, Number(Math.max(row.min, Math.min(row.max, v)).toFixed(row.digits)), true)
  }

  function activate(row) {
    if (root.busy) return root.busyNote()
    if (!row) return
    if (row.extra && root.sub === 1) return root.doAction(row.extra.id)
    if (row.kind === "nav") return root.wizGo(root.sub === 1 && row.back ? -1 : 1)
    if (row.kind === "option" || row.kind === "link" || row.id === "wizSaver") return root.doAction(row.id, row)
    if (row.kind === "toggle") return root.setValue(row.key, !root.getv(root.settings, row.key), false)
    if (row.kind === "choice") return root.adjust(row, 1)
    if (row.kind === "action") root.doAction(row.id, row)
  }

  function doAction(id, row) {
    if (id === "preview" || id === "wizPreview") {
      var session = root.session
      if (root.leased) {
        root.dismiss()                   // queues `menu closed`
        root.afterSettings(function() {  // every edit lands first, then the preview
          if (root.opened || root.session !== session) return
          root.lifecycle("preview")      // ordered after that close
        })
      } else {                           // a window holds no lease: it stays, and the saver covers it
        root.say("previewing the screensaver. any input ends it", false)
        root.afterSettings(function() { if (root.opened && root.session === session) root.lifecycle("preview") })
      }
    } else if (id === "wizBg") {
      if (row && root.wizBackground !== row.choice) { root.wizBackground = row.choice; root.wizSave() }
    } else if (id === "wizSaver") {
      root.wizScreensaver = !root.wizScreensaver
      root.wizSave()
    } else if (id === "wizSkip") {
      root.wizSkip()
    } else if (id === "useFreedoom" || id === "useShareware") {
      var which = id === "useFreedoom" ? "freedoom" : "shareware"
      var label = which === "freedoom" ? "freedoom" : "doom shareware"
      if (root.freeSelected(root.settings) === which) return root.say(label + " is selected", false)
      root.switchJob(["use-game", which], "loading " + label, "could not start " + label, function(s) {
        root.cursorTo(function(r) { return r.id === id })
        return root.freeSelected(s) === which ? [label + " is selected", false]
                                              : ["selected " + label + "; settings still show another game", true]
      })
    } else if (id === "reroute") {
      root.say("rerouting the bot", false)
      root.run(["hint-bot"], function(code, out, err) {
        root.say(code === 0 ? "bot rerouted" : (err || out).trim(), code !== 0)
      })
    } else if (id === "uselive") {
      root.liveChoice(id, "use-live-wallpaper", "adding live doom to this theme", function(w) {
        return w.active ? ["live background on", false] : ["selected; waiting for the background to switch", true]
      })
    } else if (id === "removelive") {
      root.liveChoice(id, "remove-live-wallpaper", "removing live doom from this theme", function(w) {
        return !w.can_remove ? ["removed from this theme", false] : ["still listed in this theme", true]
      })
    } else if (id === "shareware") {
      var sw = root.settings && root.settings.onboarding || ({})
      if (sw.shareware === "downloading") return root.say("the doom shareware episode is downloading", false)
      if (!root.termsOpen) {                  // first press shows id's terms; only Accept downloads
        root.termsOpen = true
        root.cursorTo(function(r) { return r.id === "shareware" })
        return root.say("read the terms; accept is at the end", false)
      }
      // already open: take the reader to the Accept button (never accept from here)
      root.cursorTo(function(r) { return r.id === "acceptTerms" })
    } else if (id === "acceptTerms") {
      var sv = root.settings && root.settings.onboarding || ({})
      if (sv.shareware === "downloading") return root.say("the doom shareware episode is downloading", false)
      if (!root.termsOpen) return
      // a click that lands as the button appears, or a held key, would accept terms nobody read
      if (Date.now() - root.termsShownAt < 800) return root.say("read the terms above, then accept", false)
      root.switchJob(["download-shareware", "--accept-terms"], "downloading the doom shareware episode",
                     "the download failed", function(s) {
        root.termsOpen = false
        var ok = s.onboarding && s.onboarding.shareware === "present" && root.freeSelected(s) === "shareware"
        if (s.onboarding && s.onboarding.show) root.wizCursor()
        else root.cursorTo(function(r) { return r.id === "useShareware" })
        return ok ? ["doom shareware is selected", false] : ["download finished; the game has not switched yet", true]
      })
    } else if (id === "phobos") {
      root.applyPhobos()
    } else if (id === "game") {
      if (row) root.useGame(row.pkg, row.label)
    } else if (id === "scan") {
      root.scanSteam()
    } else if (id === "store") {
      root.openStore()
    } else if (id === "iwad") {
      root.choose("choose-iwad")
    } else if (id === "addwads") {
      root.choose("add-wads")
    }
  }

  function cursorTo(pred) {
    for (var i = 0; i < root.rows.length; i++) if (pred(root.rows[i])) { root.cursor = i; return true }
    return false
  }

  // A synchronous backend switch: one at a time, after queued edits, then a fresh settings
  // read; `done(settings)` returns [status, isError] once the rows have been rebuilt.
  function switchJob(args, working, failure, done) {
    if (root.switching) return root.say("still applying the previous change", false)   // never a silent click
    root.switching = true
    root.say(working, false)
    root.afterSettings(function() {
      root.run(args, function(code, out, err) {
        root.switching = false
        if (code !== 0) return root.say((err || failure).trim(), true)
        root.refresh(function() {
          var o = done(root.settings || ({}))
          root.say(o[0], o[1])
        })
      })
    })
  }

  // Select an official package (IWAD, PWADs and start map in one backend step).
  function useGame(pkg, title) {
    if (!pkg) return
    var name = String(title || pkg).toLowerCase()
    root.switchJob(["use-game", pkg], "loading " + name, "could not start " + name, function(s) {
      root.cursorTo(function(r) { return r.id === "game" && r.pkg === pkg })
      var ok = s.game && s.game.selected_package === pkg
      return ok ? ["playing " + name, false] : ["selected " + name + "; settings still show another game", true]
    })
  }

  // Look through Steam libraries again; never changes the current game.
  function scanSteam() {
    root.switchJob(["scan-steam"], "looking for steam games", "the steam scan failed", function(s) {
      // the Scan row goes away once the full DOOM + DOOM II bundle is found: land on its first game
      if (!root.cursorTo(function(r) { return r.id === "scan" })) root.cursorTo(function(r) { return r.id === "game" })
      var c = s.game && s.game.catalog || ({})
      var warn = c.warnings && c.warnings.length ? String(c.warnings[0]) : ""
      return [c.summary || warn || "scan finished", false]
    })
  }

  // The DOOM + DOOM II store page, only on an explicit click/Enter. The menu closes first so
  // the browser gets the keyboard; only Steam store links are opened.
  function openStore() {
    var url = String(root.settings && root.settings.game && root.settings.game.store_url || "")
    if (url.indexOf("https://store.steampowered.com/") !== 0) url = root.storeUrl
    root.dismiss()
    if (root.testMode) root.openedUrl = url
    else Qt.openUrlExternally(url)
  }

  // Add or remove this theme's live background. The backend returns once the
  // selection has changed, so a fresh `settings` shows the result; `outcome(w)`
  // maps the new wallpaper state to [status, isError]. One change at a time.
  function liveChoice(id, verb, working, outcome) {
    var w0 = root.settings && root.settings.wallpaper || ({})
    root.switchJob([verb], working, w0.remove_reason || "could not change the live background", function(s) {
      var w = s.wallpaper || ({})
      // Rows come and go with the state (Use/Remove), so a stale index could land
      // on the opposite action: stay on this one, or on its section if it is gone.
      if (!root.cursorTo(function(r) { return r.id === id }))
        root.cursorTo(function(r) { return r.key === "wallpaper.mode" })
      return outcome(w)
    })
  }

  function movePwad(index, dir) {
    if (root.busy) return
    var list = (root.settings && root.settings.game && root.settings.game.pwads || []).slice()
    var j = index + dir
    if (j < 0 || j >= list.length) return
    var t = list[index]; list[index] = list[j]; list[j] = t
    root.setValue("game.pwads", list, true)
    root.cursor = root.cursor + dir
  }
  function removePwad(index) {
    if (root.busy) return
    var list = (root.settings && root.settings.game && root.settings.game.pwads || []).slice()
    if (index < 0 || index >= list.length) return
    var gone = root.basename(list[index])
    list.splice(index, 1)
    root.setValue("game.pwads", list, true)
    root.say("removed " + gone, false)
    Qt.callLater(root.clampCursor)
  }

  function modeText(s) {
    var m = s && s.status ? s.status.mode : ""
    var map = s && s.status && s.status.map ? s.status.map + "  ·  " : ""
    var words = { bot: "bot playing", human: "you are playing", paused: "paused", screensaver: "screensaver" }
    return map + (words[m] || m || "")
  }
  // Header status: the game's state, or where the first-run guide is.
  function headLine(s) {
    var ob = s && s.onboarding
    if (!ob || !ob.show) return root.modeText(s)
    var steps = root.wizSteps(ob)
    if (steps.indexOf(root.wizStep) === -1) return "welcome"     // still reading where the guide was
    return "welcome  ·  step " + (steps.indexOf(root.wizStep) + 1) + " of " + steps.length
  }
  readonly property string hints: root.settings && root.settings.onboarding && root.settings.onboarding.show
    ? "esc close   ↑↓ ←→ select   enter choose"
    : "esc close   ↑↓ select   ←→ adjust   enter toggle"

  Timer {
    interval: 230      // the menu skull blinks at the game's rate (8 tics)
    repeat: true
    running: root.surfaceShown
    onTriggered: root.skullFrame = 1 - root.skullFrame
  }

  // ------------------------------------------------------------------ view
  // setSource() hands the host its root as an initial property, so the card inside never
  // exists with a null model (MenuCard also re-registers on change). The host only changes
  // while the menu is closed (open() picks it), so one card is ever live.
  Loader {
    id: windowLoader
    active: !root.testMode
    property string host: ""
    function use(h) {
      if (windowLoader.host === h) return
      windowLoader.host = h
      windowLoader.setSource(h === "window" ? "MenuFloat.qml" : "MenuWindow.qml", { root: root })
    }
    Component.onCompleted: if (!root.testMode) windowLoader.use("overlay")
  }
}
