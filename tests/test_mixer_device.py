#!/usr/bin/python3
# SPDX-License-Identifier: 0BSD
"""Compile the shipped mixer device boundary against failure-injecting SDL stubs.

This exercises the actual added/changed functions from the exported patch.
The retained playback objects are sentinels; the private PipeWire proof covers
real decoder playheads and physical stream removal/return.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/sdl-mixer-device.patch'


def postimage(relative):
    lines = []
    active = False
    in_hunk = False
    for line in PATCH.read_text().splitlines():
        if line.startswith('diff --git '):
            active = line == f'diff --git a/{relative} b/{relative}'
            in_hunk = False
        elif active and line.startswith('@@ '):
            in_hunk = True
        elif active and in_hunk and line.startswith((' ', '+')):
            lines.append(line[1:])
    return '\n'.join(lines)


def function(source, name):
    match = re.search(r'(?:static )?(?:int|void) ' + re.escape(name)
                      + r'\([^\n]*\)\n\{', source)
    if not match:
        raise AssertionError('Missing complete patched function: ' + name)
    start = source.index('{', match.start())
    depth = 0
    for position in range(start, len(source)):
        if source[position] == '{':
            depth += 1
        elif source[position] == '}':
            depth -= 1
            if not depth:
                return source[match.start():position + 1]
    raise AssertionError('Incomplete patched function: ' + name)


STUBS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
typedef uint32_t SDL_AudioDeviceID;
typedef struct {
    int freq;
    uint16_t format;
    uint8_t channels;
    uint16_t samples;
    void (*callback)(void *, uint8_t *, int);
    void *userdata;
} SDL_AudioSpec;
static int audio_opened;
static SDL_AudioSpec mixer;
static SDL_AudioDeviceID audio_device;
static int live_doom_audio_paused;
static int live_doom_device_detached;
static int opens, closes, pauses, locks, unlocks, asynchronous_pauses;
static int open_fails, mismatch;
static int last_pause, last_async_pause, last_changes;
static SDL_AudioDeviceID next_device = 100, last_closed;
static SDL_AudioSpec last_desired;
static char last_error[80];
static void *music_player = (void *)0x1234;
static void *music_hook = (void *)0x2345;
static void *postmix_hook = (void *)0x3456;
static void *channels[2] = {(void *)0x4567, (void *)0x5678};
static void *effects = (void *)0x6789;
static int music_volume = 37, channel_volume = 83;
static double music_position = 3.125;
static void mix_channels(void *userdata, uint8_t *samples, int size)
{
    (void)userdata; (void)samples; (void)size;
}
static void SDL_CloseAudioDevice(SDL_AudioDeviceID device)
{
    assert(device != 0);
    closes++;
    last_closed = device;
}
static SDL_AudioDeviceID SDL_OpenAudioDevice(const char *name, int capture,
        const SDL_AudioSpec *desired, SDL_AudioSpec *obtained, int changes)
{
    assert(name == NULL && capture == 0);
    last_desired = *desired;
    last_changes = changes;
    opens++;
    if (open_fails) return 0;
    *obtained = *desired;
    if (mismatch == 1) obtained->freq++;
    if (mismatch == 2) obtained->format++;
    if (mismatch == 3) obtained->channels++;
    if (mismatch == 4) obtained->samples++;
    return next_device++;
}
static void SDL_PauseAudioDevice(SDL_AudioDeviceID device, int paused)
{
    assert(device != 0);
    pauses++;
    last_pause = paused;
}
static void SDL_LockAudioDevice(SDL_AudioDeviceID device)
{
    assert(device != 0);
    locks++;
}
static void SDL_UnlockAudioDevice(SDL_AudioDeviceID device)
{
    assert(device != 0);
    unlocks++;
}
static int SDL_SetError(const char *message, ...)
{
    snprintf(last_error, sizeof(last_error), "%s", message);
    return -1;
}
static void pause_async_music(int paused)
{
    asynchronous_pauses++;
    last_async_pause = paused;
}
static void reset(void)
{
    audio_opened = 1;
    audio_device = 7;
    mixer.freq = 44100;
    mixer.format = 0x8010;
    mixer.channels = 2;
    mixer.samples = 2048;
    mixer.callback = mix_channels;
    mixer.userdata = (void *)0x7890;
    live_doom_audio_paused = 0;
    live_doom_device_detached = 0;
    opens = closes = pauses = locks = unlocks = asynchronous_pauses = 0;
    open_fails = mismatch = last_pause = last_async_pause = last_changes = 0;
    memset(&last_desired, 0, sizeof(last_desired));
    memset(last_error, 0, sizeof(last_error));
    last_closed = 0;
}
static void retained(void)
{
    assert(audio_opened == 1);
    assert(mixer.freq == 44100 && mixer.format == 0x8010);
    assert(mixer.channels == 2 && mixer.samples == 2048);
    assert(mixer.callback == mix_channels && mixer.userdata == (void *)0x7890);
    assert(music_player == (void *)0x1234 && music_hook == (void *)0x2345);
    assert(postmix_hook == (void *)0x3456 && effects == (void *)0x6789);
    assert(channels[0] == (void *)0x4567 && channels[1] == (void *)0x5678);
    assert(music_volume == 37 && channel_volume == 83);
    assert(music_position == 3.125);
}
'''

CASES = r'''
static void shutdown_tail(void)
{
    /* The edited physical-device tail of the upstream final close branch. */
    SHUTDOWN_TAIL
    audio_opened = 0;  /* Upstream decrements the final logical open count. */
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    reset();
    if (!strcmp(argv[1], "no-init")) {
        audio_opened = 0;
        audio_device = 0;
        assert(Mix_LiveDoomSuspendDevice(1) == -1);
        assert(Mix_LiveDoomSuspendDevice(0) == -1);
        assert(!Mix_LiveDoomDeviceSuspended());
        assert(opens == 0 && closes == 0 && pauses == 0);
        assert(strstr(last_error, "not initialized"));
    } else if (!strcmp(argv[1], "idempotent")) {
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(opens == 0 && closes == 0);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        assert(Mix_LiveDoomDeviceSuspended() && audio_device == 0);
        assert(opens == 0 && closes == 1 && last_closed == 7);
        retained();
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(!Mix_LiveDoomDeviceSuspended() && audio_device == 100);
        assert(opens == 1 && closes == 1 && pauses == 1 && last_pause == 0);
        retained();
    } else if (!strcmp(argv[1], "exact-spec")) {
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(last_desired.freq == mixer.freq && last_desired.format == mixer.format);
        assert(last_desired.channels == mixer.channels && last_desired.samples == mixer.samples);
        assert(last_desired.callback == mix_channels && last_desired.userdata == NULL);
        assert(last_changes == 0);
        retained();
    } else if (!strcmp(argv[1], "pause-state")) {
        Mix_PauseAudio(1);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(last_pause == 1 && live_doom_audio_paused == 1);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        pauses = locks = unlocks = 0;
        Mix_PauseAudio(0);
        assert(pauses == 0 && locks == 0 && unlocks == 0);
        assert(last_async_pause == 0 && asynchronous_pauses == 2);
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(last_pause == 0);
        retained();
    } else if (!strcmp(argv[1], "reopen-fail")) {
        Mix_PauseAudio(1);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        open_fails = 1;
        int old_pauses = pauses;
        assert(Mix_LiveDoomSuspendDevice(0) == -1);
        assert(audio_device == 0 && Mix_LiveDoomDeviceSuspended());
        assert(pauses == old_pauses && live_doom_audio_paused == 1);
        assert(closes == 1 && opens == 1);
        retained();
        open_fails = 0;
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(audio_device == 100 && last_pause == 1 && opens == 2);
        retained();
    } else if (!strncmp(argv[1], "mismatch-", 9)) {
        mismatch = atoi(argv[1] + 9);
        assert(mismatch >= 1 && mismatch <= 4);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == -1);
        assert(audio_device == 0 && Mix_LiveDoomDeviceSuspended());
        assert(closes == 2 && last_closed == 100 && pauses == 0);
        assert(strstr(last_error, "format changed"));
        retained();
        mismatch = 0;
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        assert(audio_device == 101 && last_pause == 0);
        retained();
    } else if (!strcmp(argv[1], "lock-detached")) {
        Mix_LockAudio(); Mix_UnlockAudio();
        assert(locks == 1 && unlocks == 1);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        Mix_LockAudio(); Mix_UnlockAudio();
        assert(locks == 1 && unlocks == 1);
        assert(Mix_LiveDoomSuspendDevice(0) == 0);
        Mix_LockAudio(); Mix_UnlockAudio();
        assert(locks == 2 && unlocks == 2);
        retained();
    } else if (!strcmp(argv[1], "shutdown-detached")) {
        Mix_PauseAudio(1);
        assert(Mix_LiveDoomSuspendDevice(1) == 0);
        shutdown_tail();
        assert(closes == 1 && audio_device == 0);
        assert(!Mix_LiveDoomDeviceSuspended() && live_doom_audio_paused == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == -1 && opens == 0);
    } else if (!strcmp(argv[1], "shutdown-attached")) {
        shutdown_tail();
        assert(closes == 1 && last_closed == 7 && audio_device == 0);
        assert(!Mix_LiveDoomDeviceSuspended() && live_doom_audio_paused == 0);
        assert(Mix_LiveDoomSuspendDevice(0) == -1 && opens == 0);
    } else {
        assert(!"unknown case");
    }
    puts("PASS");
    return 0;
}
'''


class MixerDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('C compiler unavailable')
        source = postimage('src/mixer.c')
        changed = '\n\n'.join(function(source, name) for name in (
            'live_doom_close_device', 'Mix_LockAudio', 'Mix_UnlockAudio',
            'Mix_PauseAudio', 'Mix_LiveDoomSuspendDevice',
            'Mix_LiveDoomDeviceSuspended'))
        tail = re.search(r'            live_doom_close_device\(\);\n'
                         r'            live_doom_device_detached = 0;\n'
                         r'            live_doom_audio_paused = 0;', source)
        if not tail:
            raise AssertionError('Missing final-close device cleanup in shipped patch')
        cls.scratch = tempfile.TemporaryDirectory(prefix='live-doom-mixer-test-')
        cls.addClassCleanup(cls.scratch.cleanup)
        directory = Path(cls.scratch.name)
        code = directory / 'mixer-device.c'
        code.write_text(STUBS + '\n' + changed + '\n'
                        + CASES.replace('SHUTDOWN_TAIL', tail.group(0)))
        cls.binary = directory / 'mixer-device'
        result = subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra',
                                 '-Werror', str(code), '-o', str(cls.binary)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS')

    def test_uninitialized_mixer_is_not_opened_or_closed(self):
        self.run_case('no-init')

    def test_detach_resume_are_idempotent_and_retain_playback_state(self):
        self.run_case('idempotent')

    def test_resume_requests_the_exact_spec_and_callback(self):
        self.run_case('exact-spec')

    def test_pause_changes_while_detached_apply_on_resume(self):
        self.run_case('pause-state')

    def test_failed_reopen_remains_detached_and_paused_then_can_retry(self):
        self.run_case('reopen-fail')

    def test_changed_spec_is_closed_and_rejected_without_mutating_mixer(self):
        for field in range(1, 5):
            with self.subTest(field=field):
                self.run_case('mismatch-' + str(field))

    def test_detached_mixer_never_locks_device_zero(self):
        self.run_case('lock-detached')

    def test_shutdown_while_detached_resets_device_state_without_double_close(self):
        self.run_case('shutdown-detached')

    def test_shutdown_while_attached_closes_once_and_resets_device_state(self):
        self.run_case('shutdown-attached')

    def test_altered_sources_and_api_serialization_are_documented(self):
        patch = PATCH.read_text()
        self.assertEqual(patch.count('Altered by Live Doom (2026)'), 2)
        self.assertIn('original zlib notice above', patch)
        self.assertIn('Call only on the main thread', patch)
        self.assertIn('with no Mix_LockAudio() lock held', patch)
        self.assertNotIn('PRIVATE proof instrumentation', patch)
        self.assertNotIn('feasibility prototype', patch)


if __name__ == '__main__':
    unittest.main()
