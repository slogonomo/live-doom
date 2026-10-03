// SPDX-License-Identifier: 0BSD
#ifndef DOOM_DESKTOP_BRIDGE_H
#define DOOM_DESKTOP_BRIDGE_H
#include <stdint.h>
#define DD_MAGIC 0x44444f4dU
#define DD_VERSION 3
#define DD_HEADER_SIZE 4096
#define DD_MAX_WIDTH 3840
#define DD_MAX_HEIGHT 2160
#define DD_FRAME_BYTES (DD_MAX_WIDTH * DD_MAX_HEIGHT * 4)
#define DD_FILE_SIZE (DD_HEADER_SIZE + DD_FRAME_BYTES)
/* Engine writes with an odd/even sequence counter. Pixels are XRGB8888.
 * Consumers copy only when seq is even and unchanged after the copy.
 * frame is an image revision, not a render/tic count. An unchanged image
 * retains its frame ID while seq and telemetry (including tic) advance. */
typedef struct {
    uint32_t magic, version, seq, width, height, stride;
    uint32_t bot, paused, audible, gamestate;
    uint64_t frame, tic;
    int32_t leveltime, health, x, y, kills, items, secrets;
    char map[16];
    uint32_t pid, campaign, audio_peak;
    int32_t angle;
    uint32_t pixel_aspect_num, pixel_aspect_den;
    int32_t fov, view_width, view_height, hud_layout;
    uint32_t bot_replans;
    uint32_t render_fps, render_lerp;
    int32_t render_angle;
    uint32_t human_quits;
    uint32_t owner, agent_gen;
    int32_t armor, ammo[4], ready_weapon;
    uint32_t weapons_owned, keys;
    int32_t z, momx, momy, damage_count;
    uint32_t deaths, levels_completed;
    uint32_t bot_stuck;
} DDHeader;
#endif
