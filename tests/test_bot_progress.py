# SPDX-License-Identifier: 0BSD
"""Compile the shipped native watchdog and exercise navigation regressions.

The engine-facing context is stubbed; the detector itself is extracted unchanged
from the GPL engine patch. No game assets, native build, or desktop are needed.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

CONTEXT = r'''
#include <bitset>
#include <string>
#include <cstdint>
#include <climits>
#include <cassert>
struct Mobj { int32_t x = 0, y = 0; } actor;
struct player_t {
    Mobj *mo = &actor;
    int health = 100, killcount = 0, itemcount = 0, secretcount = 0;
    uint32_t weapons = 3, keys = 0;
} players[1];
struct Bot { unsigned calls = 0; void mapInit() { ++calls; } } bots[1];
constexpr int GS_LEVEL = 0, NUMWEAPONS = 9;
int consoleplayer = 0, gamestate = GS_LEVEL, leveltime = 0;
const char *gamemapname = "MAP01";
const void *botMap = &actor;
uint32_t owner = 0, replans = 0, stuckEvents = 0;
bool suspended = false, paused = false;
const unsigned *E_WeaponForDEHNum(unsigned i) {
    static unsigned indices[NUMWEAPONS] = {0,1,2,3,4,5,6,7,8};
    return &indices[i];
}
bool E_PlayerOwnsWeapon(const player_t *p, const unsigned *w) { return p->weapons & (1U << *w); }
#define ARTI_BLUECARD "0"
#define ARTI_YELLOWCARD "1"
#define ARTI_REDCARD "2"
#define ARTI_BLUESKULL "3"
#define ARTI_YELLOWSKULL "4"
#define ARTI_REDSKULL "5"
int E_GetItemOwnedAmountName(const player_t *p, const char *key) { return !!(p->keys & (1U << (*key-'0'))); }
'''

SCENARIOS = r'''
void tick(int x = 0, int y = 0) {
    actor.x = x * 65536; actor.y = y * 65536;
    ++leveltime; watchBotProgress();
}
void repeat(unsigned n, int x = 0, int y = 0) { while(n--) tick(x,y); }
int main(int argc, char **argv) {
    assert(argc == 2);
    std::string test = argv[1];
    if(test == "boundary-jitter") {
        tick(127); tick(128);
        for(unsigned i = 0; i < 874; ++i) tick(i%2 ? 127 : 128);
        assert(stuckEvents == 0);
        tick(127); assert(stuckEvents == 1 && replans == 1 && bots[0].calls == 1);
        assert(players[0].health == 100 && leveltime == 877);
    } else if(test == "closed-loop") {
        const int points[][2] = {{-512,-512},{512,-512},{512,512},{-512,512}};
        for(auto &p : points) tick(p[0],p[1]);
        for(unsigned i = 0; i < 1750; ++i) {
            auto &p = points[i%4]; tick(p[0],p[1]);
            if(i == 873) assert(stuckEvents == 0);
            if(i == 874) assert(stuckEvents == 1);
        }
        assert(stuckEvents == 2 && replans == 2 && bots[0].calls == 2);
        assert(players[0].weapons == 3 && players[0].health == 100);
    } else if(test == "meaningful-progress") {
        tick(); repeat(874); assert(stuckEvents == 0);
        tick(128); repeat(874,128); assert(stuckEvents == 0);
        ++players[0].killcount; tick(128); repeat(874,128);
        ++players[0].itemcount; tick(128); repeat(874,128);
        ++players[0].secretcount; tick(128); repeat(874,128);
        players[0].weapons |= 4; tick(128); repeat(874,128);
        players[0].keys |= 1; tick(128); repeat(874,128);
        assert(stuckEvents == 0); tick(128); assert(stuckEvents == 1);
        assert(players[0].killcount == 1 && players[0].itemcount == 1 && players[0].secretcount == 1);
    } else if(test == "ownership-pauses") {
        tick(); repeat(100);
        owner = 1; repeat(5000); owner = 2; repeat(5000);
        owner = 0; suspended = true; repeat(5000);
        suspended = false; paused = true; repeat(5000);
        assert(stuckEvents == 0 && bots[0].calls == 0);
        paused = false; repeat(774); assert(stuckEvents == 0);
        tick(); assert(stuckEvents == 1);
    } else if(test == "world-epochs") {
        tick(); repeat(874); gamemapname = "MAP02"; tick();
        repeat(874); assert(stuckEvents == 0);
        leveltime = 0; tick(); repeat(874); assert(stuckEvents == 0);
        owner = 2; leveltime = 0; tick(); repeat(5000);
        owner = 0; tick(); repeat(874); assert(stuckEvents == 0);
        players[0].health = 0; tick(); players[0].health = 100; tick();
        repeat(874); assert(stuckEvents == 0);
        gamestate = 1; tick(); gamestate = GS_LEVEL; tick();
        repeat(874); assert(stuckEvents == 0);
        Mobj replacement; players[0].mo = &replacement; tick();
        repeat(874); assert(stuckEvents == 0); tick(); assert(stuckEvents == 1);
    } else if(test == "coordinate-bounds") {
        assert(progressCell(INT32_MIN) == 0 && progressCell(INT32_MAX) == 511);
        assert(progressCell(-129*65536) == 254);
        assert(progressCell(-128*65536) == 255 && progressCell(-65536) == 255);
        assert(progressCell(0) == 256 && progressCell(127*65536) == 256);
        assert(progressCell(128*65536) == 257);
        actor.x = INT32_MIN; actor.y = INT32_MAX; ++leveltime; watchBotProgress();
        actor.x = INT32_MAX; actor.y = INT32_MIN; ++leveltime; watchBotProgress();
        repeat(875); assert(stuckEvents == 0);
    } else if(test == "unchanged-world-tic") {
        tick(); repeat(874);
        for(unsigned i = 0; i < 5000; ++i) watchBotProgress();
        assert(stuckEvents == 0); tick(); assert(stuckEvents == 1);
    } else return 2;
}
'''


@unittest.skipUnless(shutil.which('c++'), 'A C++ compiler is required for the native watchdog regression')
class BotProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='live-doom-progress-')
        cls.addClassCleanup(cls.work.cleanup)
        patch = (ROOT / 'patches/autodoom-desktop.patch').read_text()
        section = patch.split('+++ b/source/desktop_bridge.cpp\n', 1)[1].split('\ndiff --git ', 1)[0]
        source = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))
        detector = source[source.index('struct ProgressWatch {'):source.index('\nvoid stopSignal')]
        cpp = Path(cls.work.name) / 'progress.cpp'
        cpp.write_text(CONTEXT + detector + SCENARIOS)
        cls.executable = cpp.with_suffix('')
        subprocess.run([shutil.which('c++'), '-std=c++11', '-O2', '-Wall', '-Wextra',
                        str(cpp), '-o', str(cls.executable)], check=True, capture_output=True, timeout=15)

    def scenario(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_boundary_jitter(self): self.scenario('boundary-jitter')
    def test_closed_loop_and_repeated_recovery(self): self.scenario('closed-loop')
    def test_exploration_and_objectives_reset_deadline(self): self.scenario('meaningful-progress')
    def test_humans_agents_and_pauses_do_not_consume_deadline(self): self.scenario('ownership-pauses')
    def test_world_epochs(self): self.scenario('world-epochs')
    def test_signed_coordinate_bounds(self): self.scenario('coordinate-bounds')
    def test_rendering_does_not_consume_world_deadline(self): self.scenario('unchanged-world-tic')


if __name__ == '__main__':
    unittest.main()
