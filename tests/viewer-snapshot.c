// SPDX-License-Identifier: 0BSD
/* Exercise the real framebuffer consumer while another process publishes
 * frames. No compositor connection or desktop input is used by this test. */
#define main viewer_program_main
#include "../src/viewer.c"
#undef main
#include <sys/wait.h>

static void check(bool condition, const char *expression, int line) {
    if (!condition) {
        fprintf(stderr, "viewer test failed at line %d: %s\n", line, expression);
        exit(1);
    }
}
#define CHECK(expression) check((expression), #expression, __LINE__)

static void check_image_commit(int fd, struct wl_surface *surface) {
    uint32_t wire[128]; ssize_t bytes = read(fd, wire, sizeof(wire)); CHECK(bytes > 0);
    unsigned attached = 0, damaged = 0, committed = 0;
    for (size_t offset = 0; offset < (size_t)bytes / 4;) {
        uint32_t size = wire[offset + 1] >> 16, opcode = wire[offset + 1] & 0xffff;
        CHECK(size >= 8 && offset + size / 4 <= (size_t)bytes / 4);
        if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)surface)) {
            attached += opcode == WL_SURFACE_ATTACH;
            damaged += opcode == WL_SURFACE_DAMAGE_BUFFER;
            committed += opcode == WL_SURFACE_COMMIT;
        }
        offset += size / 4;
    }
    CHECK(attached == 1 && damaged == 1 && committed == 1);
}

static void test_duplicate_presentation(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    struct wl_surface *surface = (struct wl_surface *)wl_proxy_create((struct wl_proxy *)display, &wl_surface_interface);
    DDHeader *shared = mmap(NULL, DD_FILE_SIZE, PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0); CHECK(shared != MAP_FAILED);
    shared->magic = DD_MAGIC; shared->version = DD_VERSION; shared->pid = 123;
    shared->width = 320; shared->height = 200; shared->stride = 320 * 4; shared->frame = 1;
    snprintf(shared->map, sizeof(shared->map), "E1M1");
    App app = {.display = display, .shared = shared, .scratch = malloc(DD_FRAME_BYTES)};
    Screen screen = {.app = &app, .surface = surface, .configured = true, .ready = true,
        .mode = VIEW_HUMAN, .width = 320, .height = 200, .scale = 1,
        .mode_changed_at = now_ms(), .snapshot = malloc(DD_FRAME_BYTES)};
    Buffer buffer = {.width = 320, .height = 200, .stride = 320 * 4, .bytes = 320 * 200 * 4};
    buffer.pixels = calloc(1, buffer.bytes);
    buffer.wl = (struct wl_buffer *)wl_proxy_create((struct wl_proxy *)display, &wl_buffer_interface);
    CHECK(surface && app.scratch && screen.snapshot && buffer.pixels && buffer.wl);
    screen.buffers[0] = &buffer;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    CHECK(screen.caption_help_visible && screen.callback);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    /* Observation publication continues while the game's image is static.
     * Neither fresh sequence numbers nor game tics may upload that image. */
    for (unsigned i = 1; i <= 100; ++i) {
        __atomic_store_n(&shared->seq, i * 2 - 1, __ATOMIC_RELEASE);
        shared->tic = i; shared->agent_gen = i; shared->render_fps = 35;
        __atomic_store_n(&shared->seq, i * 2, __ATOMIC_RELEASE);
        CHECK(!snapshot(&screen));
        CHECK(screen.header.tic == i && screen.header.agent_gen == i && screen.header.render_fps == 35);
        caption_animate(&screen, now_ms()); render(&screen);
        CHECK(wl_display_flush(display) == 0);
    }
    uint32_t wire[16]; CHECK(read(pair[1], wire, sizeof(wire)) == -1 && errno == EAGAIN);
    /* A local caption expiry repaints once even though the image is static. */
    screen.mode_changed_at = now_ms() - 8001;
    caption_animate(&screen, now_ms()); CHECK(screen.redraw); render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    CHECK(!screen.caption_help_visible);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    caption_animate(&screen, now_ms()); render(&screen); CHECK(wl_display_flush(display) == 0);
    /* Header-only map changes update the caption; unchanged maps stay idle. */
    __atomic_store_n(&shared->seq, 201, __ATOMIC_RELEASE);
    snprintf(shared->map, sizeof(shared->map), "E1M2");
    __atomic_store_n(&shared->seq, 202, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    CHECK(!snapshot(&screen)); render(&screen); CHECK(wl_display_flush(display) == 0);
    /* A changed game image still causes exactly one normal image commit. */
    __atomic_store_n(&shared->seq, 203, __ATOMIC_RELEASE);
    ((uint32_t *)((uint8_t *)shared + DD_HEADER_SIZE))[0] = 0x00ffffff;
    shared->frame = 2;
    __atomic_store_n(&shared->seq, 204, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0);
    wl_proxy_destroy((struct wl_proxy *)buffer.wl); wl_proxy_destroy((struct wl_proxy *)surface);
    free(buffer.pixels); free(screen.snapshot); free(app.scratch); munmap(shared, DD_FILE_SIZE);
    wl_display_disconnect(display); close(pair[1]);
    puts("viewer duplicate images: 100 tic/state updates preserve the image without attaches, damage or commits; local caption expiry/map and changed images present once");
}

static void test_busy_presentation_stride(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    struct wl_surface *surface = (struct wl_surface *)wl_proxy_create((struct wl_proxy *)display, &wl_surface_interface);
    DDHeader *shared = mmap(NULL, DD_FILE_SIZE, PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0); CHECK(shared != MAP_FAILED);
    shared->magic = DD_MAGIC; shared->version = DD_VERSION; shared->pid = 123;
    shared->width = 320; shared->height = 200; shared->stride = 320 * 4; shared->frame = 1;
    snprintf(shared->map, sizeof(shared->map), "E1M1");
    App app = {.display = display, .shared = shared, .scratch = malloc(DD_FRAME_BYTES)};
    Screen screen = {.app = &app, .surface = surface, .configured = true, .ready = true,
        .mode = VIEW_BOT, .present_stride = 4, .width = 320, .height = 200, .scale = 1,
        .mode_changed_at = now_ms(), .snapshot = malloc(DD_FRAME_BYTES)};
    Buffer buffer = {.width = 320, .height = 200, .stride = 320 * 4, .bytes = 320 * 200 * 4};
    buffer.pixels = calloc(1, buffer.bytes);
    buffer.wl = (struct wl_buffer *)wl_proxy_create((struct wl_proxy *)display, &wl_buffer_interface);
    CHECK(surface && app.scratch && screen.snapshot && buffer.pixels && buffer.wl);
    screen.buffers[0] = &buffer;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    unsigned commits = 0;
    for (uint64_t tic = 1; tic <= 35; ++tic) {
        __atomic_store_n(&shared->seq, (uint32_t)tic * 2 - 1, __ATOMIC_RELEASE);
        shared->tic = tic; shared->frame = tic + 1; shared->items = (int32_t)tic;
        ((uint32_t *)((uint8_t *)shared + DD_HEADER_SIZE))[0] = (uint32_t)tic;
        __atomic_store_n(&shared->seq, (uint32_t)tic * 2, __ATOMIC_RELEASE);
        bool changed = snapshot(&screen);
        CHECK(changed == (tic % 4 == 0));
        CHECK(screen.observed_header.tic == tic && screen.observed_header.items == (int32_t)tic);
        if (changed) screen.redraw = true;
        caption_animate(&screen, now_ms()); render(&screen);
        if (changed) {
            CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface); ++commits;
            CHECK(screen.last_present_tic == tic && screen.snapshot[0] == tic);
            frame_done(&screen, screen.callback, 0); buffer.busy = false;
        } else {
            CHECK(screen.header.tic == tic / 4 * 4 && screen.snapshot[0] == tic / 4 * 4);
            CHECK(wl_display_flush(display) == 0);
        }
    }
    CHECK(commits == 8 && screen.last_present_tic == 32);
    /* Missed polls do not require observing a tic divisible by four. */
    shared->tic = 39; shared->frame = 40; shared->seq = 80;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    CHECK(screen.last_present_tic == 39);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    /* Pending caption/blur redraws cannot leak a commit on a skipped tic. */
    shared->tic = 40; shared->frame = 41; shared->seq = 82;
    screen.redraw = true; CHECK(!snapshot(&screen)); render(&screen);
    CHECK(wl_display_flush(display) == 0 && screen.last_present_tic == 39);
    /* A due frame without a free buffer does not consume the cadence. */
    shared->tic = 43; shared->frame = 44; shared->seq = 88;
    CHECK(snapshot(&screen)); screen.redraw = true; buffer.busy = true; screen.buffers[1] = &buffer; render(&screen);
    CHECK(wl_display_flush(display) == 0 && screen.last_present_tic == 39);
    shared->tic = 44; shared->frame = 45; shared->seq = 90;
    CHECK(snapshot(&screen)); buffer.busy = false; screen.buffers[1] = NULL; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    CHECK(screen.last_present_tic == 44);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    /* Another output's full-rate scratch copy cannot defeat this output. */
    Screen full = {.app = &app, .mode = VIEW_BOT, .present_stride = 1, .snapshot = malloc(DD_FRAME_BYTES)};
    CHECK(full.snapshot);
    shared->tic = 45; shared->frame = 46; shared->seq = 92;
    CHECK(snapshot(&full)); CHECK(!snapshot(&screen)); render(&screen);
    CHECK(wl_display_flush(display) == 0 && screen.last_present_tic == 44);
    /* Geometry/state transitions force one immediate image despite cadence. */
    schedule_geometry_frame(&screen); CHECK(snapshot(&screen)); render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    CHECK(screen.last_present_tic == 45 && !screen.present_urgent);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    shared->tic = 46; shared->frame = 47; shared->seq = 94; screen.mode = VIEW_HUMAN;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    shared->tic = 47; shared->frame = 48; shared->seq = 96; screen.mode = VIEW_BOT; app.saver = true;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0); buffer.busy = false; app.saver = false;
    /* Engine PID, tic and image rollbacks begin a fresh presentation epoch. */
    shared->pid = 124; shared->tic = 1; shared->frame = 1; shared->seq = 98;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0); buffer.busy = false;
    shared->tic = 0; shared->frame = 0; shared->seq = 100;
    CHECK(snapshot(&screen)); screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) > 0); check_image_commit(pair[1], surface);
    frame_done(&screen, screen.callback, 0);
    wl_proxy_destroy((struct wl_proxy *)buffer.wl); wl_proxy_destroy((struct wl_proxy *)surface);
    free(buffer.pixels); free(full.snapshot); free(screen.snapshot); free(app.scratch); munmap(shared, DD_FILE_SIZE);
    wl_display_disconnect(display); close(pair[1]);
    puts("viewer busy output: every four elapsed world tics; skipped tics copy no pixels and attach/damage/commit nothing; pending redraw/buffer failures/scratch reuse/urgent geometry/human/saver/restarts covered");
}

static void test_output_resize(void) {
    int pair[2];
    CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    struct wl_surface *surface = (struct wl_surface *)wl_proxy_create((struct wl_proxy *)display, &wl_surface_interface);
    struct zwlr_layer_surface_v1 *layer = (struct zwlr_layer_surface_v1 *)wl_proxy_create(
        (struct wl_proxy *)display, &zwlr_layer_surface_v1_interface);
    CHECK(surface && layer);
    App app = {.display = display, .started = now_ms()};
    Screen screen = {.app = &app, .surface = surface, .layer = layer, .configured = true,
        .mode = VIEW_BOT,
        .width = 1701, .height = 686, .scale = 1, .output_width = 1701, .output_height = 686,
        .logical_width = 1701, .logical_height = 686};
    snprintf(screen.name, sizeof(screen.name), "RESIZE-TEST");
    /* Reproduce a configured nested output growing after its peer is removed.
     * Surface dimensions stay authoritative until the compositor configures
     * them; a real wire request must trigger fresh layer arrangement. */
    output_mode(&screen, NULL, WL_OUTPUT_MODE_CURRENT, 1701, 1386, 60000);
    xdg_output_size(&screen, NULL, 1701, 1386);
    CHECK(screen.width == 1701 && screen.height == 686 && screen.output_changed);
    output_done(&screen, NULL);
    CHECK(wl_display_flush(display) > 0 && !screen.output_changed);
    uint32_t wire[16] = {0};
    CHECK(read(pair[1], wire, sizeof(wire)) == 24);
    CHECK(wire[0] == wl_proxy_get_id((struct wl_proxy *)layer));
    CHECK(wire[1] == (16u << 16 | ZWLR_LAYER_SURFACE_V1_SET_SIZE) && !wire[2] && !wire[3]);
    CHECK(wire[4] == wl_proxy_get_id((struct wl_proxy *)surface));
    CHECK(wire[5] == (8u << 16 | WL_SURFACE_COMMIT));
    /* A repeated unchanged batch must not create a configure/commit loop. */
    output_mode(&screen, NULL, WL_OUTPUT_MODE_CURRENT, 1701, 1386, 60000);
    xdg_output_size(&screen, NULL, 1701, 1386); output_done(&screen, NULL);
    CHECK(wl_display_flush(display) == 0);
    CHECK(read(pair[1], wire, sizeof(wire)) == -1 && errno == EAGAIN);
    /* Reproduce a withheld old frame callback: output geometry is ready but
     * the old surface/input bounds remain1701x686. A genuine configure must
     * permit one resized buffer commit without waiting for that callback. */
    screen.callback = (struct wl_callback *)wl_proxy_create((struct wl_proxy *)display, &wl_callback_interface);
    CHECK(screen.callback); screen.ready = false; screen.redraw = false;
    /* The authoritative configure reserves a bar. Raw output mode must not
     * replace that smaller drawable area, for wallpaper or fullscreen. */
    layer_configure(&screen, layer, 123, 1701, 1356);
    CHECK(screen.width == 1701 && screen.height == 1356 && screen.redraw && screen.ready && !screen.callback);
    CHECK(wl_display_flush(display) > 0);
    CHECK(read(pair[1], wire, sizeof(wire)) == 12); /* ack_configure only. */
    Buffer buffer = {.width = 1701, .height = 1356, .stride = 1701 * 4, .bytes = (size_t)1701 * 1356 * 4};
    buffer.pixels = calloc(1, buffer.bytes);
    buffer.wl = (struct wl_buffer *)wl_proxy_create((struct wl_proxy *)display, &wl_buffer_interface);
    CHECK(buffer.pixels && buffer.wl); screen.buffers[0] = &buffer;
    render(&screen);
    CHECK(buffer.busy && !screen.ready && !screen.redraw && screen.callback);
    CHECK(wl_display_flush(display) > 0);
    uint32_t resized[64] = {0}; ssize_t bytes = read(pair[1], resized, sizeof(resized)); CHECK(bytes > 0);
    bool attached = false, damaged = false, committed = false;
    for (size_t offset = 0; offset < (size_t)bytes / 4;) {
        uint32_t size = resized[offset + 1] >> 16, opcode = resized[offset + 1] & 0xffff;
        CHECK(size >= 8 && offset + size / 4 <= (size_t)bytes / 4);
        if (resized[offset] == wl_proxy_get_id((struct wl_proxy *)surface)) {
            if (opcode == WL_SURFACE_ATTACH) attached = resized[offset + 2] == wl_proxy_get_id((struct wl_proxy *)buffer.wl);
            if (opcode == WL_SURFACE_DAMAGE_BUFFER) damaged = resized[offset + 4] == 1701 && resized[offset + 5] == 1356;
            if (opcode == WL_SURFACE_COMMIT) committed = true;
        }
        offset += size / 4;
    }
    CHECK(attached && damaged && committed);
    struct wl_callback *current_callback = screen.callback;
    layer_configure(&screen, layer, 124, 1701, 1356);
    CHECK(screen.callback == current_callback && !screen.ready); /* Normal unchanged throttling. */
    CHECK(wl_display_flush(display) > 0 && read(pair[1], wire, sizeof(wire)) == 12);
    render(&screen); CHECK(wl_display_flush(display) == 0); /* No extra frame loop. */
    wl_callback_destroy(screen.callback); screen.callback = NULL;
    screen.buffers[0] = NULL; wl_proxy_destroy((struct wl_proxy *)buffer.wl); free(buffer.pixels);
    output_mode(&screen, NULL, WL_OUTPUT_MODE_CURRENT, 1701, 1386, 60000); output_done(&screen, NULL);
    CHECK(screen.height == 1356 && wl_display_flush(display) == 0);
    /* Scale changes also request geometry; the compositor chooses logical
     * bounds. A saver has no layer state and waits for xdg configure instead. */
    screen.callback = (struct wl_callback *)wl_proxy_create((struct wl_proxy *)display, &wl_callback_interface);
    CHECK(screen.callback); screen.ready = false;
    output_scale(&screen, NULL, 2); output_done(&screen, NULL);
    CHECK(screen.scale == 2 && screen.height == 1356 && screen.ready && !screen.callback && wl_display_flush(display) > 0);
    CHECK(read(pair[1], wire, sizeof(wire)) == 24);
    screen.layer = NULL;
    screen.callback = (struct wl_callback *)wl_proxy_create((struct wl_proxy *)display, &wl_callback_interface);
    CHECK(screen.callback); screen.ready = false;
    output_mode(&screen, NULL, WL_OUTPUT_MODE_CURRENT, 1920, 1080, 60000); output_done(&screen, NULL);
    CHECK(screen.width == 1701 && screen.height == 1356 && !screen.ready && wl_display_flush(display) == 0);
    struct wl_array states = {0};
    toplevel_configure(&screen, NULL, 1920, 1080, &states);
    CHECK(screen.width == 1920 && screen.height == 1080 && screen.ready && !screen.callback);
    wl_proxy_destroy((struct wl_proxy *)layer); wl_proxy_destroy((struct wl_proxy *)surface);
    wl_display_disconnect(display); close(pair[1]);
    puts("viewer resize: output change requests compositor geometry; stalled old callbacks cannot block resized buffers; unchanged configure throttles; bar bounds/xdg saver/scale remain correct");
}

static void test_wallpaper_visibility(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    struct wl_surface *surface = (struct wl_surface *)wl_proxy_create((struct wl_proxy *)display, &wl_surface_interface);
    struct zwlr_layer_surface_v1 *layer = (struct zwlr_layer_surface_v1 *)wl_proxy_create(
        (struct wl_proxy *)display, &zwlr_layer_surface_v1_interface);
    CHECK(surface && layer);
    App app = {.display = display, .started = now_ms()};
    Screen screen = {.app = &app, .surface = surface, .layer = layer, .configured = true,
        .mapped = true, .mode = VIEW_HIDDEN, .redraw = true, .ready = false, .width = 1280, .height = 720, .scale = 1};
    screen.callback = (struct wl_callback *)wl_proxy_create((struct wl_proxy *)display, &wl_callback_interface);
    CHECK(screen.callback);
    wallpaper_visibility(&screen);
    CHECK(!screen.mapped && screen.unmapped && !screen.configured && screen.ready && !screen.redraw && !screen.callback);
    CHECK(wl_display_flush(display) > 0);
    uint32_t wire[128] = {0}; CHECK(read(pair[1], wire, sizeof(wire)) == 28);
    CHECK(wire[0] == wl_proxy_get_id((struct wl_proxy *)surface));
    CHECK(wire[1] == (20u << 16 | WL_SURFACE_ATTACH) && !wire[2] && !wire[3] && !wire[4]);
    CHECK(wire[5] == wl_proxy_get_id((struct wl_proxy *)surface) && wire[6] == (8u << 16 | WL_SURFACE_COMMIT));
    screen.redraw = true; render(&screen); wallpaper_visibility(&screen);
    CHECK(wl_display_flush(display) == 0); /* Hidden never reattaches a frame. */
    screen.mode = VIEW_PAUSED;
    wallpaper_visibility(&screen);
    CHECK(!screen.unmapped && !screen.configured && screen.ready && screen.redraw);
    CHECK(wl_display_flush(display) > 0);
    ssize_t bytes = read(pair[1], wire, sizeof(wire)); CHECK(bytes > 0);
    bool size = false, anchor = false, zone = false, keyboard = false, commit = false;
    for (size_t offset = 0; offset < (size_t)bytes / 4;) {
        uint32_t length = wire[offset + 1] >> 16, opcode = wire[offset + 1] & 0xffff;
        CHECK(length >= 8 && offset + length / 4 <= (size_t)bytes / 4);
        if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)layer)) {
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_SIZE) size = !wire[offset + 2] && !wire[offset + 3];
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_ANCHOR) anchor = wire[offset + 2] == 15;
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_EXCLUSIVE_ZONE) zone = (int32_t)wire[offset + 2] == -1;
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_KEYBOARD_INTERACTIVITY) keyboard = !wire[offset + 2];
        } else if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)surface)) {
            CHECK(opcode != WL_SURFACE_ATTACH); /* Fresh configure must come first. */
            if (opcode == WL_SURFACE_COMMIT) commit = true;
        }
        offset += length / 4;
    }
    CHECK(size && anchor && zone && keyboard && commit);
    render(&screen); CHECK(wl_display_flush(display) == 0);
    layer_configure(&screen, layer, 321, 1280, 720);
    CHECK(screen.configured && screen.width == 1280 && screen.height == 720);
    CHECK(wl_display_flush(display) > 0 && read(pair[1], wire, sizeof(wire)) == 12);
    screen.mode = VIEW_HUMAN;
    configure_wallpaper_surface(&screen);
    CHECK(wl_display_flush(display) > 0);
    bytes = read(pair[1], wire, sizeof(wire)); CHECK(bytes > 0);
    zone = keyboard = false;
    for (size_t offset = 0; offset < (size_t)bytes / 4;) {
        uint32_t length = wire[offset + 1] >> 16, opcode = wire[offset + 1] & 0xffff;
        CHECK(length >= 8 && offset + length / 4 <= (size_t)bytes / 4);
        if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)layer)) {
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_EXCLUSIVE_ZONE) zone = (int32_t)wire[offset + 2] == -1;
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_KEYBOARD_INTERACTIVITY)
                keyboard = wire[offset + 2] == ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE;
        }
        offset += length / 4;
    }
    CHECK(zone && keyboard); /* Takeover keeps the same full-output extent. */
    /* A newly started hidden viewer must never attach its first waiting image. */
    screen.mode = VIEW_HIDDEN; screen.redraw = true; render(&screen);
    CHECK(wl_display_flush(display) == 0);
    wl_proxy_destroy((struct wl_proxy *)layer); wl_proxy_destroy((struct wl_proxy *)surface);
    wl_display_disconnect(display); close(pair[1]);
    puts("viewer visibility: hidden detaches buffer/cancels callback; remap restores full-output geometry and waits for configure; initial hidden never attaches");
}

static void test_layer_saver(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    App app = {.display = display, .saver = true, .saver_layer = true, .started = now_ms()};
    app.compositor = (struct wl_compositor *)wl_proxy_create((struct wl_proxy *)display, &wl_compositor_interface);
    app.shm = (struct wl_shm *)wl_proxy_create((struct wl_proxy *)display, &wl_shm_interface);
    app.layer_shell = (struct zwlr_layer_shell_v1 *)wl_proxy_create(
        (struct wl_proxy *)display, &zwlr_layer_shell_v1_interface);
    CHECK(app.compositor && app.shm && app.layer_shell);
    Screen screen = {.app = &app, .scale = 1, .width = 1280, .height = 720, .mode = VIEW_HIDDEN};
    snprintf(screen.name, sizeof(screen.name), "SAVER-TEST"); app.screens = &screen;
    create_surface(&screen);
    CHECK(screen.surface && screen.layer && !screen.xdg && !screen.toplevel && screen.ready);
    CHECK(wl_display_flush(display) > 0);
    uint32_t wire[256] = {0}; ssize_t bytes = read(pair[1], wire, sizeof(wire)); CHECK(bytes > 0);
    bool overlay = false, anchors = false, zone = false, keyboard = false, input = false, commit = false;
    for (size_t offset = 0; offset < (size_t)bytes / 4;) {
        uint32_t length = wire[offset + 1] >> 16, opcode = wire[offset + 1] & 0xffff;
        CHECK(length >= 8 && offset + length / 4 <= (size_t)bytes / 4);
        if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)app.layer_shell)) {
            CHECK(opcode == ZWLR_LAYER_SHELL_V1_GET_LAYER_SURFACE);
            overlay = wire[offset + 5] == ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY;
            CHECK(!strcmp((const char *)&wire[offset + 7], "doom-screensaver"));
        } else if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)screen.layer)) {
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_ANCHOR) anchors = wire[offset + 2] == 15;
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_EXCLUSIVE_ZONE) zone = (int32_t)wire[offset + 2] == -1;
            if (opcode == ZWLR_LAYER_SURFACE_V1_SET_KEYBOARD_INTERACTIVITY)
                keyboard = wire[offset + 2] == ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE;
        } else if (wire[offset] == wl_proxy_get_id((struct wl_proxy *)screen.surface)) {
            if (opcode == WL_SURFACE_SET_INPUT_REGION) input = !wire[offset + 2];
            if (opcode == WL_SURFACE_COMMIT) commit = true;
        }
        offset += length / 4;
    }
    CHECK(overlay && anchors && zone && keyboard && input && commit);
    update_views(&app); CHECK(screen.mode == VIEW_BOT && !app.human && !app.input_active);
    layer_configure(&screen, screen.layer, 1, 1280, 720);
    CHECK(screen.configured && !screen.snapshot_valid && !app.shared);
    Buffer buffer = {.width = 1280, .height = 720, .stride = 1280 * 4, .bytes = (size_t)1280 * 720 * 4};
    buffer.pixels = calloc(1, buffer.bytes);
    buffer.wl = (struct wl_buffer *)wl_proxy_create((struct wl_proxy *)display, &wl_buffer_interface);
    CHECK(buffer.pixels && buffer.wl); screen.buffers[0] = &buffer;
    render(&screen); CHECK(screen.mapped && buffer.busy && screen.callback);
    CHECK(((uint32_t *)buffer.pixels)[0] != 0);
    CHECK(wl_display_flush(display) > 0 && read(pair[1], wire, sizeof(wire)) > 0);
    /* A preview's Enter release and pointer jitter cannot dismiss it before
     * an actual game image is presented, even after a slow cold start. */
    app.preview = true; app.started = now_ms() - 10000;
    CHECK(!app.saver_presented_at);
    keyboard_key(&app, NULL, 0, 0, KEY_ENTER, WL_KEYBOARD_KEY_STATE_RELEASED);
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_RELEASED);
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_PRESSED);
    app.pointer_seen = true; pointer_motion(&app, NULL, 0, wl_fixed_from_int(10), 0);
    CHECK(!app.quit);
    /* First real frame starts the grace; later frames do not restart it. */
    wl_callback_destroy(screen.callback); screen.callback = NULL;
    screen.snapshot = calloc(320 * 200, 4); CHECK(screen.snapshot);
    screen.snapshot_valid = true; screen.header.width = 320; screen.header.height = 200;
    buffer.busy = false; screen.ready = screen.redraw = true; render(&screen);
    CHECK(app.saver_presented_at && now_ms() - app.saver_presented_at < 100);
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_PRESSED);
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_PRESSED);
    pointer_motion(&app, NULL, 0, wl_fixed_from_int(11), 0);
    pointer_axis(&app, NULL, 0, 0, wl_fixed_from_int(1)); CHECK(!app.quit);
    app.saver_presented_at = now_ms() - 1501;
    uint64_t first = app.saver_presented_at;
    wl_callback_destroy(screen.callback); screen.callback = NULL;
    buffer.busy = false; screen.ready = screen.redraw = true; render(&screen);
    CHECK(app.saver_presented_at == first);
    keyboard_key(&app, NULL, 0, 0, KEY_ENTER, WL_KEYBOARD_KEY_STATE_RELEASED);
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_RELEASED); CHECK(!app.quit);
    pointer_motion(&app, NULL, 0, wl_fixed_from_int(20), 0); CHECK(app.quit); app.quit = false;
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_PRESSED); CHECK(app.quit); app.quit = false;
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_PRESSED); CHECK(app.quit); app.quit = false;
    app.preview = false; app.saver_presented_at = 0;
    /* Idle savers still dismiss on a deliberate press immediately. */
    keyboard_key(&app, NULL, 0, 0, KEY_ENTER, WL_KEYBOARD_KEY_STATE_RELEASED);
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_RELEASED); CHECK(!app.quit);
    app.keyboard_focus = &screen;
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_PRESSED);
    CHECK(app.quit && !app.input_used); app.quit = false;
    pointer_button(&app, NULL, 0, 0, BTN_LEFT, WL_POINTER_BUTTON_STATE_PRESSED);
    CHECK(app.quit && !app.input_used); app.quit = false;
    app.pointer_seen = true; app.pointer_x = app.pointer_y = 0; app.started = now_ms() - 600;
    pointer_motion(&app, NULL, 0, 0, 0); CHECK(!app.quit);
    pointer_motion(&app, NULL, 0, wl_fixed_from_int(10), 0);
    CHECK(app.quit && !app.human && !app.locked && !app.relative && !app.input_used);
    wl_callback_destroy(screen.callback);
    screen.buffers[0] = NULL; wl_proxy_destroy((struct wl_proxy *)buffer.wl); free(buffer.pixels);
    zwlr_layer_surface_v1_destroy(screen.layer); wl_surface_destroy(screen.surface);
    wl_proxy_destroy((struct wl_proxy *)app.layer_shell); wl_shm_destroy(app.shm); wl_compositor_destroy(app.compositor);
    wl_display_disconnect(display); close(pair[1]);
    free(screen.snapshot);
    puts("viewer layer saver: full-output/input; preview grace starts at first real frame despite delayed engine, ignores launch releases/jitter, expires once; true idle presses still dismiss immediately");
}

static void test_policy_outage(void) {
    char directory[] = "/tmp/doom-viewer-policy-XXXXXX";
    CHECK(mkdtemp(directory));
    char socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    snprintf(socket_path, sizeof(socket_path), "%s/control.sock", directory);
    int server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0); CHECK(server >= 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, socket_path);
    if (bind(server, (struct sockaddr *)&address, sizeof(address))) {
        int error = errno;
        fprintf(stderr, "viewer policy test could not bind its local socket: %s (%d).\n"
            "Run scripts/test-viewer.sh outside the socket-restricted sandbox.\n", strerror(error), error);
        close(server); rmdir(directory); exit(1);
    }
    CHECK(!listen(server, 1));
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        struct pollfd pfd = {.fd = server, .events = POLLIN};
        CHECK(poll(&pfd, 1, 1000) > 0);
        int client = accept(server, NULL, NULL); CHECK(client >= 0);
        struct timeval timeout = {.tv_sec = 1};
        CHECK(!setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)));
        char request[256]; CHECK(read(client, request, sizeof(request)) > 0);
        const char *reply = "STATE human 1\n";
        CHECK(send(client, reply, strlen(reply), MSG_NOSIGNAL) == (ssize_t)strlen(reply));
        close(client); close(server); _exit(0);
    }
    close(server);
    App app = {.socket_path = socket_path, .started = now_ms()};
    Screen screen = {.app = &app, .snapshot_valid = true};
    snprintf(screen.name, sizeof(screen.name), "TEST-1"); app.screens = &screen;
    update_views(&app);
    CHECK(app.human == &screen && screen.mode == VIEW_HUMAN && screen.audible && screen.blur_strength == 20);
    int status; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    update_views(&app);
    CHECK(app.human == &screen && screen.mode == VIEW_HUMAN && screen.audible);
    screen.last_view_success = now_ms() - 600;
    app.refs['w'] = 1; app.input_used = sizeof(app.input_queue) - 1;
    update_views(&app);
    CHECK(app.human == NULL && screen.mode == VIEW_HIDDEN && !app.refs['w'] && !app.input_used && !app.input_failed);
    unlink(socket_path); rmdir(directory);
    puts("viewer policy: brief controller outage preserves takeover; prolonged outage releases input");
}

static void test_reserved_policy(void) {
    char directory[] = "/tmp/live-doom-reserved-policy-XXXXXX"; CHECK(mkdtemp(directory));
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)]; snprintf(path, sizeof(path), "%s/control.sock", directory);
    int server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0); CHECK(server >= 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX}; strcpy(address.sun_path, path);
    CHECK(!bind(server, (struct sockaddr *)&address, sizeof(address)) && !listen(server, 4));
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        const char *replies[] = {"STATE bot 0 20 12 30 18 5 4\n", "STATE bot 0 20 0 60 0 0\n",
                                "STATE bot 0\n", "STATE bot 0 20 0 60 0 0\n",
                                "STATE bot 0 10\n", "STATE bot 0 20 0 60 0 0 999\n"};
        for (unsigned i = 0; i < sizeof(replies) / sizeof(replies[0]); ++i) {
            struct pollfd pfd = {.fd = server, .events = POLLIN}; CHECK(poll(&pfd, 1, 1000) > 0);
            int client = accept(server, NULL, NULL); CHECK(client >= 0);
            char request[256]; CHECK(read(client, request, sizeof(request)) > 0);
            CHECK(send(client, replies[i], strlen(replies[i]), MSG_NOSIGNAL) == (ssize_t)strlen(replies[i])); close(client);
        }
        close(server); _exit(0);
    }
    close(server);
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    struct wl_display *display = wl_display_connect_to_fd(pair[0]); CHECK(display);
    App app = {.display = display, .socket_path = path, .started = now_ms()};
    Screen second = {.app = &app, .snapshot_valid = true, .mode = VIEW_BOT, .width = 1280, .height = 720};
    Screen first = {.app = &app, .snapshot_valid = true, .mode = VIEW_BOT, .width = 3440, .height = 1440, .next = &second};
    strcpy(first.name, "WIDE"); strcpy(second.name, "SECOND"); app.screens = &first;
    update_views(&app);
    CHECK(first.reserved[0] == 12 && first.reserved[1] == 30 && first.reserved[2] == 18 && first.reserved[3] == 5);
    CHECK(second.reserved[1] == 60 && first.redraw && second.redraw);
    CHECK(first.present_stride == 4 && second.present_stride == 1);
    CHECK(first.width == 3440 && first.height == 1440 && second.width == 1280 && second.height == 720);
    first.callback = (struct wl_callback *)wl_proxy_create((struct wl_proxy *)display, &wl_callback_interface);
    CHECK(first.callback);
    first.redraw = second.redraw = false; first.ready = false; update_views(&app);
    CHECK(!first.reserved[0] && !first.reserved[1] && !first.reserved[2] && !first.reserved[3] && first.redraw);
    CHECK(second.reserved[1] == 60 && !second.redraw);
    CHECK(first.present_stride == 1 && first.ready && !first.callback && first.present_urgent && second.present_stride == 1);
    update_views(&app);
    CHECK(first.present_stride == 1 && first.blur_strength == 10 && !first.reserved[1]);
    CHECK(second.present_stride == 1 && second.reserved[1] == 60);
    int status; CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    unlink(path); rmdir(directory); wl_display_disconnect(display); close(pair[1]);
    puts("viewer reserved metadata: 8-field cadence keeps insets/per-output rates; old replies restore full cadence, rearm readiness and zero insets without resize");
}

static void test_relative_mouse(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    App app = {.socket_path = "/tmp/doom-viewer-no-controller", .started = now_ms(),
        .input_active = true, .input_fd = pair[0]};
    Screen screen = {.app = &app}; app.human = app.pointer_focus = &screen;
    relative_motion(&app, NULL, 0, 0, 0, 0, wl_fixed_from_double(2.5), wl_fixed_from_double(-1.5));
    relative_motion(&app, NULL, 0, 0, 0, 0, wl_fixed_from_double(.5), wl_fixed_from_double(-.5));
    CHECK(app.mouse_remainder_x == 0 && app.mouse_remainder_y == 0);
    relative_motion(&app, (struct zwp_relative_pointer_v1 *)&screen, 0, 0, 0, 0,
        wl_fixed_from_int(9), wl_fixed_from_int(9));
    Screen other = {.app = &app}; app.pointer_focus = &other;
    relative_motion(&app, NULL, 0, 0, 0, 0, wl_fixed_from_int(8), wl_fixed_from_int(8));
    CHECK(app.input_mouse_x == 3 && app.input_mouse_y == -2);
    app.pointer_focus = &screen;
    app.mouse_fire = true; app.refs[0x9d] = 1;
    app.mouse_remainder_x = .5; app.mouse_remainder_y = -.5; app.cursor_serial = 1;
    pointer_leave(&app, NULL, 0, NULL);
    CHECK(!app.mouse_fire && !app.refs[0x9d] && !app.pointer_focus && !app.cursor_serial);
    CHECK(app.mouse_remainder_x == 0 && app.mouse_remainder_y == 0);
    app.mouse_fire = true; app.refs[0x9d] = 2; app.refs['w'] = 1;
    pointer_unlocked(&app, NULL);
    CHECK(app.human == &screen && !app.mouse_fire && app.refs[0x9d] == 1 && app.refs['w'] == 1);
    key_ref(&app, 0x9d, false); key_ref(&app, 'w', false);
    app.pointer_focus = &screen; app.saver = true;
    relative_motion(&app, NULL, 0, 0, 0, 0, wl_fixed_from_int(7), wl_fixed_from_int(7));
    app.saver = false; app.pointer_focus = NULL;
    relative_motion(&app, NULL, 0, 0, 0, 0, wl_fixed_from_int(9), wl_fixed_from_int(-9));
    input_service(&app);
    char wire[256] = {0}; CHECK(read(pair[1], wire, sizeof(wire) - 1) > 0);
    CHECK(!strcmp(wire, "mouse 3 -2\nkey 157 0 0 0\nkey 157 0 0 0\nkey 119 0 0 0\n"));
    CHECK(app.mouse_remainder_x == 0 && app.mouse_remainder_y == 0);
    input_close(&app); close(pair[1]);
    puts("viewer mouse: ordered coalesced fractions; stale/other-output/saver motion ignored; constraint loss preserves keyboard holds and Ctrl reference");
}

static void test_keyboard_stream(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    App app = {.socket_path = "/tmp/doom-viewer-no-controller", .started = now_ms(),
        .input_active = true, .input_fd = pair[0]};
    Screen screen = {.app = &app}; app.human = app.keyboard_focus = &screen;
    app.xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS); CHECK(app.xkb_context);
    struct xkb_rule_names names = {.rules = "evdev", .model = "pc105", .layout = "us"};
    app.keymap = xkb_keymap_new_from_names(app.xkb_context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS); CHECK(app.keymap);
    app.xkb_state = xkb_state_new(app.keymap); CHECK(app.xkb_state);
    keyboard_repeat(&app, NULL, 30, 400);
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_PRESSED);
    CHECK(app.human == &screen && app.refs[27] == 1);
    keyboard_key(&app, NULL, 0, 0, KEY_ESC, WL_KEYBOARD_KEY_STATE_RELEASED);
    keyboard_key(&app, NULL, 0, 0, KEY_E, WL_KEYBOARD_KEY_STATE_PRESSED);
    keyboard_key(&app, NULL, 0, 0, KEY_E, WL_KEYBOARD_KEY_STATE_RELEASED);
    xkb_mod_index_t shift = xkb_keymap_mod_get_index(app.keymap, XKB_MOD_NAME_SHIFT);
    keyboard_modifiers(&app, NULL, 0, 1u << shift, 0, 0, 0);
    keyboard_key(&app, NULL, 0, 0, KEY_1, WL_KEYBOARD_KEY_STATE_PRESSED);
    keyboard_modifiers(&app, NULL, 0, 0, 0, 0, 0);
    keyboard_key(&app, NULL, 0, 0, KEY_1, WL_KEYBOARD_KEY_STATE_RELEASED);
    keyboard_key(&app, NULL, 0, 0, KEY_GRAVE, WL_KEYBOARD_KEY_STATE_PRESSED);
    keyboard_key(&app, NULL, 0, 0, KEY_GRAVE, WL_KEYBOARD_KEY_STATE_RELEASED);
    xkb_mod_index_t ctrl = xkb_keymap_mod_get_index(app.keymap, XKB_MOD_NAME_CTRL);
    keyboard_modifiers(&app, NULL, 0, 1u << ctrl, 0, 0, 0);
    keyboard_key(&app, NULL, 0, 0, KEY_A, WL_KEYBOARD_KEY_STATE_PRESSED);
    keyboard_key(&app, NULL, 0, 0, KEY_A, WL_KEYBOARD_KEY_STATE_RELEASED);
    keyboard_modifiers(&app, NULL, 0, 0, 0, 0, 0);
    keyboard_key(&app, NULL, 0, 0, KEY_BACKSPACE, WL_KEYBOARD_KEY_STATE_PRESSED);
    app.repeat_at = now_ms() - 1; repeat_keys(&app);
    keyboard_key(&app, NULL, 0, 0, KEY_BACKSPACE, WL_KEYBOARD_KEY_STATE_RELEASED);
    CHECK(!app.repeat_at);
    input_service(&app);
    char wire[2048] = {0}; CHECK(read(pair[1], wire, sizeof(wire) - 1) > 0);
    CHECK(!strcmp(wire, "key 27 1 0 0\nkey 27 0 0 0\nkey 101 1 101 0\nkey 101 0 0 0\n"
        "key 49 1 33 0\nkey 49 0 0 0\nkey 96 1 0 0\nkey 96 0 0 0\nkey 97 1 0 0\nkey 97 0 0 0\n"
        "key 127 1 0 0\nkey 127 1 0 1\nkey 127 0 0 0\n"));
    /* F12 is the sole viewer escape hatch; Esc above stayed native. */
    keyboard_key(&app, NULL, 0, 0, KEY_F12, WL_KEYBOARD_KEY_STATE_PRESSED);
    CHECK(!app.human && !app.input_active);
    /* Overflow is reentrant: it returns bot from inside the down/repeat
     * callback. Neither caller may resurrect the cleared repeat deadline. */
    app.human = &screen; app.screens = &screen; app.input_failed = false;
    app.input_used = sizeof(app.input_queue) - 1;
    keyboard_key(&app, NULL, 0, 0, KEY_W, WL_KEYBOARD_KEY_STATE_PRESSED);
    CHECK(!app.human && !app.repeat_at && !app.refs['w']);
    app.next_query = now_ms() + 200; CHECK(idle_timeout(&app) > 0);
    app.human = &screen; app.input_failed = false; app.input_used = sizeof(app.input_queue) - 1;
    app.keys[KEY_W] = 'w'; app.refs['w'] = 1; app.repeat_key = KEY_W; app.repeat_at = now_ms() - 1;
    repeat_keys(&app);
    CHECK(!app.human && !app.repeat_at && !app.refs['w']);
    app.next_query = now_ms() + 200; CHECK(idle_timeout(&app) > 0);
    xkb_state_unref(app.xkb_state); xkb_keymap_unref(app.keymap); xkb_context_unref(app.xkb_context);
    close(pair[1]);
    puts("viewer keyboard: native Esc/E, unshifted identity with translated punctuation, suppressed Ctrl/toggle text, repeat and exact keyups, F12 bot");
}

static void test_input_lease(void) {
    char directory[] = "/tmp/doom-viewer-stream-XXXXXX"; CHECK(mkdtemp(directory));
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)]; snprintf(path, sizeof(path), "%s/input.sock", directory);
    int server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0); CHECK(server >= 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX}; strcpy(address.sun_path, path);
    if (bind(server, (struct sockaddr *)&address, sizeof(address))) {
        fprintf(stderr, "viewer stream fixture bind failed: %s; run scripts/test-viewer.sh outside the socket-restricted sandbox.\n", strerror(errno));
        close(server); rmdir(directory); exit(1);
    }
    CHECK(!listen(server, 4));
    App app = {.socket_path = path, .started = now_ms()};
    Screen screen = {.app = &app}; snprintf(screen.name, sizeof(screen.name), "STREAM-1"); app.human = &screen;
    /* Events captured before connection still follow the ownership handshake. */
    key_send_text(&app, 'w', true, 'w', false); key_send(&app, 'w', false); input_service(&app);
    CHECK(app.input_active && !app.input_connecting);
    int client = accept(server, NULL, NULL); CHECK(client >= 0);
    char wire[256] = {0}; CHECK(read(client, wire, sizeof(wire) - 1) > 0);
    CHECK(!strcmp(wire, "input STREAM-1\nkey 119 1 119 0\nkey 119 0 0 0\n"));
    CHECK(send(client, "O", 1, MSG_NOSIGNAL) == 1); input_service(&app); CHECK(app.input_reply_used == 1);
    const char *replies = "K lease42\nOK\nOK\n";
    CHECK(send(client, replies, strlen(replies), MSG_NOSIGNAL) == (ssize_t)strlen(replies));
    input_service(&app); CHECK(!app.input_reply_used && app.human == &screen);
    key_ref(&app, 'w', true); input_service(&app);
    memset(wire, 0, sizeof(wire)); CHECK(read(client, wire, sizeof(wire) - 1) > 0);
    CHECK(!strcmp(wire, "key 119 1 0 0\n"));
    const char *rejected = "ERR revoked lease\n";
    CHECK(send(client, rejected, strlen(rejected), MSG_NOSIGNAL) == (ssize_t)strlen(rejected));
    input_service(&app); CHECK(!app.human && !app.refs['w'] && !app.input_active && app.input_failed);
    close(client); close(server); unlink(path); rmdir(directory);
    puts("viewer stream: real Unix connection handshakes before ordered input, drains split/tagged acknowledgments, rejection clears held keys and closes lease");
}

static void test_input_backpressure(void) {
    int pair[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    int bytes = 1024; CHECK(!setsockopt(pair[0], SOL_SOCKET, SO_SNDBUF, &bytes, sizeof(bytes)));
    App app = {.socket_path = "/tmp/doom-viewer-no-controller", .started = now_ms(),
        .input_active = true, .input_fd = pair[0]};
    Screen screen = {.app = &app}; app.human = &screen;
    for (int i = 0; i < 1500; ++i) key_send(&app, 'w', i % 2 == 0);
    uint64_t before = now_ms(); input_service(&app);
    CHECK(now_ms() - before < 50 && app.input_active && app.input_used > app.input_sent);
    /* A lost connection clears local held state, does not retry/replay text,
     * and closes its lease; the controller owns native release on EOF. */
    app.refs['w'] = 1; close(pair[1]); input_service(&app);
    CHECK(!app.human && !app.refs['w'] && !app.input_active && !app.input_used && app.input_failed);
    CHECK(!socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair));
    app.input_fd = pair[0]; app.input_active = true; app.input_failed = false; app.human = &screen;
    app.refs['w'] = 1;
    for (int i = 0; i < 5000 && app.human; ++i) key_send(&app, 'w', i % 2 == 0);
    CHECK(!app.human && !app.refs['w'] && !app.input_active && app.input_failed);
    close(pair[1]);
    puts("viewer transport: stalled reader never blocks callbacks; disconnect/queue overflow revoke takeover and clear held state without replay");
}

int main(void) {
    test_busy_presentation_stride();
    test_duplicate_presentation();
    test_output_resize();
    test_wallpaper_visibility();
    test_layer_saver();
    test_policy_outage();
    test_reserved_policy();
    test_relative_mouse();
    test_keyboard_stream();
    test_input_backpressure();
    test_input_lease();
    DDHeader *shared = mmap(NULL, DD_FILE_SIZE, PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(shared != MAP_FAILED);
    App app = {.shared = shared, .scratch = malloc(DD_FRAME_BYTES)};
    Screen screen = {.app = &app, .snapshot = malloc(DD_FRAME_BYTES)};
    CHECK(app.scratch && screen.snapshot);
    shared->magic = DD_MAGIC; shared->version = DD_VERSION;
    shared->pid = 111;
    shared->width = 320; shared->height = 200; shared->stride = 320 * 4;
    uint32_t *pixels = (uint32_t *)((uint8_t *)shared + DD_HEADER_SIZE);
    for (unsigned i = 0; i < 320 * 200; ++i) pixels[i] = 0x00555555;
    shared->frame = 1;
    CHECK(snapshot(&screen));
    CHECK(screen.snapshot_valid && screen.header.frame == 1);
    CHECK(!snapshot(&screen));
    Screen frozen = {.app = &app, .snapshot = malloc(DD_FRAME_BYTES)};
    CHECK(frozen.snapshot && snapshot(&frozen));
    CHECK(frozen.snapshot[0] == 0x00555555 && frozen.header.pid == 111);
    /* A partial unpublished frame must not mutate the frozen last frame. */
    __atomic_store_n(&shared->seq, 1, __ATOMIC_RELEASE);
    pixels[0] = 0x00000000; shared->frame = 2;
    CHECK(!snapshot(&screen));
    CHECK(screen.snapshot[0] == 0x00555555 && screen.header.frame == 1);
    __atomic_store_n(&shared->seq, 2, __ATOMIC_RELEASE);
    shared->stride = DD_FRAME_BYTES;
    CHECK(!snapshot(&screen));
    CHECK(screen.snapshot[0] == 0x00555555 && screen.header.frame == 1);
    shared->stride = 320 * 4;

    /* A restarted engine may publish the same frame number with new pixels. */
    __atomic_store_n(&shared->seq, 3, __ATOMIC_RELEASE);
    shared->pid = 222; shared->frame = 1;
    for (unsigned i = 0; i < 320 * 200; ++i) pixels[i] = 0x00778899;
    __atomic_store_n(&shared->seq, 4, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen));
    CHECK(screen.header.pid == 222 && screen.header.frame == 1 && screen.snapshot[0] == 0x00778899);
    CHECK(frozen.header.pid == 111 && frozen.snapshot[0] == 0x00555555);

    pid_t writer = fork(); CHECK(writer >= 0);
    if (writer == 0) {
        for (uint32_t frame = 3; frame < 900; ++frame) {
            __atomic_store_n(&shared->seq, frame * 2 + 1, __ATOMIC_RELEASE);
            uint32_t color = 0x00010000 | frame;
            for (unsigned i = 0; i < 320 * 200; ++i) pixels[i] = color;
            shared->frame = frame;
            __atomic_store_n(&shared->seq, frame * 2 + 2, __ATOMIC_RELEASE);
            usleep(200);
        }
        _exit(0);
    }
    unsigned accepted = 0;
    uint64_t deadline = now_ms() + 5000;
    while (now_ms() < deadline) {
        if (snapshot(&screen) && screen.header.frame >= 3) {
            uint32_t expected = 0x00010000 | (uint32_t)screen.header.frame;
            for (unsigned i = 0; i < 320 * 200; ++i) CHECK(screen.snapshot[i] == expected);
            ++accepted;
        }
        int status;
        pid_t result = waitpid(writer, &status, WNOHANG);
        if (result == writer) { CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0); writer = 0; break; }
        usleep(100);
    }
    if (writer) { kill(writer, SIGTERM); waitpid(writer, NULL, 0); CHECK(!"publisher test timed out"); }
    CHECK(accepted > 20);
    printf("viewer snapshot: %u concurrent frames validated; frozen frames preserved\n", accepted);
    CHECK(frozen.header.pid == 111 && frozen.header.frame == 1 && frozen.snapshot[0] == 0x00555555);
    CHECK(snapshot(&frozen));
    CHECK(frozen.header.pid == 222 && frozen.header.frame == shared->frame);
    CHECK(frozen.snapshot[0] == (0x00010000 | (uint32_t)shared->frame));
    puts("viewer snapshots: equal-frame engine restart refreshes; frozen secondary resumes complete shared frame");
    /* Verify the complete v2 capacity rather than only small legacy frames. */
    __atomic_store_n(&shared->seq, 2003, __ATOMIC_RELEASE);
    shared->frame = 1000; shared->width = DD_MAX_WIDTH; shared->height = DD_MAX_HEIGHT; shared->stride = DD_MAX_WIDTH * 4;
    for (size_t i = 0; i < (size_t)DD_MAX_WIDTH * DD_MAX_HEIGHT; ++i) pixels[i] = 0x00123456;
    __atomic_store_n(&shared->seq, 2004, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen));
    CHECK(screen.header.width == DD_MAX_WIDTH && screen.header.height == DD_MAX_HEIGHT);
    CHECK(screen.snapshot[(size_t)DD_MAX_WIDTH * DD_MAX_HEIGHT - 1] == 0x00123456);
    puts("viewer transport: complete 3840x2160 v2 frame copied successfully");
    __atomic_store_n(&shared->seq, 2005, __ATOMIC_RELEASE);
    shared->width = 640; shared->height = 400; shared->stride = 640 * 4;
    shared->pixel_aspect_num = 5; shared->pixel_aspect_den = 6;
    for (unsigned i = 0; i < 640 * 400; ++i) pixels[i] = 0x00abcdef;
    __atomic_store_n(&shared->seq, 2006, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen));
    CHECK(screen.header.frame == 1000 && screen.header.width == 640 && screen.snapshot[0] == 0x00abcdef);
    __atomic_store_n(&shared->seq, 2007, __ATOMIC_RELEASE);
    shared->pixel_aspect_num = shared->pixel_aspect_den = 1;
    __atomic_store_n(&shared->seq, 2008, __ATOMIC_RELEASE);
    CHECK(snapshot(&screen));
    CHECK(screen.header.pixel_aspect_num == 1 && screen.header.pixel_aspect_den == 1);
    puts("viewer transport: unchanged frame counter still refreshes changed geometry/pixel aspect");
    free(frozen.snapshot); free(screen.snapshot); free(app.scratch); munmap(shared, DD_FILE_SIZE);
    return 0;
}
