/* SPDX-License-Identifier: 0BSD */
#define _GNU_SOURCE
#include "bridge.h"
#include <cairo/cairo.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wayland-cursor.h>
#include <xkbcommon/xkbcommon.h>
#include "xdg-shell-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "wlr-layer-shell-unstable-v1-client-protocol.h"
#include "relative-pointer-unstable-v1-client-protocol.h"
#include "pointer-constraints-unstable-v1-client-protocol.h"

typedef struct App App;
typedef struct Screen Screen;
typedef struct Buffer Buffer;
enum ViewMode { VIEW_HIDDEN, VIEW_PAUSED, VIEW_BOT, VIEW_HUMAN };
struct Buffer {
    struct wl_buffer *wl;
    void *pixels;
    size_t bytes;
    int width, height, stride;
    bool busy, retired;
    Buffer *next;
};
struct Screen {
    App *app;
    uint32_t global;
    struct wl_output *output;
    struct zxdg_output_v1 *xdg_output;
    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer;
    struct xdg_surface *xdg;
    struct xdg_toplevel *toplevel;
    struct wl_callback *callback;
    Buffer *buffers[2];
    char name[128];
    int width, height, scale;
    int output_width, output_height, logical_width, logical_height, output_transform;
    int reserved[4]; /* left, top, right, bottom in surface logical units */
    bool configured, redraw, ready, snapshot_valid, audible, removed;
    bool mapped, unmapped;
    bool output_changed;
    bool query_failed;
    enum ViewMode mode;
    uint32_t *snapshot;
    DDHeader header;
    DDHeader observed_header; /* Latest telemetry, independent of held pixels. */
    uint64_t last_frame;
    uint64_t last_present_tic, last_present_frame;
    uint32_t last_present_pid;
    unsigned present_stride;
    bool image_presented, present_urgent;
    uint64_t last_view_success;
    uint64_t mode_changed_at;
    bool caption_help_visible;
    int blur_strength, blur_cached_strength, blur_width, blur_height;
    uint32_t *blurred;
    bool blur_valid;
    double blur_mix, blur_from, blur_target;
    uint64_t blur_changed_at;
    Screen *next;
};
struct App {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_pointer *pointer;
    struct wl_keyboard *keyboard;
    struct wl_cursor_theme *cursor_theme;
    struct wl_cursor *cursor;
    struct wl_surface *cursor_surface;
    struct zwlr_layer_shell_v1 *layer_shell;
    struct xdg_wm_base *wm_base;
    struct zxdg_output_manager_v1 *output_manager;
    struct zwp_relative_pointer_manager_v1 *relative_manager;
    struct zwp_pointer_constraints_v1 *constraints;
    struct zwp_relative_pointer_v1 *relative;
    struct zwp_locked_pointer_v1 *locked;
    struct xkb_context *xkb_context;
    struct xkb_keymap *keymap;
    struct xkb_state *xkb_state;
    Screen *screens, *pointer_focus, *keyboard_focus, *human;
    Buffer *buffers;
    const char *socket_path, *frame_path;
    int frame_fd;
    const DDHeader *shared;
    uint32_t keys[768], refs[256];
    uint32_t *scratch, cursor_serial;
    DDHeader scratch_header;
    bool scratch_valid;
    bool mouse_fire, saver, saver_layer, preview, quit, pointer_seen;
    bool lock_active;
    bool input_active, input_connecting, input_failed, returning_bot;
    int input_fd;
    char input_queue[65536], input_reply[512];
    size_t input_used, input_sent, input_reply_used;
    int64_t input_mouse_x, input_mouse_y;
    int repeat_rate, repeat_delay;
    uint32_t repeat_key;
    uint64_t repeat_at;
    double pointer_x, pointer_y, mouse_remainder_x, mouse_remainder_y;
    uint64_t started, next_query;
    uint64_t saver_presented_at;
};
static volatile sig_atomic_t interrupted;
static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static void on_signal(int sig) { (void)sig; interrupted = 1; }
static void fail(const char *message) { fprintf(stderr, "doom-viewer: %s\n", message); exit(1); }
static void diagnostic(App *app, const char *event) {
    fprintf(stderr, "doom-viewer [%llu ms]: %s\n", (unsigned long long)(now_ms() - app->started), event);
}
static void set_cursor(App *app);
static void return_bot(App *app, const char *reason);

/* One ordered stream per human lease. Event callbacks only enqueue; neither
 * a stalled controller nor its 35 Hz engine may block Wayland dispatch. EOF
 * revokes the lease in the controller, so an ambiguous send is never replayed. */
static void input_close(App *app) {
    if (app->input_active) close(app->input_fd);
    app->input_active = app->input_connecting = false;
    app->input_used = app->input_sent = app->input_reply_used = 0;
    app->input_mouse_x = app->input_mouse_y = 0;
}
static void input_failed(App *app, const char *reason) {
    input_close(app); app->input_failed = true;
    return_bot(app, reason);
}
static bool input_append(App *app, const char *line) {
    if (app->saver || !app->human || app->input_failed) return false;
    size_t length = strlen(line);
    if (app->input_sent) {
        memmove(app->input_queue, app->input_queue + app->input_sent, app->input_used - app->input_sent);
        app->input_used -= app->input_sent; app->input_sent = 0;
    }
    if (length > sizeof(app->input_queue) - app->input_used) {
        input_failed(app, "input queue exhausted; releasing takeover"); return false;
    }
    memcpy(app->input_queue + app->input_used, line, length); app->input_used += length;
    return true;
}
static void input_mouse_flush(App *app) {
    while (app->input_mouse_x || app->input_mouse_y) {
        int x = (int)fmax(-16384, fmin(16384, app->input_mouse_x));
        int y = (int)fmax(-16384, fmin(16384, app->input_mouse_y));
        char line[80]; snprintf(line, sizeof(line), "mouse %d %d\n", x, y);
        app->input_mouse_x -= x; app->input_mouse_y -= y;
        if (!input_append(app, line)) break;
    }
}
static void input_start(App *app) {
    if (app->saver || !app->human || app->input_active || app->input_failed) return;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (fd < 0 || !app->socket_path || strlen(app->socket_path) >= sizeof(address.sun_path)) {
        if (fd >= 0) close(fd);
        input_failed(app, "cannot open input lease"); return;
    }
    strcpy(address.sun_path, app->socket_path);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) {
        close(fd); input_failed(app, "input controller unavailable"); return;
    }
    app->input_fd = fd; app->input_active = true; app->input_connecting = result < 0;
    char hello[256]; snprintf(hello, sizeof(hello), "input %s\n", app->human->name);
    size_t length = strlen(hello);
    if (length + app->input_used > sizeof(app->input_queue)) {
        input_failed(app, "input queue exhausted"); return;
    }
    memmove(app->input_queue + length, app->input_queue, app->input_used);
    memcpy(app->input_queue, hello, length); app->input_used += length;
}
static void input_service(App *app) {
    input_mouse_flush(app); input_start(app);
    if (!app->input_active) return;
    if (app->input_connecting) {
        struct pollfd pfd = {.fd = app->input_fd, .events = POLLOUT};
        if (poll(&pfd, 1, 0) <= 0) return;
        int error = 0; socklen_t size = sizeof(error);
        if (getsockopt(app->input_fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) {
            input_failed(app, "input connection failed"); return;
        }
        app->input_connecting = false;
    }
    while (app->input_sent < app->input_used) {
        ssize_t sent = send(app->input_fd, app->input_queue + app->input_sent,
            app->input_used - app->input_sent, MSG_NOSIGNAL);
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) { input_failed(app, "input stream disconnected"); return; }
        app->input_sent += (size_t)sent;
    }
    if (app->input_sent == app->input_used) app->input_sent = app->input_used = 0;
    for (;;) {
        char bytes[1024]; ssize_t count = recv(app->input_fd, bytes, sizeof(bytes), 0);
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { input_failed(app, "input lease ended"); return; }
        for (ssize_t i = 0; i < count; ++i) {
            if (bytes[i] == '\n') {
                app->input_reply[app->input_reply_used] = 0;
                if (strncmp(app->input_reply, "OK", 2)) {
                    input_failed(app, "controller rejected input lease"); return;
                }
                app->input_reply_used = 0;
            } else if (app->input_reply_used + 1 < sizeof(app->input_reply)) {
                app->input_reply[app->input_reply_used++] = bytes[i];
            } else { input_failed(app, "invalid input response"); return; }
        }
    }
}

/* Each request has its own short-lived connection: a stalled controller must
 * never prevent the Wayland client from servicing compositor events. */
static bool command(App *app, const char *request, char *reply, size_t size) {
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return false;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(app->socket_path) >= sizeof(address.sun_path)) { close(fd); return false; }
    strcpy(address.sun_path, app->socket_path);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) { close(fd); return false; }
    struct pollfd pfd = {.fd = fd, .events = POLLOUT};
    if (result < 0 && poll(&pfd, 1, 35) <= 0) { close(fd); return false; }
    int error = 0; socklen_t error_size = sizeof(error);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &error_size) || error) { close(fd); return false; }
    size_t length = strlen(request), sent = 0;
    while (sent < length) {
        ssize_t count = send(fd, request + sent, length - sent, MSG_NOSIGNAL);
        if (count <= 0) { close(fd); return false; }
        sent += (size_t)count;
    }
    if (send(fd, "\n", 1, MSG_NOSIGNAL) != 1) { close(fd); return false; }
    shutdown(fd, SHUT_WR);
    if (!reply || !size) { close(fd); return true; }
    size_t used = 0;
    uint64_t deadline = now_ms() + (!strncmp(request, "takeover ", 9) ? 250 : 35);
    pfd.events = POLLIN;
    while (used + 1 < size) {
        uint64_t current = now_ms();
        if (current >= deadline || poll(&pfd, 1, (int)(deadline - current)) <= 0) break;
        ssize_t count = recv(fd, reply + used, size - used - 1, 0);
        if (count <= 0) break;
        used += (size_t)count;
        if (memchr(reply, '\n', used)) break;
    }
    reply[used] = 0;
    close(fd);
    return used > 0;
}
static void key_send_text(App *app, int key, bool down, uint32_t character, bool repeat) {
    char request[64];
    input_mouse_flush(app);
    snprintf(request, sizeof(request), "key %d %d %u %d\n", key, down, character, repeat);
    input_append(app, request);
}
static void key_send(App *app, int key, bool down) { key_send_text(app, key, down, 0, false); }
static void key_ref(App *app, unsigned key, bool down) {
    if (!key || key >= 256) return;
    if (down) { if (app->refs[key]++ == 0) key_send(app, (int)key, true); }
    else if (app->refs[key] && --app->refs[key] == 0) key_send(app, (int)key, false);
}
static void release_keys(App *app) {
    for (int key = 1; key < 256; ++key) if (app->refs[key]) key_send(app, key, false);
    memset(app->keys, 0, sizeof(app->keys));
    memset(app->refs, 0, sizeof(app->refs));
    app->mouse_fire = false;
    app->mouse_remainder_x = app->mouse_remainder_y = 0;
    app->repeat_at = 0;
}
static void release_pointer_input(App *app) {
    if (app->mouse_fire) key_ref(app, 0x9d, false);
    app->mouse_fire = false;
    app->mouse_remainder_x = app->mouse_remainder_y = 0;
}
static void release_lock(App *app) {
    /* Invalidate the proxy first, so queued stale callbacks cannot act on the
     * next ownership generation. Persistent constraints may also deactivate
     * temporarily without the controller releasing human ownership. */
    struct zwp_locked_pointer_v1 *locked = app->locked;
    struct zwp_relative_pointer_v1 *relative = app->relative;
    app->locked = NULL; app->relative = NULL; app->lock_active = false;
    if (locked) zwp_locked_pointer_v1_destroy(locked);
    if (relative) zwp_relative_pointer_v1_destroy(relative);
}
static void return_bot(App *app, const char *reason) {
    if (app->saver || !app->human || app->returning_bot) return;
    app->returning_bot = true;
    char event[256]; snprintf(event, sizeof(event), "returning %s to bot: %s", app->human->name, reason);
    diagnostic(app, event);
    release_keys(app);
    input_service(app); input_close(app);
    release_lock(app);
    Screen *screen = app->human;
    app->human = NULL;
    screen->mode = VIEW_BOT; screen->redraw = true;
    screen->mode_changed_at = now_ms();
    if (screen->layer) {
        if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2) zwlr_layer_surface_v1_set_layer(screen->layer, ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
        zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
        wl_surface_commit(screen->surface);
    }
    set_cursor(app);
    command(app, "bot", NULL, 0);
    app->next_query = 0;
    app->returning_bot = false;
}

static void buffer_release(void *data, struct wl_buffer *wl) {
    (void)wl; ((Buffer *)data)->busy = false;
}
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};
static void buffer_destroy(Buffer *buffer) {
    wl_buffer_destroy(buffer->wl);
    munmap(buffer->pixels, buffer->bytes);
    free(buffer);
}
static void collect_buffers(App *app) {
    Buffer **link = &app->buffers;
    while (*link) {
        Buffer *buffer = *link;
        if (buffer->retired && !buffer->busy) { *link = buffer->next; buffer_destroy(buffer); }
        else link = &buffer->next;
    }
}
static Buffer *make_buffer(App *app, int width, int height) {
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384) return NULL;
    size_t bytes = (size_t)width * height * 4;
    if (bytes > INT32_MAX) return NULL;
    int fd = memfd_create("doom-desktop-pixels", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, (off_t)bytes)) { if (fd >= 0) close(fd); return NULL; }
    void *pixels = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { close(fd); return NULL; }
    struct wl_shm_pool *pool = wl_shm_create_pool(app->shm, fd, (int)bytes);
    Buffer *buffer = calloc(1, sizeof(*buffer));
    if (!buffer) fail("out of memory");
    buffer->wl = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4, WL_SHM_FORMAT_XRGB8888);
    buffer->pixels = pixels; buffer->bytes = bytes;
    buffer->width = width; buffer->height = height; buffer->stride = width * 4;
    wl_buffer_add_listener(buffer->wl, &buffer_listener, buffer);
    wl_shm_pool_destroy(pool); close(fd);
    buffer->next = app->buffers; app->buffers = buffer;
    return buffer;
}
static void retire_screen_buffers(Screen *screen) {
    for (int i = 0; i < 2; ++i) {
        if (screen->buffers[i]) screen->buffers[i]->retired = true;
        screen->buffers[i] = NULL;
    }
}
static bool presentation_due(const Screen *screen, const DDHeader *header) {
    if (screen->present_stride != 4 || screen->mode != VIEW_BOT || screen->app->saver
        || !screen->snapshot_valid || !screen->image_presented || screen->present_urgent) return true;
    bool same_geometry = screen->header.width == header->width && screen->header.height == header->height
        && screen->header.stride == header->stride && screen->header.pixel_aspect_num == header->pixel_aspect_num
        && screen->header.pixel_aspect_den == header->pixel_aspect_den;
    bool same_epoch = screen->last_present_pid == header->pid && header->tic >= screen->last_present_tic
        && header->frame >= screen->last_present_frame && !strcmp(screen->header.map, header->map);
    return !same_geometry || !same_epoch || header->tic - screen->last_present_tic >= 4;
}
static bool snapshot(Screen *screen) {
    const DDHeader *shared = screen->app->shared;
    if (!shared) return false;
    for (int attempt = 0; attempt < 4; ++attempt) {
        uint32_t seq = __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE);
        if (seq & 1) continue;
        DDHeader header;
        memcpy(&header, shared, sizeof(header));
        if (header.magic != DD_MAGIC || (header.version != 2 && header.version != 3) || !header.width || !header.height
            || header.width > DD_MAX_WIDTH || header.height > DD_MAX_HEIGHT
            || header.stride < header.width * 4 || (size_t)header.stride * header.height > DD_FRAME_BYTES) return false;
        header.map[sizeof(header.map) - 1] = 0;
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (seq != __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE)) continue;
        screen->observed_header = header;
        if (screen->snapshot_valid && screen->last_frame == header.frame && screen->header.pid == header.pid
            && screen->header.width == header.width && screen->header.height == header.height
            && screen->header.stride == header.stride && screen->header.pixel_aspect_num == header.pixel_aspect_num
            && screen->header.pixel_aspect_den == header.pixel_aspect_den) {
            /* The image ID is independent of observation updates. Keep tic,
             * owner and render-rate metadata current without copying or
             * presenting unchanged pixels. Only the map enters our caption. */
            __atomic_thread_fence(__ATOMIC_ACQUIRE);
            if (seq != __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE)) continue;
            bool caption_changed = strcmp(screen->header.map, header.map) != 0;
            screen->header = header;
            if (caption_changed) screen->present_urgent = true;
            return caption_changed;
        }
        /* Decide before either full-frame memcpy. Presentation is per output,
         * keyed to its last real image commit rather than a modulo tic that a
         * busy callback/poll loop might repeatedly miss. State/geometry redraws
         * and new engine/image epochs still publish immediately. */
        if (!presentation_due(screen, &header)) return false;
        if (screen->app->scratch_valid && screen->app->scratch_header.seq == seq
            && screen->app->scratch_header.frame == header.frame && screen->app->scratch_header.pid == header.pid) {
            memcpy(screen->snapshot, screen->app->scratch, (size_t)header.width * header.height * 4);
            screen->header = screen->app->scratch_header;
            screen->last_frame = header.frame; screen->snapshot_valid = true;
            screen->blur_valid = false;
            return true;
        }
        const uint8_t *source = (const uint8_t *)shared + DD_HEADER_SIZE;
        screen->app->scratch_valid = false;
        for (uint32_t y = 0; y < header.height; ++y)
            memcpy(screen->app->scratch + (size_t)y * header.width, source + (size_t)y * header.stride, header.width * 4);
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (seq != __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE)) continue;
        memcpy(screen->snapshot, screen->app->scratch, (size_t)header.width * header.height * 4);
        screen->app->scratch_header = header; screen->app->scratch_valid = true;
        screen->header = header; screen->last_frame = header.frame; screen->snapshot_valid = true;
        screen->blur_valid = false;
        return true;
    }
    return false;
}
static void text_label(cairo_t *cr, const char *text, double x, double y, double size, double alpha) {
    cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
    cairo_set_font_size(cr, size);
    cairo_set_source_rgba(cr, 0, 0, 0, alpha * .75);
    cairo_move_to(cr, x + 1, y + 1); cairo_show_text(cr, text);
    cairo_set_source_rgba(cr, .87, .88, .86, alpha);
    cairo_move_to(cr, x, y); cairo_show_text(cr, text);
}
typedef struct { double x, y, width, height, aspect; bool top_cropped; } GameBox;
typedef struct { double x, y, width, height; } SafeBox;
static SafeBox overlay_area(const Screen *screen) {
    /* The game remains full-output. Only our text avoids reserved panels;
     * saver overlays cover those panels and therefore use the whole output. */
    if (screen->app->saver) return (SafeBox){0, 0, screen->width, screen->height};
    double left = fmin(screen->width, fmax(0, screen->reserved[0]));
    double top = fmin(screen->height, fmax(0, screen->reserved[1]));
    double right = fmin(screen->width - left, fmax(0, screen->reserved[2]));
    double bottom = fmin(screen->height - top, fmax(0, screen->reserved[3]));
    return (SafeBox){left, top, screen->width - left - right, screen->height - top - bottom};
}
static GameBox game_box(const Screen *screen) {
    double aspect = 4.0 / 3.0;
    if (screen->snapshot_valid && screen->header.width && screen->header.height) {
        uint32_t num = screen->header.pixel_aspect_num, den = screen->header.pixel_aspect_den;
        if (!num || !den || num > 16 || den > 16) num = den = 1;
        aspect = (double)screen->header.width * num / ((double)screen->header.height * den);
    }
    double width = fmin(screen->width, screen->height * aspect), height = width / aspect;
    double output_aspect = (double)screen->width / screen->height;
    if (output_aspect >= aspect && output_aspect / aspect <= 1.03) {
        /* The desktop loses only the bar's few rows relative to the native
         * monitor frame. Fill at unchanged geometry and crop from the top;
         * every bottom HUD corner remains visible. Engine messages have a
         * matching 3%-height safe inset. Unlike output aspects stay contained. */
        width = screen->width; height = width / aspect;
        return (GameBox){0, screen->height - height, width, height, aspect, height > screen->height + .001};
    }
    return (GameBox){(screen->width - width) / 2, (screen->height - height) / 2, width, height, aspect, false};
}
static void rounded_rectangle(cairo_t *cr, double x, double y, double width, double height, double radius) {
    cairo_new_sub_path(cr);
    cairo_arc(cr, x + width - radius, y + radius, radius, -M_PI / 2, 0);
    cairo_arc(cr, x + width - radius, y + height - radius, radius, 0, M_PI / 2);
    cairo_arc(cr, x + radius, y + height - radius, radius, M_PI / 2, M_PI);
    cairo_arc(cr, x + radius, y + radius, radius, M_PI, M_PI * 3 / 2);
    cairo_close_path(cr);
}
static void paint_caption(Screen *screen, cairo_t *cr, GameBox box) {
    SafeBox safe = overlay_area(screen);
    char label[256]; const char *help = NULL;
    const char *map = screen->header.map[0] ? screen->header.map : "loading";
    bool recent = !screen->mode_changed_at || now_ms() - screen->mode_changed_at < 8000;
    bool paused = screen->mode == VIEW_PAUSED || screen->mode == VIEW_HIDDEN;
    if (screen->app->saver) {
        snprintf(label, sizeof(label), "Live Doom · %s · bot playing · %s", map, screen->audible ? "sound on" : "muted");
        if (recent) help = "Move mouse or press a key to dismiss";
    } else if (screen->mode == VIEW_HUMAN) {
        snprintf(label, sizeof(label), "Live Doom · %s · you’re playing · F12 returns to bot", map);
        if (recent) help = "WASD + mouse · click / Ctrl fire · E use · Esc menu";
    } else if (paused) {
        snprintf(label, sizeof(label), "Live Doom · paused while you work");
    } else {
        snprintf(label, sizeof(label), "Live Doom · %s · bot playing · %s", map, screen->audible ? "sound on" : "muted");
        if (recent) help = "Click to play · right-click settings";
    }
    screen->caption_help_visible = help != NULL;
    if (safe.width < 64 || safe.height < 56) return;
    double size = screen->width < 900 ? 11 : 13;
    cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
    cairo_text_extents_t label_extents, help_extents = {0};
    cairo_set_font_size(cr, size); cairo_text_extents(cr, label, &label_extents);
    if (help) { cairo_set_font_size(cr, size - 1); cairo_text_extents(cr, help, &help_extents); }
    double width = fmin(safe.width - 24, fmax(label_extents.x_advance, help_extents.x_advance) + 28);
    double height = help ? 52 : 32;
    double right = fmin(box.x + box.width - 14, safe.x + safe.width - 14);
    double x = fmax(safe.x + 12, right - width);
    /* Use top letterboxing on unlike outputs; otherwise the upper-right
     * overlay leaves Doom's pickup messages and all bottom HUD corners clear. */
    double y = box.y >= safe.y + height + 20 ? safe.y + 12 : fmax(box.y, safe.y) + 14;
    y = fmin(y, safe.y + safe.height - height - 2);
    cairo_save(cr);
    cairo_rectangle(cr, safe.x, safe.y, safe.width, safe.height); cairo_clip(cr);
    rounded_rectangle(cr, x, y, width, height, 7);
    cairo_set_source_rgba(cr, .018, .021, .024, paused ? .86 : .67); cairo_fill(cr);
    cairo_rectangle(cr, x + 8, y + 10, 2, height - 20);
    if (paused) cairo_set_source_rgba(cr, .82, .64, .37, .88);
    else if (screen->mode == VIEW_HUMAN) cairo_set_source_rgba(cr, .79, .31, .26, .94);
    else cairo_set_source_rgba(cr, .62, .65, .62, .62);
    cairo_fill(cr);
    cairo_rectangle(cr, x + 14, y + 4, width - 21, height - 8); cairo_clip(cr);
    text_label(cr, label, x + 17, y + 21, size, .87);
    if (help) text_label(cr, help, x + 17, y + 41, size - 1, .65);
    cairo_restore(cr);
}
static void caption_animate(Screen *screen, uint64_t current) {
    bool recent = !screen->mode_changed_at || current - screen->mode_changed_at < 8000;
    bool help = recent && (screen->app->saver || screen->mode == VIEW_BOT || screen->mode == VIEW_HUMAN);
    /* Static game images no longer supply repeated repaints. Expiring the
     * local help text still needs one presentation, then it stays idle. */
    if (help != screen->caption_help_visible) screen->redraw = true;
}
static bool blur_animate(Screen *screen, uint64_t current) {
    double target = !screen->app->saver && screen->mode == VIEW_PAUSED && screen->blur_strength > 0 ? 1 : 0;
    if (screen->blur_target != target) {
        screen->blur_from = screen->blur_mix; screen->blur_target = target;
        screen->blur_changed_at = current;
    }
    double progress = fmin(1, (double)(current - screen->blur_changed_at) / 120);
    double eased = progress * progress * (3 - 2 * progress);
    double mix = screen->blur_from + (screen->blur_target - screen->blur_from) * eased;
    if (fabs(mix - screen->blur_mix) > .000001) { screen->blur_mix = mix; screen->redraw = true; }
    return progress < 1 && screen->blur_from != screen->blur_target;
}
static void blur_pass(const uint32_t *source, uint32_t *target, int width, int height, double extent, bool vertical) {
    int lines = vertical ? width : height, length = vertical ? height : width;
    int radius = (int)extent; double fraction = extent - radius;
    for (int line = 0; line < lines; ++line) {
        int sum[3] = {0};
        for (int i = -radius; i <= radius; ++i) {
            int at = i < 0 ? 0 : i;
            uint32_t pixel = source[vertical ? (size_t)at * width + line : (size_t)line * width + at];
            sum[0] += pixel & 255; sum[1] += (pixel >> 8) & 255; sum[2] += (pixel >> 16) & 255;
        }
        double count = radius * 2 + 1 + fraction * 2;
        for (int at = 0; at < length; ++at) {
            int edge_left = at - radius - 1, edge_right = at + radius + 1;
            if (edge_left < 0) edge_left = 0;
            if (edge_right >= length) edge_right = length - 1;
            uint32_t left = source[vertical ? (size_t)edge_left * width + line : (size_t)line * width + edge_left];
            uint32_t right = source[vertical ? (size_t)edge_right * width + line : (size_t)line * width + edge_right];
            uint32_t result = 0;
            for (int color = 0; color < 3; ++color) result |= (uint32_t)((sum[color] + fraction *
                (((left >> (color * 8)) & 255) + ((right >> (color * 8)) & 255))) / count) << (color * 8);
            target[vertical ? (size_t)at * width + line : (size_t)line * width + at] = result;
            int leave = at - radius, enter = at + radius + 1;
            if (leave < 0) leave = 0;
            if (enter >= length) enter = length - 1;
            uint32_t old = source[vertical ? (size_t)leave * width + line : (size_t)line * width + leave];
            uint32_t next = source[vertical ? (size_t)enter * width + line : (size_t)line * width + enter];
            for (int color = 0; color < 3; ++color) sum[color] += (int)((next >> (color * 8)) & 255) - (int)((old >> (color * 8)) & 255);
        }
    }
}
static bool prepare_blur(Screen *screen) {
    if (!screen->snapshot_valid || screen->blur_strength <= 0) return false;
    if (screen->blur_valid && screen->blur_cached_strength == screen->blur_strength) return true;
    int width = (int)screen->header.width, height = (int)screen->header.height;
    double factor = fmin(1, 640.0 / fmax(width, height));
    int bw = fmax(1, ceil(width * factor)), bh = fmax(1, ceil(height * factor));
    size_t bytes = (size_t)bw * bh * 4;
    uint32_t *pixels = malloc(bytes), *scratch = malloc(bytes);
    if (!pixels || !scratch) { free(pixels); free(scratch); return false; }
    for (int y = 0; y < bh; ++y) for (int x = 0; x < bw; ++x)
        pixels[(size_t)y * bw + x] = screen->snapshot[(size_t)(y * height / bh) * width + x * width / bw];
    double radius = screen->blur_strength / 20.0;
    if (radius >= bw) radius = bw - 1;
    if (radius >= bh) radius = bh - 1;
    for (int pass = 0; pass < 3; ++pass) {
        blur_pass(pixels, scratch, bw, bh, radius, false);
        blur_pass(scratch, pixels, bw, bh, radius, true);
    }
    free(scratch); free(screen->blurred); screen->blurred = pixels;
    screen->blur_width = bw; screen->blur_height = bh;
    screen->blur_cached_strength = screen->blur_strength; screen->blur_valid = true;
    return true;
}
static void paint(Screen *screen, Buffer *buffer) {
    cairo_surface_t *target = cairo_image_surface_create_for_data(buffer->pixels, CAIRO_FORMAT_RGB24,
        buffer->width, buffer->height, buffer->stride);
    cairo_t *cr = cairo_create(target);
    cairo_scale(cr, screen->scale, screen->scale);
    cairo_set_source_rgb(cr, .018, .021, .024); cairo_paint(cr);
    GameBox box = game_box(screen);
    if (screen->snapshot_valid) {
        cairo_surface_t *source = cairo_image_surface_create_for_data((unsigned char *)screen->snapshot,
            CAIRO_FORMAT_RGB24, (int)screen->header.width, (int)screen->header.height, (int)screen->header.width * 4);
        cairo_save(cr); cairo_translate(cr, box.x, box.y);
        cairo_scale(cr, box.width / screen->header.width, box.height / screen->header.height);
        cairo_set_source_surface(cr, source, 0, 0);
        cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_NEAREST);
        cairo_paint(cr); cairo_restore(cr); cairo_surface_destroy(source);
        if (screen->blur_mix > 0 && prepare_blur(screen)) {
            cairo_surface_t *blur = cairo_image_surface_create_for_data((unsigned char *)screen->blurred,
                CAIRO_FORMAT_RGB24, screen->blur_width, screen->blur_height, screen->blur_width * 4);
            cairo_save(cr); cairo_translate(cr, box.x, box.y);
            cairo_scale(cr, box.width / screen->blur_width, box.height / screen->blur_height);
            cairo_set_source_surface(cr, blur, 0, 0);
            cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_BILINEAR);
            cairo_paint_with_alpha(cr, screen->blur_mix);
            cairo_restore(cr); cairo_surface_destroy(blur);
        }
    } else {
        SafeBox safe = overlay_area(screen);
        cairo_save(cr); cairo_rectangle(cr, safe.x, safe.y, safe.width, safe.height); cairo_clip(cr);
        cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
        cairo_set_font_size(cr, 24);
        cairo_text_extents_t title;
        cairo_text_extents(cr, "Live Doom", &title);
        text_label(cr, "Live Doom", safe.x + (safe.width - title.width) / 2 - title.x_bearing,
            safe.y + safe.height / 2, 24, .7);
        text_label(cr, "Waiting for the game", safe.x + safe.width / 2 - 76,
            safe.y + safe.height / 2 + 28, 14, .55);
        cairo_restore(cr);
    }
    paint_caption(screen, cr, box);
    cairo_destroy(cr); cairo_surface_flush(target); cairo_surface_destroy(target);
}
static void frame_done(void *data, struct wl_callback *callback, uint32_t time) {
    (void)time;
    Screen *screen = data;
    wl_callback_destroy(callback); screen->callback = NULL; screen->ready = true;
}
static const struct wl_callback_listener frame_listener = {.done = frame_done};
static void render(Screen *screen) {
    /* Hidden policy removes the wallpaper rather than painting a frozen game
     * over the newly selected static background. Saver windows are separate. */
    if (!screen->app->saver && screen->mode == VIEW_HIDDEN) return;
    if (!screen->configured || screen->removed || !screen->ready || !screen->redraw) return;
    if (!presentation_due(screen, &screen->observed_header)) return;
    int width = screen->width * screen->scale, height = screen->height * screen->scale;
    if (screen->buffers[0] && (screen->buffers[0]->width != width || screen->buffers[0]->height != height)) retire_screen_buffers(screen);
    Buffer *buffer = NULL;
    for (int i = 0; i < 2; ++i) {
        if (!screen->buffers[i]) screen->buffers[i] = make_buffer(screen->app, width, height);
        if (screen->buffers[i] && !screen->buffers[i]->busy) { buffer = screen->buffers[i]; break; }
    }
    if (!buffer) return;
    paint(screen, buffer); buffer->busy = true;
    wl_surface_set_buffer_scale(screen->surface, screen->scale);
    wl_surface_attach(screen->surface, buffer->wl, 0, 0);
    wl_surface_damage_buffer(screen->surface, 0, 0, width, height);
    screen->callback = wl_surface_frame(screen->surface);
    wl_callback_add_listener(screen->callback, &frame_listener, screen);
    screen->ready = false; screen->redraw = false;
    screen->mapped = true;
    wl_surface_commit(screen->surface);
    screen->present_urgent = false;
    if (screen->snapshot_valid) {
        screen->last_present_tic = screen->header.tic; screen->last_present_frame = screen->last_frame;
        screen->last_present_pid = screen->header.pid; screen->image_presented = true;
    }
    if (screen->app->saver && screen->snapshot_valid && !screen->app->saver_presented_at)
        screen->app->saver_presented_at = now_ms();
}

static bool saver_input_ready(const App *app) {
    /* Starting the grace at the first actual game-image commit prevents a
     * cold engine load from consuming it while only a placeholder is shown. */
    return !app->preview || (app->saver_presented_at && now_ms() - app->saver_presented_at >= 1500);
}

static void relative_motion(void *data, struct zwp_relative_pointer_v1 *relative, uint32_t hi, uint32_t lo,
        wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t unaccel_x, wl_fixed_t unaccel_y) {
    (void)hi; (void)lo; (void)dx; (void)dy;
    App *app = data;
    if (relative != app->relative || !app->human || app->pointer_focus != app->human || app->saver) return;
    app->mouse_remainder_x += wl_fixed_to_double(unaccel_x);
    app->mouse_remainder_y += wl_fixed_to_double(unaccel_y);
    int x = (int)app->mouse_remainder_x, y = (int)app->mouse_remainder_y;
    if (x || y) {
        app->input_mouse_x += x; app->input_mouse_y += y;
        app->mouse_remainder_x -= x; app->mouse_remainder_y -= y;
    }
}
static const struct zwp_relative_pointer_v1_listener relative_listener = {.relative_motion = relative_motion};
static void pointer_locked(void *data, struct zwp_locked_pointer_v1 *pointer) {
    App *app = data;
    if (pointer != app->locked) return;
    app->lock_active = true;
    diagnostic(app, "relative mouse constraint activated");
}
static void pointer_unlocked(void *data, struct zwp_locked_pointer_v1 *pointer) {
    App *app = data;
    if (pointer != app->locked) return;
    app->lock_active = false;
    release_pointer_input(app);
    diagnostic(app, "relative mouse constraint deactivated; controller retains ownership authority");
}
static const struct zwp_locked_pointer_v1_listener locked_listener = {.locked = pointer_locked, .unlocked = pointer_unlocked};
static void ensure_lock(App *app) {
    if (app->saver || !app->human || !app->pointer || app->locked || !app->constraints || !app->relative_manager) return;
    app->relative = zwp_relative_pointer_manager_v1_get_relative_pointer(app->relative_manager, app->pointer);
    zwp_relative_pointer_v1_add_listener(app->relative, &relative_listener, app);
    app->locked = zwp_pointer_constraints_v1_lock_pointer(app->constraints, app->human->surface,
        app->pointer, NULL, ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
    zwp_locked_pointer_v1_add_listener(app->locked, &locked_listener, app);
    wl_surface_commit(app->human->surface);
}
static Screen *screen_for_surface(App *app, struct wl_surface *surface) {
    for (Screen *screen = app->screens; screen; screen = screen->next) if (screen->surface == surface) return screen;
    return NULL;
}
static void set_cursor(App *app) {
    if (!app->pointer || !app->pointer_focus || !app->cursor_serial) return;
    if (app->saver || app->human) {
        wl_pointer_set_cursor(app->pointer, app->cursor_serial, NULL, 0, 0);
    } else if (app->cursor && app->cursor_surface) {
        struct wl_cursor_image *image = app->cursor->images[0];
        wl_pointer_set_cursor(app->pointer, app->cursor_serial, app->cursor_surface, image->hotspot_x, image->hotspot_y);
        wl_surface_attach(app->cursor_surface, wl_cursor_image_get_buffer(image), 0, 0);
        wl_surface_damage(app->cursor_surface, 0, 0, image->width, image->height);
        wl_surface_commit(app->cursor_surface);
    }
}
static void pointer_enter(void *data, struct wl_pointer *pointer, uint32_t serial,
        struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y) {
    App *app = data;
    if (app->saver && saver_input_ready(app) && app->pointer_seen && now_ms() - app->started > 500) app->quit = true;
    app->pointer_focus = screen_for_surface(app, surface);
    char event[256]; snprintf(event, sizeof(event), "pointer entered %s at %.0f,%.0f", app->pointer_focus ? app->pointer_focus->name : "unknown surface", wl_fixed_to_double(x), wl_fixed_to_double(y));
    diagnostic(app, event);
    app->pointer_x = wl_fixed_to_double(x); app->pointer_y = wl_fixed_to_double(y);
    app->pointer_seen = true;
    (void)pointer; app->cursor_serial = serial; set_cursor(app);
    ensure_lock(app);
}
static void pointer_leave(void *data, struct wl_pointer *pointer, uint32_t serial, struct wl_surface *surface) {
    (void)pointer; (void)serial; (void)surface;
    App *app = data;
    release_pointer_input(app);
    app->pointer_focus = NULL; app->cursor_serial = 0;
}
static void pointer_motion(void *data, struct wl_pointer *pointer, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)pointer; (void)time;
    App *app = data;
    double px = wl_fixed_to_double(x), py = wl_fixed_to_double(y);
    if (app->saver && saver_input_ready(app) && app->pointer_seen && now_ms() - app->started > 500
        && (fabs(px - app->pointer_x) > .25 || fabs(py - app->pointer_y) > .25)) app->quit = true;
    app->pointer_x = px; app->pointer_y = py; app->pointer_seen = true;
}
static void pointer_button(void *data, struct wl_pointer *pointer, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
    (void)pointer; (void)time;
    App *app = data;
    if (app->saver) {
        if (state == WL_POINTER_BUTTON_STATE_PRESSED && saver_input_ready(app)) app->quit = true;
        return;
    }
    Screen *screen = app->pointer_focus;
    char event[256]; snprintf(event, sizeof(event), "pointer button %u state=%u on %s mode=%d", button, state, screen ? screen->name : "no surface", screen ? (int)screen->mode : -1);
    diagnostic(app, event);
    if (!screen || screen->mode == VIEW_HIDDEN) return;
    bool down = state == WL_POINTER_BUTTON_STATE_PRESSED;
    if (button == BTN_RIGHT && down) {
        char request[256]; snprintf(request, sizeof(request), "settings %s", screen->name);
        command(app, request, NULL, 0); return;
    }
    if (screen->mode == VIEW_PAUSED) return;
    if (button == BTN_LEFT && app->human == screen) {
        if (down != app->mouse_fire) { app->mouse_fire = down; key_ref(app, 0x9d, down); }
    } else if (button == BTN_LEFT && down && screen->mode == VIEW_BOT) {
        char request[256], reply[128];
        snprintf(request, sizeof(request), "takeover %s", screen->name);
        if (command(app, request, reply, sizeof(reply)) && !strncmp(reply, "OK", 2)) {
            /* Hide immediately using the serial from the initiating click. */
            wl_pointer_set_cursor(app->pointer, serial, NULL, 0, 0);
            app->next_query = 0;
        }
    }
}
static void pointer_axis(void *data, struct wl_pointer *pointer, uint32_t time, uint32_t axis, wl_fixed_t value) {
    (void)pointer; (void)time; (void)axis; (void)value;
    App *app = data;
    if (app->saver && saver_input_ready(app) && now_ms() - app->started > 500) app->quit = true;
}
static void pointer_frame(void *data, struct wl_pointer *pointer) { (void)data; (void)pointer; }
static void pointer_axis_source(void *data, struct wl_pointer *pointer, uint32_t source) { (void)data; (void)pointer; (void)source; }
static void pointer_axis_stop(void *data, struct wl_pointer *pointer, uint32_t time, uint32_t axis) { (void)data; (void)pointer; (void)time; (void)axis; }
static void pointer_axis_discrete(void *data, struct wl_pointer *pointer, uint32_t axis, int32_t discrete) { (void)data; (void)pointer; (void)axis; (void)discrete; }
static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion, .button = pointer_button,
    .axis = pointer_axis, .frame = pointer_frame, .axis_source = pointer_axis_source,
    .axis_stop = pointer_axis_stop, .axis_discrete = pointer_axis_discrete,
};
static void keyboard_keymap(void *data, struct wl_keyboard *keyboard, uint32_t format, int32_t fd, uint32_t size) {
    (void)keyboard;
    App *app = data;
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || !size) { close(fd); return; }
    char *mapping = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (mapping == MAP_FAILED) return;
    struct xkb_keymap *keymap = xkb_keymap_new_from_string(app->xkb_context, mapping,
        XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
    munmap(mapping, size);
    if (!keymap) return;
    struct xkb_state *state = xkb_state_new(keymap);
    if (!state) { xkb_keymap_unref(keymap); return; }
    xkb_state_unref(app->xkb_state); xkb_keymap_unref(app->keymap);
    app->keymap = keymap; app->xkb_state = state;
}
static void keyboard_enter(void *data, struct wl_keyboard *keyboard, uint32_t serial, struct wl_surface *surface, struct wl_array *keys) {
    (void)keyboard; (void)serial; (void)keys;
    App *app = data; app->keyboard_focus = screen_for_surface(app, surface);
    if (app->keyboard_focus) diagnostic(app, "game keyboard focus entered");
}
static void keyboard_leave(void *data, struct wl_keyboard *keyboard, uint32_t serial, struct wl_surface *surface) {
    (void)keyboard; (void)serial; (void)surface;
    App *app = data; app->keyboard_focus = NULL;
    if (app->human) { release_keys(app); diagnostic(app, "human keyboard focus left"); }
}
static unsigned doom_key(xkb_keysym_t symbol) {
    symbol = xkb_keysym_to_lower(symbol);
    switch (symbol) {
    case XKB_KEY_Up: return 0xad; case XKB_KEY_Down: return 0xaf;
    case XKB_KEY_Left: return 0xac; case XKB_KEY_Right: return 0xae;
    case XKB_KEY_Shift_L: case XKB_KEY_Shift_R: return 0xb6;
    case XKB_KEY_Control_L: case XKB_KEY_Control_R: return 0x9d;
    case XKB_KEY_Alt_L: case XKB_KEY_Alt_R: return 0xb8;
    case XKB_KEY_Return: case XKB_KEY_KP_Enter: return 13;
    case XKB_KEY_Tab: return 9; case XKB_KEY_BackSpace: return 127;
    case XKB_KEY_Escape: return 27;
    case XKB_KEY_F1: return 187; case XKB_KEY_F2: return 188; case XKB_KEY_F3: return 189;
    case XKB_KEY_F4: return 190; case XKB_KEY_F5: return 191; case XKB_KEY_F6: return 192;
    case XKB_KEY_F7: return 193; case XKB_KEY_F8: return 194; case XKB_KEY_F9: return 195;
    case XKB_KEY_F10: return 196; case XKB_KEY_F11: return 215;
    case XKB_KEY_Home: return 200; case XKB_KEY_Page_Up: return 201; case XKB_KEY_End: return 207;
    case XKB_KEY_Page_Down: return 209; case XKB_KEY_Insert: return 210;
    case XKB_KEY_Delete: return 254; case XKB_KEY_Pause: return 255;
    default: return symbol >= 32 && symbol < 127 ? symbol : 0;
    }
}
static unsigned physical_key(App *app, uint32_t key) {
    xkb_layout_index_t layout = xkb_state_key_get_layout(app->xkb_state, key + 8);
    const xkb_keysym_t *symbols = NULL;
    int count = xkb_keymap_key_get_syms_by_level(app->keymap, key + 8, layout, 0, &symbols);
    return count > 0 ? doom_key(symbols[0]) : 0;
}
static uint32_t key_character(App *app, uint32_t key, unsigned identity, bool repeat) {
    if ((!repeat && identity == '`') ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0 ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_ALT, XKB_STATE_MODS_EFFECTIVE) > 0 ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_LOGO, XKB_STATE_MODS_EFFECTIVE) > 0) return 0;
    uint32_t character = xkb_state_key_get_utf32(app->xkb_state, key + 8);
    return character >= 32 && character <= 126 ? character : 0;
}
static void repeat_keys(App *app) {
    uint64_t current = now_ms();
    if (!app->repeat_at || current < app->repeat_at || !app->human || app->saver) return;
    unsigned identity = app->keys[app->repeat_key];
    if (!identity || app->keyboard_focus != app->human) { app->repeat_at = 0; return; }
    key_send_text(app, identity, true, key_character(app, app->repeat_key, identity, true), true);
    if (app->human && !app->input_failed) app->repeat_at = current + (uint64_t)(1000 / app->repeat_rate);
}
static void keyboard_key(void *data, struct wl_keyboard *keyboard, uint32_t serial, uint32_t time, uint32_t key, uint32_t state) {
    (void)keyboard; (void)serial; (void)time;
    App *app = data;
    if (app->saver) {
        if (state == WL_KEYBOARD_KEY_STATE_PRESSED && saver_input_ready(app)) app->quit = true;
        return;
    }
    if (!app->human || app->keyboard_focus != app->human || !app->xkb_state || key >= 768) return;
    bool down = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    xkb_keysym_t symbol = xkb_state_key_get_one_sym(app->xkb_state, key + 8);
    if (down && symbol == XKB_KEY_F12) { return_bot(app, "F12"); return; }
    if (down && !app->keys[key]) {
        unsigned identity = physical_key(app, key);
        app->keys[key] = identity;
        if (identity && app->refs[identity]++ == 0)
            key_send_text(app, identity, true, key_character(app, key, identity, false), false);
        if (app->human && !app->input_failed && identity && app->repeat_rate > 0 && xkb_keymap_key_repeats(app->keymap, key + 8)) {
            app->repeat_key = key; app->repeat_at = now_ms() + (uint64_t)app->repeat_delay;
        }
    } else if (!down && app->keys[key]) {
        key_ref(app, app->keys[key], false); app->keys[key] = 0;
        if (app->repeat_key == key) app->repeat_at = 0;
    }
}
static void keyboard_modifiers(void *data, struct wl_keyboard *keyboard, uint32_t serial,
        uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
    (void)keyboard; (void)serial;
    App *app = data;
    if (app->xkb_state) xkb_state_update_mask(app->xkb_state, depressed, latched, locked, 0, 0, group);
}
static void keyboard_repeat(void *data, struct wl_keyboard *keyboard, int32_t rate, int32_t delay) {
    (void)keyboard; App *app = data;
    app->repeat_rate = rate > 0 ? (rate > 100 ? 100 : rate) : 0;
    app->repeat_delay = delay > 0 ? delay : 1;
    if (!app->repeat_rate) app->repeat_at = 0;
}
static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = keyboard_keymap, .enter = keyboard_enter, .leave = keyboard_leave,
    .key = keyboard_key, .modifiers = keyboard_modifiers, .repeat_info = keyboard_repeat,
};
static void seat_capabilities(void *data, struct wl_seat *seat, uint32_t capabilities) {
    App *app = data;
    if ((capabilities & WL_SEAT_CAPABILITY_POINTER) && !app->pointer) {
        app->pointer = wl_seat_get_pointer(seat); wl_pointer_add_listener(app->pointer, &pointer_listener, app); ensure_lock(app);
    } else if (!(capabilities & WL_SEAT_CAPABILITY_POINTER) && app->pointer) {
        release_pointer_input(app); release_lock(app); wl_pointer_release(app->pointer);
        app->pointer = NULL; app->pointer_focus = NULL; app->cursor_serial = 0;
    }
    if ((capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && !app->keyboard) {
        app->keyboard = wl_seat_get_keyboard(seat); wl_keyboard_add_listener(app->keyboard, &keyboard_listener, app);
    } else if (!(capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && app->keyboard) {
        release_keys(app); wl_keyboard_release(app->keyboard); app->keyboard = NULL; app->keyboard_focus = NULL;
    }
}
static void seat_name(void *data, struct wl_seat *seat, const char *name) { (void)data; (void)seat; (void)name; }
static const struct wl_seat_listener seat_listener = {.capabilities = seat_capabilities, .name = seat_name};

static void schedule_geometry_frame(Screen *screen) {
    /* An occluded output may withhold the old frame callback indefinitely.
     * Its callback must not block publishing the newly configured buffer and
     * input bounds. Resume normal frame throttling after that one commit. */
    if (screen->callback) { wl_callback_destroy(screen->callback); screen->callback = NULL; }
    screen->ready = true; screen->redraw = true;
    screen->present_urgent = true;
}
static void configure_geometry(Screen *screen, int width, int height) {
    if (screen->width != width || screen->height != height) {
        screen->width = width; screen->height = height;
        schedule_geometry_frame(screen);
    }
    screen->redraw = true;
}
static void layer_configure(void *data, struct zwlr_layer_surface_v1 *layer, uint32_t serial, uint32_t width, uint32_t height) {
    Screen *screen = data;
    zwlr_layer_surface_v1_ack_configure(layer, serial);
    if (width && height && width <= 16384 && height <= 16384) {
        configure_geometry(screen, (int)width, (int)height);
        screen->configured = true; screen->redraw = true;
    }
}
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *layer) { (void)layer; ((Screen *)data)->removed = true; }
static const struct zwlr_layer_surface_v1_listener layer_listener = {.configure = layer_configure, .closed = layer_closed};
static void xdg_configure(void *data, struct xdg_surface *surface, uint32_t serial) {
    Screen *screen = data; xdg_surface_ack_configure(surface, serial);
    screen->configured = true; screen->redraw = true;
}
static const struct xdg_surface_listener xdg_listener = {.configure = xdg_configure};
static void toplevel_configure(void *data, struct xdg_toplevel *toplevel, int32_t width, int32_t height, struct wl_array *states) {
    (void)toplevel; (void)states;
    Screen *screen = data;
    if (width > 0 && height > 0 && width <= 16384 && height <= 16384) {
        configure_geometry(screen, width, height);
    }
}
static void toplevel_close(void *data, struct xdg_toplevel *toplevel) { (void)toplevel; ((Screen *)data)->app->quit = true; }
static const struct xdg_toplevel_listener toplevel_listener = {.configure = toplevel_configure, .close = toplevel_close};
static void wm_ping(void *data, struct xdg_wm_base *wm, uint32_t serial) { (void)data; xdg_wm_base_pong(wm, serial); }
static const struct xdg_wm_base_listener wm_listener = {.ping = wm_ping};
static void configure_wallpaper_surface(Screen *screen) {
    App *app = screen->app;
    bool saver = app->saver_layer;
    /* A NULL-buffer unmap resets layer-shell state. Reapply the complete
     * initial state before its bufferless remap commit and fresh configure. */
    zwlr_layer_surface_v1_set_size(screen->layer, 0, 0);
    zwlr_layer_surface_v1_set_anchor(screen->layer, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP
        | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    /* Fill beneath reserved panels, including transparent bars. The layer
     * controls stacking; a negative zone does not reserve workspace space. */
    zwlr_layer_surface_v1_set_exclusive_zone(screen->layer, -1);
    if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2)
        zwlr_layer_surface_v1_set_layer(screen->layer, saver ? ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY
            : screen->mode == VIEW_HUMAN ? ZWLR_LAYER_SHELL_V1_LAYER_TOP : ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
    zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer, saver || screen->mode == VIEW_HUMAN
        ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
    struct wl_region *empty = !saver && screen->mode == VIEW_HIDDEN ? wl_compositor_create_region(app->compositor) : NULL;
    wl_surface_set_input_region(screen->surface, empty);
    if (empty) wl_region_destroy(empty);
}
static void wallpaper_visibility(Screen *screen) {
    if (!screen->layer || screen->app->saver) return;
    if (screen->mode == VIEW_HIDDEN && screen->mapped) {
        if (screen->callback) { wl_callback_destroy(screen->callback); screen->callback = NULL; }
        wl_surface_attach(screen->surface, NULL, 0, 0);
        wl_surface_commit(screen->surface);
        screen->mapped = false; screen->unmapped = true;
        screen->configured = false; screen->ready = true;
        screen->redraw = false;
        char event[256]; snprintf(event, sizeof(event), "wallpaper %s unmapped", screen->name);
        diagnostic(screen->app, event);
    } else if (screen->mode != VIEW_HIDDEN && screen->unmapped) {
        configure_wallpaper_surface(screen);
        wl_surface_commit(screen->surface);
        screen->unmapped = false; screen->ready = true; screen->redraw = true; screen->present_urgent = true;
        char event[256]; snprintf(event, sizeof(event), "wallpaper %s remapping; waiting for configure", screen->name);
        diagnostic(screen->app, event);
    }
}
static void create_surface(Screen *screen) {
    App *app = screen->app;
    if (screen->surface || screen->removed || !app->compositor || !app->shm) return;
    bool layer = !app->saver || app->saver_layer;
    if (layer ? !app->layer_shell : !app->wm_base) return;
    screen->surface = wl_compositor_create_surface(app->compositor);
    screen->ready = true; screen->redraw = true;
    if (app->saver && !app->saver_layer) {
        screen->xdg = xdg_wm_base_get_xdg_surface(app->wm_base, screen->surface);
        xdg_surface_add_listener(screen->xdg, &xdg_listener, screen);
        screen->toplevel = xdg_surface_get_toplevel(screen->xdg);
        xdg_toplevel_add_listener(screen->toplevel, &toplevel_listener, screen);
        xdg_toplevel_set_app_id(screen->toplevel, "org.omarchy.doom.screensaver");
        xdg_toplevel_set_title(screen->toplevel, "DOOM Screensaver");
        xdg_toplevel_set_fullscreen(screen->toplevel, screen->output);
    } else {
        screen->layer = zwlr_layer_shell_v1_get_layer_surface(app->layer_shell, screen->surface,
            screen->output, app->saver_layer ? ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY : ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM,
            app->saver_layer ? "doom-screensaver" : "doom-desktop");
        zwlr_layer_surface_v1_add_listener(screen->layer, &layer_listener, screen);
        /* Wallpaper fills beneath panels; the disposable saver overlays the
         * complete output without a workspace/toplevel activation. */
        configure_wallpaper_surface(screen);
    }
    wl_surface_commit(screen->surface);
}
static void finish_output_update(Screen *screen) {
    create_surface(screen);
    if (screen->output_changed && screen->configured && screen->layer && !screen->removed) {
        /* Some compositors report a resized nested output without arranging
         * existing layers. Reassert compositor-sized geometry to request a
         * fresh configure, retaining the full-output negative exclusive zone.
         * Only layer_configure supplies the surface's drawable dimensions. */
        zwlr_layer_surface_v1_set_size(screen->layer, 0, 0);
        wl_surface_commit(screen->surface);
        char event[256]; snprintf(event, sizeof(event), "output %s changed; requesting layer geometry", screen->name);
        diagnostic(screen->app, event);
    }
    screen->output_changed = false;
}
static void output_geometry(void *data, struct wl_output *output, int32_t x, int32_t y, int32_t pw,
        int32_t ph, int32_t subpixel, const char *make, const char *model, int32_t transform) {
    (void)output; (void)x; (void)y; (void)pw; (void)ph; (void)subpixel; (void)make; (void)model;
    Screen *screen = data;
    if (screen->output_transform != transform) { screen->output_transform = transform; screen->output_changed = true; }
}
static void output_mode(void *data, struct wl_output *output, uint32_t flags, int32_t width, int32_t height, int32_t refresh) {
    (void)refresh;
    Screen *screen = data;
    if ((flags & WL_OUTPUT_MODE_CURRENT) && width > 0 && height > 0) {
        if (screen->output_width != width || screen->output_height != height) screen->output_changed = true;
        screen->output_width = width; screen->output_height = height;
        if (!screen->configured) { screen->width = width / screen->scale; screen->height = height / screen->scale; }
        if (output && wl_output_get_version(output) < 2) finish_output_update(screen);
    }
}
static void output_done(void *data, struct wl_output *output) { (void)output; finish_output_update(data); }
static void output_scale(void *data, struct wl_output *output, int32_t scale) {
    (void)output; Screen *screen = data;
    if (scale >= 1 && scale <= 8 && screen->scale != scale) {
        screen->scale = scale; screen->output_changed = true;
        schedule_geometry_frame(screen);
    }
}
static void output_name(void *data, struct wl_output *output, const char *name) {
    (void)output; Screen *screen = data;
    snprintf(screen->name, sizeof(screen->name), "%s", name); screen->app->next_query = 0;
    char event[256]; snprintf(event, sizeof(event), "output named %s (wl_output v%u)", screen->name, wl_output_get_version(screen->output));
    diagnostic(screen->app, event);
}
static void output_description(void *data, struct wl_output *output, const char *description) { (void)data; (void)output; (void)description; }
static const struct wl_output_listener output_listener = {
    .geometry = output_geometry, .mode = output_mode, .done = output_done, .scale = output_scale,
    .name = output_name, .description = output_description,
};
static void xdg_output_position(void *data, struct zxdg_output_v1 *output, int32_t x, int32_t y) { (void)data; (void)output; (void)x; (void)y; }
static void xdg_output_size(void *data, struct zxdg_output_v1 *output, int32_t width, int32_t height) {
    (void)output; Screen *screen = data;
    if (width > 0 && height > 0) {
        if (screen->logical_width != width || screen->logical_height != height) screen->output_changed = true;
        screen->logical_width = width; screen->logical_height = height;
        if (!screen->configured) { screen->width = width; screen->height = height; }
    }
}
static void xdg_output_done(void *data, struct zxdg_output_v1 *output) {
    if (zxdg_output_v1_get_version(output) < 3) finish_output_update(data);
}
static void xdg_output_name(void *data, struct zxdg_output_v1 *output, const char *name) {
    (void)output; output_name(data, NULL, name);
}
static void xdg_output_description(void *data, struct zxdg_output_v1 *output, const char *description) { (void)data; (void)output; (void)description; }
static const struct zxdg_output_v1_listener xdg_output_listener = {
    .logical_position = xdg_output_position, .logical_size = xdg_output_size, .done = xdg_output_done,
    .name = xdg_output_name, .description = xdg_output_description,
};
static void ensure_output_name(Screen *screen) {
    if (screen->xdg_output || !screen->app->output_manager || screen->removed) return;
    screen->xdg_output = zxdg_output_manager_v1_get_xdg_output(screen->app->output_manager, screen->output);
    zxdg_output_v1_add_listener(screen->xdg_output, &xdg_output_listener, screen);
}
static void registry_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    App *app = data;
    if (!strcmp(interface, wl_compositor_interface.name))
        app->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, version < 4 ? version : 4);
    else if (!strcmp(interface, wl_shm_interface.name)) app->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    else if (!strcmp(interface, zwlr_layer_shell_v1_interface.name))
        app->layer_shell = wl_registry_bind(registry, name, &zwlr_layer_shell_v1_interface, version < 4 ? version : 4);
    else if (!strcmp(interface, xdg_wm_base_interface.name)) {
        app->wm_base = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(app->wm_base, &wm_listener, app);
    } else if (!strcmp(interface, zxdg_output_manager_v1_interface.name)) {
        app->output_manager = wl_registry_bind(registry, name, &zxdg_output_manager_v1_interface, version < 3 ? version : 3);
        for (Screen *screen = app->screens; screen; screen = screen->next) ensure_output_name(screen);
    } else if (!strcmp(interface, wl_seat_interface.name) && !app->seat) {
        app->seat = wl_registry_bind(registry, name, &wl_seat_interface, version < 5 ? version : 5);
        wl_seat_add_listener(app->seat, &seat_listener, app);
    } else if (!strcmp(interface, zwp_relative_pointer_manager_v1_interface.name))
        app->relative_manager = wl_registry_bind(registry, name, &zwp_relative_pointer_manager_v1_interface, 1);
    else if (!strcmp(interface, zwp_pointer_constraints_v1_interface.name))
        app->constraints = wl_registry_bind(registry, name, &zwp_pointer_constraints_v1_interface, 1);
    else if (!strcmp(interface, wl_output_interface.name)) {
        Screen *screen = calloc(1, sizeof(*screen)); if (!screen) fail("out of memory");
        screen->app = app; screen->global = name; screen->scale = 1;
        screen->width = 1280; screen->height = 720; screen->mode = VIEW_HIDDEN;
        screen->mode_changed_at = now_ms();
        screen->snapshot = malloc(DD_FRAME_BYTES); if (!screen->snapshot) fail("out of memory");
        snprintf(screen->name, sizeof(screen->name), "output-%u", name);
        screen->output = wl_registry_bind(registry, name, &wl_output_interface, version < 4 ? version : 4);
        wl_output_add_listener(screen->output, &output_listener, screen);
        screen->next = app->screens; app->screens = screen;
        char event[256]; snprintf(event, sizeof(event), "output global %u advertised v%u (bound v%u)", name, version, wl_output_get_version(screen->output));
        diagnostic(app, event);
        ensure_output_name(screen);
    }
}
static void registry_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)registry; App *app = data;
    for (Screen *screen = app->screens; screen; screen = screen->next) if (screen->global == name) screen->removed = true;
}
static const struct wl_registry_listener registry_listener = {.global = registry_global, .global_remove = registry_remove};
static void screen_destroy(Screen *screen) {
    App *app = screen->app;
    if (app->human == screen) { return_bot(app, "output removed"); app->human = NULL; }
    if (app->pointer_focus == screen) app->pointer_focus = NULL;
    if (app->keyboard_focus == screen) app->keyboard_focus = NULL;
    if (screen->callback) wl_callback_destroy(screen->callback);
    if (screen->toplevel) xdg_toplevel_destroy(screen->toplevel);
    if (screen->xdg) xdg_surface_destroy(screen->xdg);
    if (screen->layer) zwlr_layer_surface_v1_destroy(screen->layer);
    if (screen->surface) wl_surface_destroy(screen->surface);
    if (screen->xdg_output) zxdg_output_v1_destroy(screen->xdg_output);
    if (screen->output) {
        if (wl_output_get_version(screen->output) >= 3) wl_output_release(screen->output);
        else wl_output_destroy(screen->output);
    }
    retire_screen_buffers(screen); free(screen->snapshot); free(screen->blurred); free(screen);
}
static void remove_screens(App *app) {
    Screen **link = &app->screens;
    while (*link) {
        Screen *screen = *link;
        if (screen->removed) { *link = screen->next; screen_destroy(screen); }
        else link = &screen->next;
    }
}
static void update_views(App *app) {
    Screen *human = NULL;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        enum ViewMode mode = VIEW_HIDDEN; bool audible = false; unsigned present_stride = 1;
        if (app->saver) { mode = VIEW_BOT; audible = app->shared && app->shared->audible; }
        else {
            char request[256], reply[256], word[32]; int sound, blur = 20, reserved[4] = {0}, stride = 1;
            snprintf(request, sizeof(request), "view %s", screen->name);
            int fields = command(app, request, reply, sizeof(reply))
                ? sscanf(reply, "STATE %31s %d %d %d %d %d %d %d", word, &sound, &blur,
                    &reserved[0], &reserved[1], &reserved[2], &reserved[3], &stride) : 0;
            bool valid = fields >= 2;
            if (valid && strcmp(word, "bot") && strcmp(word, "human") && strcmp(word, "paused") && strcmp(word, "hidden")) valid = false;
            if (valid) {
                /* Older controllers omit reserved geometry. Do not use a
                 * partial tail; each output owns its complete set of insets. */
                if (fields < 7) memset(reserved, 0, sizeof(reserved));
                if (fields == 8 && stride == 4 && !strcmp(word, "bot")) present_stride = 4;
                for (int i = 0; i < 4; ++i) {
                    reserved[i] = reserved[i] < 0 ? 0 : reserved[i] > 16384 ? 16384 : reserved[i];
                    if (screen->reserved[i] != reserved[i]) {
                        screen->reserved[i] = reserved[i]; screen->redraw = true; screen->present_urgent = true;
                    }
                }
                blur = blur < 0 ? 0 : blur > 100 ? 100 : blur;
                if (screen->blur_strength != blur) { screen->blur_strength = blur; screen->blur_valid = false; screen->redraw = true; }
                if (!strcmp(word, "bot")) mode = VIEW_BOT;
                else if (!strcmp(word, "human")) mode = VIEW_HUMAN;
                else if (!strcmp(word, "paused")) mode = VIEW_PAUSED;
                audible = sound != 0;
                if (!screen->last_view_success || mode != screen->mode || audible != screen->audible) {
                    char event[256]; snprintf(event, sizeof(event), "view %s -> %s sound=%d", screen->name, word, sound);
                    diagnostic(app, event);
                }
                screen->last_view_success = now_ms(); screen->query_failed = false;
            } else {
                if (!screen->query_failed) diagnostic(app, "view query temporarily unavailable; retaining recent policy for 500 ms");
                screen->query_failed = true;
                if (screen->last_view_success && now_ms() - screen->last_view_success <= 500) {
                    mode = screen->mode; audible = screen->audible;
                    present_stride = screen->present_stride;
                }
            }
        }
        if (screen->present_stride != present_stride) {
            screen->present_stride = present_stride; screen->redraw = true;
            screen->image_presented = false;
            /* Promoting a busy output must not wait for a callback withheld
             * while it was occluded. Rearm one frame, then normal throttling. */
            schedule_geometry_frame(screen);
        }
        if (mode != screen->mode || audible != screen->audible) {
            if (screen->layer && !screen->unmapped && mode != screen->mode) {
                struct wl_region *empty = NULL;
                if (mode == VIEW_HIDDEN) empty = wl_compositor_create_region(app->compositor);
                wl_surface_set_input_region(screen->surface, empty);
                if (empty) wl_region_destroy(empty);
                wl_surface_commit(screen->surface);
            }
            if (screen->layer && !screen->unmapped && (mode == VIEW_HUMAN || screen->mode == VIEW_HUMAN)) {
                /* The layer-shell protocol only guarantees exclusive focus on
                 * top/overlay. Promote during takeover, then restore wallpaper
                 * stacking as soon as work resumes or the player returns. */
                if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2)
                    zwlr_layer_surface_v1_set_layer(screen->layer, mode == VIEW_HUMAN ? ZWLR_LAYER_SHELL_V1_LAYER_TOP : ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
                zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer,
                    mode == VIEW_HUMAN ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE
                    : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
                wl_surface_commit(screen->surface);
            }
            screen->mode = mode; screen->audible = audible; screen->redraw = true; screen->present_urgent = true;
            screen->mode_changed_at = now_ms();
        }
        wallpaper_visibility(screen);
        if (mode == VIEW_HUMAN) human = screen;
        /* First snapshot paints a useful initial image even if it is paused. */
        if (!screen->snapshot_valid && snapshot(screen)) screen->redraw = true;
    }
    if (human != app->human) {
        char event[384]; snprintf(event, sizeof(event), "controller ownership: %s -> %s",
            app->human ? app->human->name : "bot", human ? human->name : "bot");
        diagnostic(app, event);
        /* The controller has already changed ownership and released native
         * holds. Discard the old lease before clearing local refs; a saturated
         * queue must not reenter return_bot and overwrite this newer policy. */
        input_close(app); app->input_failed = true; release_keys(app);
        release_lock(app); app->human = human;
        app->input_failed = false; set_cursor(app);
    }
    ensure_lock(app);
    app->next_query = now_ms() + 200;
}
static void try_frame_mapping(App *app) {
    if (app->shared) return;
    int fd = open(app->frame_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    struct stat status;
    if (fstat(fd, &status) || status.st_size < DD_FILE_SIZE) { close(fd); return; }
    void *mapping = mmap(NULL, DD_FILE_SIZE, PROT_READ, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) { close(fd); return; }
    app->shared = mapping; app->frame_fd = fd;
}
static int idle_timeout(App *app) {
    int timeout = 200;
    uint64_t current = now_ms();
    if (app->next_query <= current) return 0;
    if (app->next_query - current < (uint64_t)timeout) timeout = (int)(app->next_query - current);
    if (app->repeat_at) {
        int repeat_timeout = app->repeat_at > current ? (int)(app->repeat_at - current) : 0;
        if (repeat_timeout < timeout) timeout = repeat_timeout;
    }
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        if (screen->mode == VIEW_HUMAN && !app->saver) {
            /* This header is our stable frame snapshot. Appended fields in
             * older engines are zero, so retain the previous60Hz fallback. */
            unsigned fps = screen->header.render_fps;
            if (fps < 35 || fps > 120) fps = 60;
            int frame_timeout = 1000 / (int)fps;
            if (timeout > frame_timeout) timeout = frame_timeout;
        } else if (screen->mode == VIEW_BOT ||
            (screen->blur_mix != screen->blur_target && current - screen->blur_changed_at < 120)) {
            if (timeout > 16) timeout = 16;
        }
    }
    return timeout;
}
static void cleanup(App *app) {
    if (!app->saver && app->human) return_bot(app, "viewer stopping");
    input_close(app);
    release_lock(app);
    while (app->screens) { Screen *screen = app->screens; app->screens = screen->next; screen_destroy(screen); }
    while (app->buffers) { Buffer *buffer = app->buffers; app->buffers = buffer->next; buffer_destroy(buffer); }
    if (app->keyboard) wl_keyboard_release(app->keyboard);
    if (app->pointer) wl_pointer_release(app->pointer);
    if (app->seat) wl_seat_release(app->seat);
    if (app->cursor_surface) wl_surface_destroy(app->cursor_surface);
    if (app->cursor_theme) wl_cursor_theme_destroy(app->cursor_theme);
    if (app->constraints) zwp_pointer_constraints_v1_destroy(app->constraints);
    if (app->relative_manager) zwp_relative_pointer_manager_v1_destroy(app->relative_manager);
    if (app->layer_shell) {
        if (zwlr_layer_shell_v1_get_version(app->layer_shell) >= 3) zwlr_layer_shell_v1_destroy(app->layer_shell);
        else wl_proxy_destroy((struct wl_proxy *)app->layer_shell);
    }
    if (app->wm_base) xdg_wm_base_destroy(app->wm_base);
    if (app->output_manager) zxdg_output_manager_v1_destroy(app->output_manager);
    if (app->shm) wl_shm_destroy(app->shm);
    if (app->compositor) wl_compositor_destroy(app->compositor);
    if (app->registry) wl_registry_destroy(app->registry);
    if (app->display) { wl_display_flush(app->display); wl_display_disconnect(app->display); }
    xkb_state_unref(app->xkb_state); xkb_keymap_unref(app->keymap); xkb_context_unref(app->xkb_context);
    if (app->shared) munmap((void *)app->shared, DD_FILE_SIZE);
    if (app->frame_fd >= 0) close(app->frame_fd);
    free(app->scratch);
}
int main(int argc, char **argv) {
    App app = {.frame_fd = -1};
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--frame") && i + 1 < argc) app.frame_path = argv[++i];
        else if (!strcmp(argv[i], "--socket") && i + 1 < argc) app.socket_path = argv[++i];
        else if (!strcmp(argv[i], "--screensaver")) app.saver = true;
        else if (!strcmp(argv[i], "--screensaver-layer")) app.saver = app.saver_layer = true;
        else if (!strcmp(argv[i], "--preview")) app.preview = true;
        else if (!strcmp(argv[i], "--omarchy-marker") && i + 1 < argc) ++i;
        else if (!strcmp(argv[i], "--help")) {
            puts("Usage: doom-viewer --frame PATH --socket PATH [--screensaver | --screensaver-layer] [--preview]"); return 0;
        } else { fprintf(stderr, "doom-viewer: unknown or incomplete argument: %s\n", argv[i]); return 2; }
    }
    if (app.preview && !app.saver) fail("--preview requires --screensaver or --screensaver-layer");
    if (!app.frame_path || !app.socket_path) fail("--frame and --socket are required");
    app.scratch = malloc(DD_FRAME_BYTES); if (!app.scratch) fail("out of memory");
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    app.started = now_ms(); app.xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!app.xkb_context) fail("cannot initialize keyboard support");
    app.display = wl_display_connect(NULL); if (!app.display) fail("cannot connect to the Wayland desktop");
    app.registry = wl_display_get_registry(app.display);
    wl_registry_add_listener(app.registry, &registry_listener, &app);
    if (wl_display_roundtrip(app.display) < 0 || wl_display_roundtrip(app.display) < 0) fail("compositor disconnected during setup");
    if (!app.compositor || !app.shm || ((app.saver && !app.saver_layer) ? !app.wm_base : !app.layer_shell)) fail("required Wayland protocols are unavailable");
    if (!app.saver && (!app.constraints || !app.relative_manager)) fail("relative mouse and pointer-lock protocols are required for takeover");
    app.cursor_theme = wl_cursor_theme_load(NULL, 24, app.shm);
    if (app.cursor_theme) app.cursor = wl_cursor_theme_get_cursor(app.cursor_theme, "left_ptr");
    if (app.cursor) app.cursor_surface = wl_compositor_create_surface(app.compositor);
    for (Screen *screen = app.screens; screen; screen = screen->next) create_surface(screen);
    int display_fd = wl_display_get_fd(app.display);
    int status = 0;
    while (!app.quit && !interrupted) {
        if (wl_display_dispatch_pending(app.display) < 0) { status = 1; break; }
        remove_screens(&app); collect_buffers(&app);
        if (now_ms() >= app.next_query) { try_frame_mapping(&app); update_views(&app); }
        repeat_keys(&app); input_service(&app);
        for (Screen *screen = app.screens; screen; screen = screen->next) {
            blur_animate(screen, now_ms());
            caption_animate(screen, now_ms());
            if (screen->ready && (screen->mode == VIEW_BOT || screen->mode == VIEW_HUMAN) && snapshot(screen)) screen->redraw = true;
            render(screen);
        }
        int prepared;
        while ((prepared = wl_display_prepare_read(app.display)) != 0) {
            if (wl_display_dispatch_pending(app.display) < 0) { status = 1; app.quit = true; break; }
        }
        if (app.quit) { if (prepared == 0) wl_display_cancel_read(app.display); break; }
        int flushed = wl_display_flush(app.display);
        struct pollfd pfds[2] = {{.fd = display_fd, .events = POLLIN},
            {.fd = app.input_active ? app.input_fd : -1, .events = POLLIN}};
        if (app.input_connecting || app.input_sent < app.input_used) pfds[1].events |= POLLOUT;
        if (flushed < 0 && errno == EAGAIN) pfds[0].events |= POLLOUT;
        else if (flushed < 0) { wl_display_cancel_read(app.display); status = 1; break; }
        int result = poll(pfds, 2, idle_timeout(&app));
        if (result > 0 && (pfds[0].revents & POLLIN)) {
            if (wl_display_read_events(app.display) < 0) { status = 1; break; }
        } else wl_display_cancel_read(app.display);
        if (result < 0 && errno != EINTR) { status = 1; break; }
        if (result > 0 && (pfds[0].revents & (POLLERR | POLLHUP | POLLNVAL))) { status = 1; break; }
        if (result > 0 && pfds[1].revents) input_service(&app);
    }
    cleanup(&app);
    return status;
}
