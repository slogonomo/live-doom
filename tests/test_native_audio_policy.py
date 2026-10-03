# SPDX-License-Identifier: 0BSD
"""Exercise the shipped native audio-device policy with failure-injected stubs.

The actual function is extracted unchanged from the GPL engine patch. Tests need
only a C++ compiler; no mixer build, game data, audio server, or desktop is used.
The real mixer/device preservation proof is separate from these policy tests.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

CONTEXT = r'''
#include <atomic>
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

std::vector<std::string> events;
struct Gate {
    bool value = false;
    void store(bool next, std::memory_order) {
        value = next;
        events.push_back(next ? "gate-on" : "gate-off");
    }
} audioGate;
std::atomic<uint32_t> audioPeak{123};
bool audible = false, suspended = false;
int deviceSuspended = -1;
uint64_t audioMutedSince = 0, audioRetryAt = 0;
bool audioMutePending = false, audioReopenWarning = false;

bool mixerInitialized = true, physicalDetached = false, physicalPaused = false;
unsigned closeCalls = 0, openCalls = 0, stopCalls = 0, callbackCalls = 0;
unsigned reopenFailures = 0, musicCalls = 0, volumeCalls = 0;
unsigned staleSounds = 4;
int musicVolume = 37;
double musicPosition = 17.25;
std::string logs;

int Mix_QuerySpec(int *, unsigned short *, int *) {
    return mixerInitialized ? 1 : 0;
}
int Mix_LiveDoomDeviceSuspended() { return physicalDetached; }
void S_StopSounds(bool all) {
    assert(all);
    ++stopCalls;
    staleSounds = 0;
    events.push_back("stop-sfx");
}
void Mix_PauseAudio(int pause) {
    physicalPaused = pause;
    events.push_back(pause ? "pause" : "unpause");
    if(!pause && !physicalDetached) {
        // A callback is allowed immediately: the final gate and stale sound
        // cleanup must already be correct at this externally visible boundary.
        ++callbackCalls;
        assert(audioGate.value);
        assert(staleSounds == 0);
    }
}
int Mix_LiveDoomSuspendDevice(int detach) {
    if(detach) {
        ++closeCalls;
        events.push_back("detach");
        assert(!audioGate.value && staleSounds == 0);
        physicalDetached = true;
        return 0;
    }
    ++openCalls;
    events.push_back("reopen");
    assert(physicalPaused && staleSounds == 0 && !audioGate.value);
    if(reopenFailures) {
        --reopenFailures;
        return -1;
    }
    physicalDetached = false;
    return 0;
}
const char *SDL_GetError() { return "fake unavailable device"; }
int test_fprintf(FILE *, const char *format, ...) {
    char buffer[1024];
    va_list args;
    va_start(args, format);
    const int count = vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    assert(count >= 0 && count < static_cast<int>(sizeof(buffer)));
    logs += buffer;
    return count;
}
// Preferences and decoder ownership belong to the native game, never this
// policy. These sentinels fail if a future policy changes volume or restarts it.
int Mix_VolumeMusic(int next) { ++volumeCalls; musicVolume = next; return next; }
void I_SetMusicVolume(int next) { ++volumeCalls; musicVolume = next; }
void S_StopMusic() { ++musicCalls; musicPosition = 0; }
int Mix_HaltMusic() { ++musicCalls; musicPosition = 0; return 0; }

#define fprintf test_fprintf
'''

SCENARIOS = r'''
#undef fprintf
unsigned count(const std::string &text) {
    unsigned result = 0;
    size_t offset = 0;
    while((offset = logs.find(text, offset)) != std::string::npos) {
        ++result;
        offset += text.size();
    }
    return result;
}
void expectPreferencesUntouched() {
    assert(musicVolume == 37 && musicPosition == 17.25);
    assert(musicCalls == 0 && volumeCalls == 0);
}
void initializeAudible() {
    audible = true;
    staleSounds = 0;
    serviceAudioDevice(0);
    assert(audioGate.value && !physicalPaused && !physicalDetached);
    events.clear();
}
int main(int argc, char **argv) {
    assert(argc == 2);
    const std::string test = argv[1];
    if(test == "quiet-grace") {
        initializeAudible();
        staleSounds = 4;
        audible = false;
        serviceAudioDevice(100);
        assert(!audioGate.value && audioPeak.load() == 0);
        assert(!physicalDetached && !physicalPaused && closeCalls == 0 && stopCalls == 0);
        serviceAudioDevice(1599);
        assert(closeCalls == 0 && stopCalls == 0);
        serviceAudioDevice(1600);
        assert(physicalDetached && physicalPaused && closeCalls == 1 && stopCalls == 1);
        assert(!audioGate.value && audioPeak.load() == 0);
        for(uint64_t now = 1601; now < 10000; ++now) serviceAudioDevice(now);
        assert(closeCalls == 1 && stopCalls == 1);
        assert(count("released while inaudible") == 1);
    } else if(test == "paused-grace") {
        // Wall-clock zero is valid; no world tic or resumed simulation is
        // required to release a paused device after its grace period.
        audible = true;
        suspended = true;
        serviceAudioDevice(0);
        assert(physicalPaused && !physicalDetached && !audioGate.value);
        serviceAudioDevice(1499);
        assert(closeCalls == 0);
        serviceAudioDevice(1500);
        assert(closeCalls == 1 && physicalDetached);
        suspended = false;
        audible = false;
        serviceAudioDevice(5000);
        assert(physicalPaused && physicalDetached && openCalls == 0 && !audioGate.value);
    } else if(test == "brief-mute") {
        initializeAudible();
        audible = false;
        serviceAudioDevice(100);
        serviceAudioDevice(1500);
        assert(closeCalls == 0 && !audioGate.value);
        audible = true;
        serviceAudioDevice(1501);
        assert(audioGate.value && !audioMutePending);
        assert(closeCalls == 0 && openCalls == 0 && stopCalls == 0);
        audible = false;
        serviceAudioDevice(1600);
        serviceAudioDevice(3099);
        assert(closeCalls == 0);
        serviceAudioDevice(3100);
        assert(closeCalls == 1);
    } else if(test == "uninitialized") {
        mixerInitialized = false;
        audible = true;
        audioGate.value = true;
        serviceAudioDevice(10000);
        assert(!audioGate.value && audioPeak.load() == 0);
        assert(openCalls == 0 && closeCalls == 0 && stopCalls == 0);
        assert(events == std::vector<std::string>{"gate-off"});
        assert(logs.empty() && !audioMutePending);
    } else if(test == "resume-clean") {
        audible = true;
        physicalDetached = true;
        physicalPaused = false;
        deviceSuspended = 1;
        serviceAudioDevice(100);
        const std::vector<std::string> expected = {
            "pause", "stop-sfx", "reopen", "gate-on", "unpause"
        };
        assert(events == expected);
        assert(openCalls == 1 && closeCalls == 0 && stopCalls == 1);
        assert(callbackCalls == 1 && !physicalDetached && !physicalPaused && audioGate.value);
        serviceAudioDevice(101);
        assert(openCalls == 1 && stopCalls == 1 && callbackCalls == 1);
        assert(count("device resumed") == 1);
    } else if(test == "reopen-retry") {
        audible = true;
        physicalDetached = true;
        physicalPaused = true;
        deviceSuspended = 1;
        reopenFailures = 2;
        serviceAudioDevice(0);
        assert(openCalls == 1 && audioRetryAt == 2000 && audioReopenWarning);
        assert(!audioGate.value && audioPeak.load() == 0 && physicalDetached && physicalPaused);
        for(uint64_t now = 1; now < 2000; ++now) serviceAudioDevice(now);
        assert(openCalls == 1 && stopCalls == 1 && callbackCalls == 0);
        serviceAudioDevice(2000);
        assert(openCalls == 2 && audioRetryAt == 4000 && !audioGate.value);
        serviceAudioDevice(3999);
        assert(openCalls == 2);
        serviceAudioDevice(4000);
        assert(openCalls == 3 && !physicalDetached && !physicalPaused && audioGate.value);
        assert(audioRetryAt == 0 && !audioReopenWarning);
        assert(count("unavailable; retrying") == 1);
        assert(count("device resumed") == 1);
        assert(callbackCalls == 1);
        // A later independent outage may produce its own one-time warning.
        audible = false;
        serviceAudioDevice(5000);
        serviceAudioDevice(6500);
        assert(physicalDetached);
        reopenFailures = 1;
        audible = true;
        serviceAudioDevice(6501);
        assert(openCalls == 4 && !audioGate.value && physicalDetached);
        assert(count("unavailable; retrying") == 2);
    } else return 2;
    expectPreferencesUntouched();
}
'''


@unittest.skipUnless(shutil.which('c++'), 'A C++ compiler is required for the native audio policy regression')
class NativeAudioPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='live-doom-audio-policy-')
        cls.addClassCleanup(cls.work.cleanup)
        patch = (ROOT / 'patches/autodoom-desktop.patch').read_text()
        section = patch.split('+++ b/source/desktop_bridge.cpp\n', 1)[1].split('\ndiff --git ', 1)[0]
        source = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))
        policy = source[source.index('void serviceAudioDevice(uint64_t now)'):source.index('\nvoid applyMode()')]
        cpp = Path(cls.work.name) / 'audio-policy.cpp'
        cpp.write_text(CONTEXT + policy + SCENARIOS)
        cls.executable = cpp.with_suffix('')
        subprocess.run([shutil.which('c++'), '-std=c++11', '-O2', '-Wall', '-Wextra',
                        str(cpp), '-o', str(cls.executable)], check=True, capture_output=True, timeout=15)

    def scenario(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_running_quiet_device_closes_once_after_wall_clock_grace(self):
        self.scenario('quiet-grace')

    def test_paused_world_also_releases_device_after_grace(self):
        self.scenario('paused-grace')

    def test_brief_mute_cancels_close_and_next_mute_gets_fresh_grace(self):
        self.scenario('brief-mute')

    def test_no_initialized_audio_stays_silent_without_device_calls(self):
        self.scenario('uninitialized')

    def test_resume_clears_stale_sounds_and_sets_gate_before_callback(self):
        self.scenario('resume-clean')

    def test_failed_reopen_is_silent_retries_bounded_and_warns_once_per_outage(self):
        self.scenario('reopen-retry')


if __name__ == '__main__':
    unittest.main()
