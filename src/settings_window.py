# SPDX-License-Identifier: 0BSD
"""Immediate GTK settings, used when the Omarchy menu overlay is unavailable."""
from __future__ import annotations

import json
import threading


def show():
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib, Gdk, Pango
    from controller import config_for, settings_path, request, validate_config, validate_wad, manual_game_changes

    cfg = config_for(settings_path())
    window = Gtk.Window(title='Live Doom')
    window.set_default_size(620, 720)
    window.set_border_width(22)
    window.connect('destroy', Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    window.add(box)
    box.pack_start(Gtk.Label(label='DOOM // DESKTOP', xalign=0), False, False, 0)
    box.pack_start(Gtk.Label(label='Click the desktop to play. Esc opens Doom menus; F12 returns to the bot.', xalign=0), False, False, 0)
    status = Gtk.Label(xalign=0)
    status.set_line_wrap(True)
    status.set_name('settings-status')
    pending = {}
    busy = False
    timer = None
    closing = False

    def close(*_):
        nonlocal closing, timer
        closing = True
        window.hide()
        if timer is not None:
            GLib.source_remove(timer)
            timer = None
        if pending and not busy:
            flush()
        elif not busy:
            window.destroy()
        return True

    window.connect('delete-event', close)
    window.connect('key-press-event', lambda _, event: close() if event.keyval == Gdk.KEY_Escape else False)

    def flush():
        nonlocal busy, timer
        if busy:
            return GLib.SOURCE_CONTINUE
        timer = None
        changes = manual_game_changes(pending)
        pending.clear()
        if not changes:
            return GLib.SOURCE_REMOVE
        try:
            validate_config(cfg | changes)
        except (OSError, ValueError, TypeError) as exc:
            status.set_text(str(exc))
            if closing:
                window.destroy()
            return GLib.SOURCE_REMOVE
        busy = True
        status.set_text('Updating…')

        def finish(answer):
            nonlocal busy, refreshing_games
            busy = False
            if answer == 'OK':
                cfg.update(changes)
                if 'game_package' in changes and changes['game_package'] is None:
                    refreshing_games = True
                    official.set_active_id('custom')
                    refreshing_games = False
            status.set_text('Settings saved.' if answer == 'OK' else answer)
            if closing:
                if pending:
                    flush()
                else:
                    window.destroy()
            return GLib.SOURCE_REMOVE

        def send():
            try:
                # Package identity is computed by the backend; raw configure
                # does not accept that read-only field.
                supplied = {key: value for key, value in changes.items() if key != 'game_package'}
                answer = request('configure ' + json.dumps(supplied), timeout=130)
            except (OSError, ValueError) as exc:
                answer = str(exc)
            GLib.idle_add(finish, answer)

        threading.Thread(target=send, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def update(changes):
        nonlocal timer
        pending.update(changes)
        if timer is None:
            timer = GLib.timeout_add(150, flush)

    def checkbox(label, key, parent):
        check = Gtk.CheckButton(label=label)
        check.set_active(cfg[key])
        check.set_name(key)
        check.connect('toggled', lambda widget: update({key: widget.get_active()}))
        parent.pack_start(check, False, False, 0)
        return check

    row = Gtk.Box(spacing=12)
    row.pack_start(Gtk.Label(label='Sound:'), False, False, 0)
    checkbox('While playing', 'playing_sound', row)
    checkbox('Screensaver', 'screensaver_sound', row)
    checkbox('On idle desktop', 'desktop_sound', row)
    box.pack_start(row, False, False, 0)
    row = Gtk.Box(spacing=12)
    row.pack_start(Gtk.Label(label='Live Background:'), False, False, 0)
    empty = Gtk.RadioButton.new_with_label_from_widget(None, 'Empty desktop only')
    all_desktops = Gtk.RadioButton.new_with_label_from_widget(empty, 'All desktops')
    for widget, value in ((empty, 'empty'), (all_desktops, 'all')):
        widget.set_active(cfg['wallpaper_mode'] == value)
        widget.connect('toggled', lambda button, mode: update({'wallpaper_mode': mode}) if button.get_active() else None, value)
        row.pack_start(widget, False, False, 0)
    box.pack_start(row, False, False, 0)

    def action(command, message):
        def send():
            try:
                answer = request(command, timeout=130)
            except OSError as exc:
                answer = str(exc)
            GLib.idle_add(lambda: (status.set_text(message if answer == 'OK' else answer), GLib.SOURCE_REMOVE)[1])
        threading.Thread(target=send, daemon=True).start()

    row = Gtk.Box(spacing=12)
    checkbox('Live screensaver', 'screensaver_enabled', row)
    preview = Gtk.Button(label='Preview')
    preview.connect('clicked', lambda *_: action('preview-screensaver', 'Screensaver preview started. Move the mouse to dismiss.'))
    row.pack_start(preview, False, False, 0)
    box.pack_start(row, False, False, 0)

    def slider(label, key, lower, upper, step, digits):
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label=label), False, False, 0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lower, upper, step)
        scale.set_value(cfg[key])
        scale.set_digits(digits)
        scale.set_name(key)
        scale.connect('value-changed', lambda widget: update({key: round(widget.get_value(), digits) if digits else int(widget.get_value())}))
        row.pack_start(scale, True, True, 0)
        box.pack_start(row, False, False, 0)

    slider('Blur when paused (%)', 'pause_blur', 0, 100, 1, 0)
    slider('Mouse sensitivity', 'mouse_sensitivity', 0.1, 4, 0.1, 1)
    checkbox('Full mouselook (look up and down)', 'mouselook', box)
    steam_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    official = Gtk.ComboBoxText()
    official.set_name('steam-game')
    steam_note = Gtk.Label(xalign=0)
    steam_note.set_line_wrap(True)
    refreshing_games = False
    game_jobs = []
    game_timer = None
    chooser = model = None

    def refresh_games(value):
        nonlocal refreshing_games
        game = value.get('game', {})
        catalog = game.get('catalog', {})
        refreshing_games = True
        official.remove_all()
        official.append('custom', 'Custom IWAD and WADs')
        for item in catalog.get('packages', []):
            if item.get('compatible'):
                official.append(item['id'], item['title'])
        official.set_active_id(game.get('selected_package') or 'custom')
        if chooser is not None and game.get('iwad'):
            chooser.set_filename(game['iwad'])
        if model is not None:
            model.clear()
            for path in game.get('pwads', []):
                model.append([path])
        refreshing_games = False
        unavailable = [item['title'] + ': ' + item.get('reason', 'Unavailable')
                       for item in catalog.get('packages', []) if not item.get('compatible')]
        steam_note.set_text(catalog.get('summary', 'Find games installed through Steam.')
                            + ('\n' + '\n'.join(unavailable) if unavailable else ''))

    def pump_game_jobs():
        nonlocal busy, game_timer
        if closing or not game_jobs:
            game_timer = None
            return GLib.SOURCE_REMOVE
        if busy or pending:
            if pending and not busy:
                flush()
            return GLib.SOURCE_CONTINUE
        busy = True
        command = game_jobs.pop(0)
        status.set_text('Finding Steam games…' if command == 'scan-steam' else 'Selecting game…')

        def finish(answer):
            nonlocal busy
            busy = False
            if answer.startswith('ERR'):
                status.set_text(answer)
            else:
                try:
                    value = json.loads(answer)
                    game = value['game']
                    cfg.update(iwad=game['iwad'], pwads=game['pwads'], start_map=game.get('start_map'),
                               game_package=game.get('selected_package'))
                    refresh_games(value)
                    status.set_text(game['catalog'].get('summary', 'Game selected.'))
                except (ValueError, KeyError, TypeError):
                    status.set_text('Unable to read the game settings. Reopen this window to refresh.')
            if closing:
                if pending:
                    flush()
                else:
                    window.destroy()
            elif pending:
                flush()
            return GLib.SOURCE_REMOVE

        def send():
            try:
                answer = request(command, timeout=130)
            except (OSError, ValueError) as error:
                answer = 'ERR ' + str(error)
            GLib.idle_add(finish, answer)

        threading.Thread(target=send, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def game_action(command):
        nonlocal game_timer
        if not game_jobs or game_jobs[-1] != command:
            game_jobs.append(command)
        if game_timer is None:
            game_timer = GLib.timeout_add(150, pump_game_jobs)

    official.connect('changed', lambda widget: game_action('use-game ' + widget.get_active_id())
                     if not refreshing_games and widget.get_active_id() not in (None, 'custom') else None)
    scan = Gtk.Button(label='Find Steam games')
    scan.connect('clicked', lambda *_: game_action('scan-steam'))
    steam_box.pack_start(Gtk.Label(label='Official Steam games', xalign=0), False, False, 0)
    steam_box.pack_start(official, False, False, 0)
    steam_box.pack_start(scan, False, False, 0)
    steam_box.pack_start(steam_note, False, False, 0)
    steam_box.pack_start(Gtk.LinkButton.new_with_label(
        'https://store.steampowered.com/app/2280/DOOM__DOOM_II/', 'Get DOOM + DOOM II on Steam'),
        False, False, 0)
    box.pack_start(steam_box, False, False, 0)
    try:
        refresh_games(json.loads(request('settings-json')))
    except (OSError, ValueError):
        refresh_games({})
    try:
        agents = json.loads(request('settings-json')).get('agent', {}).get('available', [])
    except (OSError, ValueError):
        agents = []
    if agents:
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label='Driver'), False, False, 0)
        driver = Gtk.ComboBoxText()
        driver.append('autodoom', 'AutoDoom')
        for agent in agents:
            driver.append(agent['id'], agent['name'])
        driver.set_active_id(cfg['agent_selected'])
        driver.connect('changed', lambda widget: update({'agent_selected': widget.get_active_id()}) if widget.get_active_id() else None)
        row.pack_start(driver, True, True, 0)
        box.pack_start(row, False, False, 0)
    chooser = Gtk.FileChooserButton(title='Choose a Doom or Doom II IWAD')
    wad_filter = Gtk.FileFilter()
    wad_filter.set_name('Doom WAD files')
    for pattern in ('*.wad', '*.WAD'):
        wad_filter.add_pattern(pattern)
    chooser.add_filter(wad_filter)
    chooser.set_filename(cfg['iwad'])
    box.pack_start(Gtk.Label(label='Game file (IWAD)', xalign=0), False, False, 0)
    box.pack_start(chooser, False, False, 0)
    box.pack_start(Gtk.Label(label='Additional levels and mods (loaded in order)', xalign=0), False, False, 0)
    model = Gtk.ListStore(str)
    for path in cfg['pwads']:
        model.append([path])
    view = Gtk.TreeView(model=model)
    view.set_headers_visible(False)
    view.set_tooltip_column(0)
    renderer = Gtk.CellRendererText()
    renderer.set_property('ellipsize', Pango.EllipsizeMode.MIDDLE)
    view.append_column(Gtk.TreeViewColumn('WAD file', renderer, text=0))
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_min_content_height(80)
    scrolled.add(view)
    box.pack_start(scrolled, True, True, 0)
    row = Gtk.Box(spacing=8)
    buttons = {name: Gtk.Button(label=name) for name in ('Add WADs…', 'Remove', 'Move up', 'Move down')}
    for button in buttons.values():
        row.pack_start(button, False, False, 0)
    box.pack_start(row, False, False, 0)

    def wad_changed(*_):
        if not refreshing_games:
            update({'pwads': [entry[0] for entry in model]})

    for signal in ('row-inserted', 'row-changed', 'row-deleted', 'rows-reordered'):
        model.connect(signal, wad_changed)
    chooser.connect('file-set', lambda widget: update({'iwad': widget.get_filename(), 'pwads': [entry[0] for entry in model]}) if not refreshing_games and widget.get_filename() else None)

    def selection_changed(*_):
        selected = view.get_selection().get_selected()[1]
        index = model.get_path(selected).get_indices()[0] if selected is not None else -1
        buttons['Remove'].set_sensitive(index >= 0)
        buttons['Move up'].set_sensitive(index > 0)
        buttons['Move down'].set_sensitive(index >= 0 and index < len(model) - 1)
    view.get_selection().connect('changed', selection_changed)
    selection_changed()

    def add_levels(*_):
        dialog = Gtk.FileChooserDialog(title='Add Doom level or mod WADs', parent=window, action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.ACCEPT)
        dialog.set_select_multiple(True)
        dialog.add_filter(wad_filter)
        try:
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                paths = [str(validate_wad(path)) for path in dialog.get_filenames()]
                existing = {entry[0] for entry in model}
                for path in paths:
                    if path not in existing:
                        model.append([path])
                        existing.add(path)
        except (OSError, ValueError) as exc:
            status.set_text(str(exc))
        finally:
            dialog.destroy()

    def remove_level(*_):
        selected = view.get_selection().get_selected()[1]
        if selected is not None:
            model.remove(selected)

    def move_level(direction):
        selected = view.get_selection().get_selected()[1]
        if selected is not None:
            adjacent = model.iter_previous(selected) if direction < 0 else model.iter_next(selected)
            if adjacent is not None:
                model.swap(selected, adjacent)
                selection_changed()

    buttons['Add WADs…'].connect('clicked', add_levels)
    buttons['Remove'].connect('clicked', remove_level)
    buttons['Move up'].connect('clicked', lambda *_: move_level(-1))
    buttons['Move down'].connect('clicked', lambda *_: move_level(1))
    reroute = Gtk.Button(label='Reroute bot')
    reroute.connect('clicked', lambda *_: action('hint-bot', 'The bot is looking for another route. Game progress is preserved.'))
    box.pack_start(status, False, False, 0)
    box.pack_start(reroute, False, False, 0)
    window.show_all()
    Gtk.main()
    if timer is not None:
        GLib.source_remove(timer)
    if game_timer is not None:
        GLib.source_remove(game_timer)
