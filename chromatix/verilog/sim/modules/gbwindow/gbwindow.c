//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// LiteX simulation module: Game Boy LCD window (SDL2) and keyboard/gamepad buttons.
//
// Pads:
// - gb_lcd:   clk (hClk), clkena, data[14:0] (RGB555, R in the LSBs), vsync: 160x144 frames shown
//             in a window at each vsync (the title shows the simulation speed).
// - sim_ctrl: keys[7:0] ({right, left, down, up, start, select, b, a}, driven by the module),
//             finish (driven by the module: window closed or Escape).
// - gb_audio: left[15:0], right[15:0] (signed samples, hClk): sampled at 32768Hz (simulation time),
//             recorded to a WAV file and/or played (only sounds right near realtime).
//
// Keys: arrows (D-pad), X (A), Z (B), Enter (Start), Backspace/Right Shift (Select), P (pause),
// F12 (screenshot, BMP), Escape (quit). A connected gamepad is also mapped.
//
// Args (JSON): scale (window scale, default 4), realtime (never run faster than the Game Boy),
// screenshot_frame (screenshot of this frame, tests), wav (audio WAV file), audio (live audio).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <json-c/json.h>
#include <SDL2/SDL.h>

#include "error.h"
#include "modules.h"

#define WIDTH  160
#define HEIGHT 144
#define PIXELS (WIDTH*HEIGHT)

#define GB_FPS (4194304.0/70224.0)

#define AUDIO_RATE    32768 // hClk/512.
#define AUDIO_DIVIDER 512

// Button bits ({right, left, down, up, start, select, b, a}).
#define BTN_A      (1 << 0)
#define BTN_B      (1 << 1)
#define BTN_SELECT (1 << 2)
#define BTN_START  (1 << 3)
#define BTN_UP     (1 << 4)
#define BTN_DOWN   (1 << 5)
#define BTN_LEFT   (1 << 6)
#define BTN_RIGHT  (1 << 7)

struct session_s {
  // Pads.
  char     *clk;
  char     *clkena;
  uint16_t *data;
  char     *vsync;
  char     *keys;
  char     *finish;
  uint16_t *left;
  uint16_t *right;
  // State.
  clk_edge_state_t edge;
  char     vsync_d;
  unsigned n;
  uint32_t fb[PIXELS];
  // Window.
  SDL_Window         *window;
  SDL_Renderer       *renderer;
  SDL_Texture        *texture;
  SDL_GameController *pad;
  int      scale;
  int      realtime;
  int      paused;
  uint8_t  kbd;
  uint8_t  pad_buttons;
  unsigned screenshots;
  int64_t  screenshot_frame;
  // Audio.
  unsigned audio_count;
  FILE    *wav;
  uint32_t wav_samples;
  int      audio;
  SDL_AudioDeviceID audio_dev;
  int16_t  audio_buf[2*1024];
  unsigned audio_n;
  // Statistics.
  uint64_t frames;
  uint64_t start_ms;
  uint64_t title_ms;
  uint64_t title_frames;
  uint64_t title_ps;
  uint64_t time_ps;
};

static int litex_sim_module_pads_get(struct pad_s *pads, char *name, void **signal)
{
  int i = 0;
  *signal = NULL;
  if (!pads || !name)
    return RC_INVARG;
  while (pads[i].name) {
    if (!strcmp(pads[i].name, name)) {
      *signal = pads[i].signal;
      break;
    }
    i++;
  }
  return RC_OK;
}

// Audio --------------------------------------------------------------------------------------------

static void wav_header(struct session_s *s)
{
  uint32_t data_bytes = s->wav_samples*4;
  uint8_t  h[44];
  memcpy(h, "RIFF", 4);
  uint32_t v;
  v = 36 + data_bytes;        memcpy(h +  4, &v, 4);
  memcpy(h + 8, "WAVEfmt ", 8);
  v = 16;                     memcpy(h + 16, &v, 4);
  uint16_t w;
  w = 1;                      memcpy(h + 20, &w, 2); // PCM.
  w = 2;                      memcpy(h + 22, &w, 2); // Stereo.
  v = AUDIO_RATE;             memcpy(h + 24, &v, 4);
  v = AUDIO_RATE*4;           memcpy(h + 28, &v, 4);
  w = 4;                      memcpy(h + 32, &w, 2);
  w = 16;                     memcpy(h + 34, &w, 2);
  memcpy(h + 36, "data", 4);
  v = data_bytes;             memcpy(h + 40, &v, 4);
  long pos = ftell(s->wav);
  fseek(s->wav, 0, SEEK_SET);
  fwrite(h, 1, sizeof(h), s->wav);
  fseek(s->wav, pos ? pos : (long)sizeof(h), SEEK_SET);
  fflush(s->wav);
}

static void audio_sample(struct session_s *s)
{
  int16_t lr[2] = {(int16_t)*s->left, (int16_t)*s->right};
  if (s->wav) {
    fwrite(lr, sizeof(int16_t), 2, s->wav);
    if ((++s->wav_samples % AUDIO_RATE) == 0)
      wav_header(s); // Valid file even if the simulation is interrupted.
  }
  if (s->audio_dev) {
    s->audio_buf[s->audio_n++] = lr[0];
    s->audio_buf[s->audio_n++] = lr[1];
    if (s->audio_n == sizeof(s->audio_buf)/sizeof(s->audio_buf[0])) {
      // Limit the latency (simulation faster than realtime): drop when more than 0.25s queued.
      if (SDL_GetQueuedAudioSize(s->audio_dev) < AUDIO_RATE)
        SDL_QueueAudio(s->audio_dev, s->audio_buf, sizeof(s->audio_buf));
      s->audio_n = 0;
    }
  }
}

static struct session_s *exit_session;

static void gbwindow_exit(void)
{
  struct session_s *s = exit_session;
  if (s && s->wav) {
    wav_header(s);
    fclose(s->wav);
    s->wav = NULL;
    printf("[gbwindow] WAV: %u samples.\n", s->wav_samples);
  }
}

// Window -------------------------------------------------------------------------------------------

static int window_open(struct session_s *s)
{
  if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER) < 0) {
    fprintf(stderr, "[gbwindow] SDL_Init failed: %s\n", SDL_GetError());
    return RC_ERROR;
  }
  s->window = SDL_CreateWindow("ChromatiX sim", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
    WIDTH*s->scale, HEIGHT*s->scale, SDL_WINDOW_SHOWN | SDL_WINDOW_RESIZABLE);
  if (!s->window)
    return RC_ERROR;
  s->renderer = SDL_CreateRenderer(s->window, -1, 0);
  if (!s->renderer)
    return RC_ERROR;
  SDL_RenderSetLogicalSize(s->renderer, WIDTH, HEIGHT);
  s->texture = SDL_CreateTexture(s->renderer, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING,
    WIDTH, HEIGHT);
  if (!s->texture)
    return RC_ERROR;
  for (int i = 0; i < SDL_NumJoysticks(); i++) {
    if (SDL_IsGameController(i)) {
      s->pad = SDL_GameControllerOpen(i);
      if (s->pad) {
        printf("[gbwindow] Gamepad: %s\n", SDL_GameControllerName(s->pad));
        break;
      }
    }
  }
  if (s->audio) {
    SDL_AudioSpec want = {0};
    want.freq     = AUDIO_RATE;
    want.format   = AUDIO_S16SYS;
    want.channels = 2;
    want.samples  = 1024;
    if (SDL_InitSubSystem(SDL_INIT_AUDIO) == 0)
      s->audio_dev = SDL_OpenAudioDevice(NULL, 0, &want, NULL, 0);
    if (s->audio_dev)
      SDL_PauseAudioDevice(s->audio_dev, 0);
    else
      fprintf(stderr, "[gbwindow] No audio: %s\n", SDL_GetError());
  }
  s->start_ms = SDL_GetTicks64();
  s->title_ms = s->start_ms;
  return RC_OK;
}

static void window_present(struct session_s *s)
{
  SDL_UpdateTexture(s->texture, NULL, s->fb, WIDTH*sizeof(uint32_t));
  SDL_RenderClear(s->renderer);
  SDL_RenderCopy(s->renderer, s->texture, NULL, NULL);
  SDL_RenderPresent(s->renderer);
}

static void window_screenshot(struct session_s *s)
{
  char name[64];
  SDL_Surface *surface = SDL_CreateRGBSurfaceWithFormatFrom(s->fb, WIDTH, HEIGHT, 32,
    WIDTH*sizeof(uint32_t), SDL_PIXELFORMAT_ARGB8888);
  snprintf(name, sizeof(name), "screenshot_%04u.bmp", s->screenshots++);
  if (surface && SDL_SaveBMP(surface, name) == 0)
    printf("[gbwindow] %s\n", name);
  SDL_FreeSurface(surface);
}

static uint8_t key_button(SDL_Keycode key)
{
  switch (key) {
    case SDLK_x:         return BTN_A;
    case SDLK_z:         return BTN_B;
    case SDLK_RETURN:    return BTN_START;
    case SDLK_BACKSPACE:
    case SDLK_RSHIFT:    return BTN_SELECT;
    case SDLK_UP:        return BTN_UP;
    case SDLK_DOWN:      return BTN_DOWN;
    case SDLK_LEFT:      return BTN_LEFT;
    case SDLK_RIGHT:     return BTN_RIGHT;
    default:             return 0;
  }
}

static uint8_t pad_button(uint8_t button)
{
  switch (button) {
    case SDL_CONTROLLER_BUTTON_A:          return BTN_A;
    case SDL_CONTROLLER_BUTTON_B:          return BTN_B;
    case SDL_CONTROLLER_BUTTON_X:          return BTN_B;
    case SDL_CONTROLLER_BUTTON_START:      return BTN_START;
    case SDL_CONTROLLER_BUTTON_BACK:       return BTN_SELECT;
    case SDL_CONTROLLER_BUTTON_DPAD_UP:    return BTN_UP;
    case SDL_CONTROLLER_BUTTON_DPAD_DOWN:  return BTN_DOWN;
    case SDL_CONTROLLER_BUTTON_DPAD_LEFT:  return BTN_LEFT;
    case SDL_CONTROLLER_BUTTON_DPAD_RIGHT: return BTN_RIGHT;
    default:                               return 0;
  }
}

// Returns 1 when the simulation must end.
static int window_events(struct session_s *s)
{
  SDL_Event event;
  while (SDL_PollEvent(&event)) {
    switch (event.type) {
      case SDL_QUIT:
        return 1;
      case SDL_KEYDOWN:
        if (event.key.keysym.sym == SDLK_ESCAPE)
          return 1;
        if (event.key.repeat)
          break;
        if (event.key.keysym.sym == SDLK_p)
          s->paused = !s->paused;
        else if (event.key.keysym.sym == SDLK_F12)
          window_screenshot(s);
        s->kbd |= key_button(event.key.keysym.sym);
        break;
      case SDL_KEYUP:
        s->kbd &= ~key_button(event.key.keysym.sym);
        break;
      case SDL_CONTROLLERBUTTONDOWN:
        s->pad_buttons |= pad_button(event.cbutton.button);
        break;
      case SDL_CONTROLLERBUTTONUP:
        s->pad_buttons &= ~pad_button(event.cbutton.button);
        break;
      case SDL_CONTROLLERDEVICEADDED:
        if (!s->pad)
          s->pad = SDL_GameControllerOpen(event.cdevice.which);
        break;
    }
  }
  return 0;
}

static void window_title(struct session_s *s)
{
  uint64_t now = SDL_GetTicks64();
  if (now - s->title_ms < 1000)
    return;
  double seconds   = (now - s->title_ms)/1000.0;
  double fps       = (s->frames - s->title_frames)/seconds;
  double realtime  = ((s->time_ps - s->title_ps)*1e-12)/seconds;
  char   title[128];
  snprintf(title, sizeof(title), "ChromatiX sim - %.1f fps (%.3fx realtime)%s", fps, realtime,
    s->paused ? " - paused" : "");
  SDL_SetWindowTitle(s->window, title);
  s->title_ms     = now;
  s->title_frames = s->frames;
  s->title_ps     = s->time_ps;
}

// Module -------------------------------------------------------------------------------------------

static int gbwindow_start(void *b)
{
  printf("[gbwindow] loaded\n");
  return RC_OK;
}

static int gbwindow_new(void **sess, char *args)
{
  struct session_s *s = (struct session_s *)calloc(1, sizeof(struct session_s));
  if (!s)
    return RC_NOENMEM;
  s->scale = 4;
  s->screenshot_frame = -1;
  if (args) {
    json_object *json = json_tokener_parse(args);
    json_object *value;
    if (json) {
      if (json_object_object_get_ex(json, "scale", &value))
        s->scale = json_object_get_int(value);
      if (json_object_object_get_ex(json, "realtime", &value))
        s->realtime = json_object_get_boolean(value);
      if (json_object_object_get_ex(json, "screenshot_frame", &value))
        s->screenshot_frame = json_object_get_int64(value);
      if (json_object_object_get_ex(json, "audio", &value))
        s->audio = json_object_get_boolean(value);
      if (json_object_object_get_ex(json, "wav", &value) && json_object_get_string_len(value)) {
        s->wav = fopen(json_object_get_string(value), "wb");
        if (s->wav)
          wav_header(s);
      }
      json_object_put(json);
    }
  }
  *sess = (void *)s;
  exit_session = s;
  atexit(gbwindow_exit);
  return window_open(s);
}

static int gbwindow_add_pads(void *sess, struct pad_list_s *plist)
{
  struct session_s *s = (struct session_s *)sess;
  if (!sess || !plist)
    return RC_INVARG;
  if (!strcmp(plist->name, "gb_lcd")) {
    litex_sim_module_pads_get(plist->pads, "clk",    (void **)&s->clk);
    litex_sim_module_pads_get(plist->pads, "clkena", (void **)&s->clkena);
    litex_sim_module_pads_get(plist->pads, "data",   (void **)&s->data);
    litex_sim_module_pads_get(plist->pads, "vsync",  (void **)&s->vsync);
  }
  if (!strcmp(plist->name, "gb_audio")) {
    litex_sim_module_pads_get(plist->pads, "left",  (void **)&s->left);
    litex_sim_module_pads_get(plist->pads, "right", (void **)&s->right);
  }
  if (!strcmp(plist->name, "sim_ctrl")) {
    litex_sim_module_pads_get(plist->pads, "keys",   (void **)&s->keys);
    litex_sim_module_pads_get(plist->pads, "finish", (void **)&s->finish);
  }
  return RC_OK;
}

static int gbwindow_close(void *sess)
{
  struct session_s *s = (struct session_s *)sess;
  gbwindow_exit();
  exit_session = NULL;
  if (s->pad)
    SDL_GameControllerClose(s->pad);
  if (s->window) {
    SDL_DestroyTexture(s->texture);
    SDL_DestroyRenderer(s->renderer);
    SDL_DestroyWindow(s->window);
    SDL_Quit();
  }
  free(s);
  return RC_OK;
}

static void gbwindow_frame(struct session_s *s)
{
  // Incomplete frames (LCD off): black.
  if (s->n != PIXELS)
    memset(s->fb, 0, sizeof(s->fb));
  if ((int64_t)s->frames == s->screenshot_frame)
    window_screenshot(s);
  s->frames++;
  window_present(s);
  if (window_events(s))
    *s->finish = 1;
  // Pause: keep the window alive, simulation stopped.
  while (s->paused && !*s->finish) {
    SDL_Delay(10);
    if (window_events(s))
      *s->finish = 1;
    window_title(s);
  }
  // Realtime: never faster than the Game Boy.
  if (s->realtime) {
    uint64_t target_ms = s->start_ms + (uint64_t)(s->frames*1000.0/GB_FPS);
    uint64_t now       = SDL_GetTicks64();
    if (target_ms > now)
      SDL_Delay(target_ms - now);
  }
  *s->keys = s->kbd | s->pad_buttons;
  window_title(s);
}

static int gbwindow_tick(void *sess, uint64_t time_ps)
{
  struct session_s *s = (struct session_s *)sess;
  if (!s->clk || !clk_pos_edge(&s->edge, *s->clk))
    return RC_OK;
  s->time_ps = time_ps;
  if (s->left && ++s->audio_count == AUDIO_DIVIDER) {
    s->audio_count = 0;
    if (s->wav || s->audio_dev)
      audio_sample(s);
  }
  if (*s->clkena && s->n < PIXELS) {
    uint16_t d = *s->data;
    uint32_t r = d & 0x1f, g = (d >> 5) & 0x1f, b = (d >> 10) & 0x1f;
    s->fb[s->n++] = 0xff000000 | (((r << 3) | (r >> 2)) << 16) | (((g << 3) | (g >> 2)) << 8) |
      ((b << 3) | (b >> 2));
  }
  if (*s->vsync && !s->vsync_d) {
    gbwindow_frame(s);
    s->n = 0;
  }
  s->vsync_d = *s->vsync;
  return RC_OK;
}

static struct ext_module_s ext_mod = {
  "gbwindow",
  gbwindow_start,
  gbwindow_new,
  gbwindow_add_pads,
  gbwindow_close,
  gbwindow_tick
};

int litex_sim_ext_module_init(int (*register_module)(struct ext_module_s *))
{
  return register_module(&ext_mod);
}
