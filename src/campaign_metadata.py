# SPDX-License-Identifier: 0BSD
"""Original Eternity metadata for selectable installed Doom expansions.

This creates only a tiny PWAD with routing/boss flags and an original ending
sentence. It contains no commercial map, image, music, or story text. Load it
after the installed campaign PWAD, then set native ``campaign START_MAP``.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import struct
import tempfile

MAP_NAME = re.compile(r'(?:E[1-9]M[1-9]|MAP[0-9]{2})\Z')
MASTER_STARTS = {
    'attack': 'MAP01', 'blacktwr': 'MAP25', 'bloodsea': 'MAP07',
    'canyon': 'MAP01', 'catwalk': 'MAP01', 'combine': 'MAP01',
    'fistula': 'MAP01', 'garrison': 'MAP01', 'geryon': 'MAP08',
    'manor': 'MAP01', 'mephisto': 'MAP07', 'minos': 'MAP05',
    'nessus': 'MAP07', 'paradox': 'MAP01', 'subspace': 'MAP01',
    'subterra': 'MAP01', 'teeth': 'MAP31', 'ttrap': 'MAP01',
    'vesperas': 'MAP09', 'virgil': 'MAP03',
}


def _kind(package_id):
    if package_id is None:
        return None
    if not isinstance(package_id, str):
        raise ValueError('Invalid campaign package')
    kind = package_id.removeprefix('steam-')
    if kind == 'master':
        kind = 'masterlevels'
    elif kind.startswith('master-'):
        kind = 'master:' + kind[7:]
    return kind


def _wad(lumps):
    payload, entries = bytearray(), bytearray()
    for name, data in lumps:
        entries.extend(struct.pack('<ii8s', 12 + len(payload), len(data), name.encode('ascii')))
        payload.extend(data)
    return struct.pack('<4sii', b'PWAD', len(lumps), 12 + len(payload)) + payload + entries


def campaign_bytes(package_id, start_map, pwads):
    """Return deterministic original metadata, or None for a base/custom game."""
    kind = _kind(package_id)
    if kind is None or kind in ('doom', 'doom2', 'tnt', 'plutonia'):
        return None
    starts = {'nerve': 'MAP01', 'masterlevels': 'MAP01', 'sigil': 'E5M1', 'sigil2': 'E6M1'}
    legacy = kind.partition(':')[2] if kind.startswith('master:') else None
    if legacy in MASTER_STARTS:
        expected, basename = MASTER_STARTS[legacy], legacy + '.wad'
    elif kind in starts:
        expected = starts[kind]
        basename = ('masterlevels' if kind == 'masterlevels' else kind) + '.wad'
    else:
        raise ValueError('Unsupported campaign package')
    if not isinstance(start_map, str) or not MAP_NAME.fullmatch(start_map) or start_map != expected:
        raise ValueError('Campaign start map does not match its installed package')
    if len(pwads) != 1 or Path(pwads[0]).name.lower() != basename:
        raise ValueError('Campaign adapter requires its single installed PWAD')
    if kind == 'sigil' or kind == 'sigil2':
        episode = 5 if kind == 'sigil' else 6
        maps = [f'E{episode}M{i}' for i in range(1, 10)]
        terminal, secret_from = maps[7], maps[5 if episode == 5 else 2]
        secret_return = maps[6 if episode == 5 else 3]
    elif kind == 'nerve':
        maps = [f'MAP{i:02d}' for i in range(1, 10)]
        terminal, secret_from, secret_return = 'MAP08', 'MAP04', 'MAP05'
    elif kind == 'masterlevels':
        maps = [f'MAP{i:02d}' for i in range(1, 22)]
        terminal, secret_from, secret_return = 'MAP20', 'MAP18', 'MAP19'
    else:
        maps = ['MAP31', 'MAP32'] if legacy == 'teeth' else [expected]
        terminal, secret_from, secret_return = maps[-1], 'MAP31' if legacy == 'teeth' else None, None
    sections = []
    for index, name in enumerate(maps):
        values = [('killfinale', 'true')]
        if legacy is None:
            # Suppress inherited Doom II MAP07 behavior; re-enable the
            # compilation's actual relocated vanilla boss actions below.
            values += [('boss-specials', '0')]
        if name == 'MAP19' and kind == 'masterlevels':
            values[-1] = ('boss-specials', 'MAP07_1')
        elif name == 'MAP20' and kind == 'masterlevels':
            values[-1] = ('boss-specials', 'MAP07_1 | MAP07_2')
        if index + 1 < len(maps):
            values += [('nextlevel', maps[index + 1])]
        if name == maps[-1] and secret_return:
            values += [('nextlevel', secret_return)]
        if name == secret_from:
            values += [('nextsecret', maps[-1])]
        ends = name == terminal or (legacy is not None and name == expected)
        if ends:
            values += [('killfinale', 'false'), ('finale-normal', 'true'), ('finale-secret', 'false'),
                       ('intertext', 'DDEND'), ('inter-backdrop', 'FLOOR4_8'),
                       ('intermusic', 'D_VICTOR' if kind.startswith('sigil') else 'D_READ_M'),
                       ('endofgame', 'true'),
                       ('finaletype', 'doom_credits' if kind.startswith('sigil') else 'text')]
        sections.append('[' + name + ']\n' + ''.join(f'{key} = {value}\n' for key, value in values))
    # EMAPINFO values are single assignments: duplicate killfinale overwrites
    # the earlier default through the parser's setString semantics.
    text = ('# Original Doom Desktop campaign compatibility metadata v1\n'
            + '\n'.join(sections)).encode('ascii')
    return _wad([('EMAPINFO', text), ('DDEND', b'Campaign complete.\n')])


def prepare_campaign(package_id: str | None, start_map: str | None,
                     pwads: list[str | Path], destination: Path) -> Path | None:
    """Atomically write a metadata PWAD to the supplied destination filename."""
    data = campaign_bytes(package_id, start_map, pwads)
    if data is None:
        return None
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.campaign-', suffix='.tmp', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
