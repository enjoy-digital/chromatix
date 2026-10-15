// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Voxel game HAL, ChromatiX SoC (--with-app, --with-voxel): LCDPSRAMFramebuffer, ButtonsCSR,
// PCMAudio, timer0, VoxelRenderer (3D view, software when absent: vr.c).

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include <irq.h>
#include <libbase/uart.h>
#include <generated/csr.h>
#include <generated/mem.h>
#include <generated/soc.h>

#include "hal.h"
#include "layout.h"

#define PCM_DEPTH 512

static volatile struct host_block *host = (volatile struct host_block *)(MAIN_RAM_BASE + LAYOUT_HOST_OFFSET);

static void fb_init(void);
static void vr_init(void);

/* Time: timer0 1kHz interrupt (milliseconds counter), also sampling the interrupted PC (profiler). */
static volatile uint32_t  ms;
static volatile int       prof_enabled;
static volatile uint32_t *prof_hist = (volatile uint32_t *)(MAIN_RAM_BASE + LAYOUT_PROF_OFFSET);

extern char _ftext[];

static void timer_isr(void)
{
	timer0_ev_pending_write(1);
	ms++;
	if (prof_enabled) {
		uint32_t pc, bucket;
		__asm__ volatile ("csrr %0, mepc" : "=r"(pc));
		bucket = (pc - host->prof_base) >> host->prof_shift;
		if (bucket < host->prof_buckets) {
			prof_hist[bucket]++;
			host->prof_samples++;
		}
	}
}

void hal_init(void)
{
	irq_setmask(0);
	irq_setie(1);
	uart_init();
	timer0_en_write(0);
	timer0_load_write(CONFIG_CLOCK_FREQUENCY/1000);
	timer0_reload_write(CONFIG_CLOCK_FREQUENCY/1000);
	timer0_en_write(1);
	timer0_ev_pending_write(1);
	timer0_ev_enable_write(1);
	irq_attach(TIMER0_INTERRUPT, timer_isr);
	irq_setmask(irq_getmask() | (1 << TIMER0_INTERRUPT));
	host->bench_magic = 0;
	host->frames      = 0;
	host->prof_magic  = 0;
	host->status      = 0;
	fb_init();
	vr_init();
}

int hal_poll(void)
{
	return 1;
}

uint32_t hal_ticks_ms(void)
{
	return ms;
}

uint32_t hal_buttons(void)
{
	return demo_buttons_status_read();
}

/* LCD ------------------------------------------------------------------------------------------ */

/* LCDPSRAMFramebuffer: 3 RGB555 frame buffers in the PSRAM, 8-bit lines (+ palette) written
   through 4 line buffers used in order (copied to the back buffer in the PSRAM), front buffer
   changed at vsync. */
#define FB_BUFFERS     3
#define FB_SLOTS       4
#define FB_LINE_WORDS  (HAL_LCD_WIDTH/4)
#define FB_PALETTE     0x400 /* Words. */

static volatile uint32_t *fb_lines = (volatile uint32_t *)FRAMEBUFFER_BASE;
static int                fb_slot;
static int                fb_back = 1;

static volatile uint32_t *fb_line_begin(void)
{
	/* Wait for the line buffer to be copied. */
	while ((framebuffer_status_read() >> CSR_FRAMEBUFFER_STATUS_BUSY_OFFSET) & (1 << fb_slot));
	return &fb_lines[fb_slot*FB_LINE_WORDS];
}

static void fb_line_end(int y, int buffer)
{
	framebuffer_line_write(
		(y       << CSR_FRAMEBUFFER_LINE_LINE_OFFSET)   |
		(buffer  << CSR_FRAMEBUFFER_LINE_BUFFER_OFFSET) |
		(fb_slot << CSR_FRAMEBUFFER_LINE_SLOT_OFFSET));
	fb_slot = (fb_slot + 1) % FB_SLOTS;
}

static void fb_wait_idle(void)
{
	while ((framebuffer_status_read() >> CSR_FRAMEBUFFER_STATUS_BUSY_OFFSET) & ((1 << FB_SLOTS) - 1));
}

static void fb_init(void)
{
	/* All buffers black: palette entry 0 black. */
	fb_lines[FB_PALETTE] = 0;
	for (int b = 0; b < FB_BUFFERS; b++)
		for (int y = 0; y < HAL_LCD_HEIGHT; y++) {
			volatile uint32_t *dst = fb_line_begin();
			for (int x = 0; x < FB_LINE_WORDS; x++)
				dst[x] = 0;
			fb_line_end(y, b);
		}
}

void hal_lcd_palette(const uint16_t *rgb555)
{
	/* Lines copied after this use the new palette. */
	fb_wait_idle();
	for (int i = 0; i < 256; i++)
		fb_lines[FB_PALETTE + i] = rgb555[i];
}

/* HUD lines: kept and written to each back buffer (the 3D view is redrawn every frame). */
static uint8_t hud[HAL_LCD_HEIGHT - VR_HEIGHT][HAL_LCD_WIDTH] __attribute__((aligned(4)));
static uint8_t hud_dirty = (1 << FB_BUFFERS) - 1;

static void fb_line(int y, const uint8_t *pixels)
{
	volatile uint32_t *dst = fb_line_begin();
	const uint32_t    *src = (const uint32_t *)pixels;
	for (int x = 0; x < FB_LINE_WORDS; x++)
		dst[x] = src[x];
	fb_line_end(y, fb_back);
}

void hal_lcd_line(int y, const uint8_t *pixels)
{
	if (y >= VR_HEIGHT) {
		memcpy(hud[y - VR_HEIGHT], pixels, HAL_LCD_WIDTH);
		hud_dirty = (1 << FB_BUFFERS) - 1;
		return;
	}
	fb_line(y, pixels);
}

void hal_lcd_present(void)
{
	int displayed;

	if (hud_dirty & (1 << fb_back)) {
		for (int y = VR_HEIGHT; y < HAL_LCD_HEIGHT; y++)
			fb_line(y, hud[y - VR_HEIGHT]);
		hud_dirty &= ~(1 << fb_back);
	}

	/* Back buffer complete: displayed from the next frame. */
	fb_wait_idle();
	framebuffer_control_write(fb_back << CSR_FRAMEBUFFER_CONTROL_FRONT_OFFSET);
	/* Next back buffer: neither the displayed one nor the requested one (no wait). */
	displayed = (framebuffer_status_read() >> CSR_FRAMEBUFFER_STATUS_FRONT_OFFSET) & 0x3;
	for (int b = 0; b < FB_BUFFERS; b++)
		if (b != fb_back && b != displayed) {
			fb_back = b;
			break;
		}
}

/* Audio ---------------------------------------------------------------------------------------- */

static void (*audio_fill)(void);

static void pcm_isr(void)
{
	audio_fill();
}

void hal_audio_start(void (*fill)(void))
{
	audio_fill = fill;
	irq_attach(PCM_INTERRUPT, pcm_isr);
	pcm_ev_enable_write(1);
	irq_setmask(irq_getmask() | (1 << PCM_INTERRUPT));
}

int hal_audio_free(void)
{
	return PCM_DEPTH - 1 - pcm_level_read();
}

void hal_audio_write(uint32_t sample)
{
	pcm_data_write(sample);
}

/* 3D view -------------------------------------------------------------------------------------- */

/* Shadow copies (software renderer, hardware RAMs are write only). */
static uint32_t vr_world[VR_WORLD_WORDS];
static uint32_t vr_textures[VR_TEXTURE_WORDS];
static uint16_t vr_blocks[VR_BLOCKS];
static int      vr_hw;
static uint32_t vr_cycles;

#ifdef CSR_VOXEL_BASE
/* VoxelRenderer memories (words): world, textures, blocks. */
static volatile uint32_t *vr_mem = (volatile uint32_t *)VOXEL_BASE;
#define VR_MEM_TEXTURES 0x1000
#define VR_MEM_BLOCKS   0x1800
#endif

static void vr_init(void)
{
#ifdef CSR_VOXEL_BASE
	const char *args = hal_args();
	vr_hw = !(args && strstr(args, "sw"));
#endif
}

uint32_t *hal_vr_world(void)
{
	return vr_world;
}

void hal_vr_world_word(unsigned word)
{
#ifdef CSR_VOXEL_BASE
	vr_mem[word] = vr_world[word];
#else
	(void)word;
#endif
}

uint32_t *hal_vr_textures(void)
{
	return vr_textures;
}

uint16_t *hal_vr_blocks(void)
{
	return vr_blocks;
}

void hal_vr_upload(void)
{
#ifdef CSR_VOXEL_BASE
	for (int i = 0; i < VR_TEXTURE_WORDS; i++)
		vr_mem[VR_MEM_TEXTURES + i] = vr_textures[i];
	for (int i = 0; i < VR_BLOCKS; i++)
		vr_mem[VR_MEM_BLOCKS + i] = vr_blocks[i];
#endif
}

#ifdef CSR_VOXEL_BASE
static uint32_t u16(int16_t v)
{
	return (uint16_t)v;
}

_Static_assert(VR_HEIGHT % FB_SLOTS == 0, "renderer lines must keep the line buffers sequence");

static void vr_render_hw(const struct vr_camera *cam)
{
	voxel_rows_write(0 << CSR_VOXEL_ROWS_FIRST_OFFSET | VR_HEIGHT << CSR_VOXEL_ROWS_COUNT_OFFSET);
	voxel_cell_write(cam->cx | cam->cy << 8 | cam->cz << 16);
	voxel_frac_write(cam->fx | cam->fy << 8 | cam->fz << 16);
	voxel_fwd_xy_write(u16(cam->fwd[0]) | u16(cam->fwd[1]) << 16);
	voxel_fwd_z_write(u16(cam->fwd[2]));
	voxel_right_xy_write(u16(cam->right[0]) | u16(cam->right[1]) << 16);
	voxel_right_z_write(u16(cam->right[2]));
	voxel_down_xy_write(u16(cam->down[0]) | u16(cam->down[1]) << 16);
	voxel_down_z_write(u16(cam->down[2]));
	voxel_window_write(cam->ox | cam->oz << 8);
	voxel_t_far_write(cam->t_far);
	voxel_fog_start_write(cam->fog_start);
	voxel_fog_write(cam->fog_shift << CSR_VOXEL_FOG_SHIFT_OFFSET | cam->sky_base << CSR_VOXEL_FOG_SKY_BASE_OFFSET);
	voxel_select_write(
		(uint32_t)cam->sel_en << CSR_VOXEL_SELECT_ENABLE_OFFSET |
		(uint32_t)cam->sel_x  << CSR_VOXEL_SELECT_X_OFFSET |
		(uint32_t)cam->sel_y  << CSR_VOXEL_SELECT_Y_OFFSET |
		(uint32_t)cam->sel_z  << CSR_VOXEL_SELECT_Z_OFFSET);
	voxel_colors_write(
		(uint32_t)cam->sel_index   << CSR_VOXEL_COLORS_SELECT_OFFSET |
		(uint32_t)cam->cross_en    << CSR_VOXEL_COLORS_CROSS_ENABLE_OFFSET |
		(uint32_t)cam->cross_index << CSR_VOXEL_COLORS_CROSS_OFFSET |
		(uint32_t)(cam->sel_shift & 31) << CSR_VOXEL_COLORS_SELECT_SHIFT_OFFSET);
	/* The renderer uses the line buffers: CPU line writes only before/after. */
	/* Line buffers copied in order: the renderer continues the CPU's sequence (VR_HEIGHT lines: a
	   multiple of FB_SLOTS, so the CPU's next slot is unchanged). */
	voxel_control_write(1 << CSR_VOXEL_CONTROL_START_OFFSET | fb_back << CSR_VOXEL_CONTROL_BUFFER_OFFSET |
		fb_slot << CSR_VOXEL_CONTROL_SLOT_OFFSET);
	while (voxel_status_read() & (1 << CSR_VOXEL_STATUS_BUSY_OFFSET));
	vr_cycles = voxel_cycles_read();
}
#endif

void hal_vr_render(const struct vr_camera *cam)
{
	const struct vr_scene scene = {vr_world, vr_textures, vr_blocks};
	uint8_t line[VR_WIDTH] __attribute__((aligned(4)));

#ifdef CSR_VOXEL_BASE
	if (vr_hw) {
		vr_render_hw(cam);
		return;
	}
#endif
	for (int y = 0; y < VR_HEIGHT; y++) {
		vr_line(&scene, cam, y, line);
		fb_line(y, line);
	}
}

int hal_vr_hardware(void)
{
	return vr_hw;
}

uint32_t hal_vr_cycles(void)
{
	return vr_cycles;
}

/* Host interface ------------------------------------------------------------------------------- */

const char *hal_args(void)
{
	if (host->magic != HOST_ARGS_MAGIC)
		return NULL;
	host->args[sizeof(host->args) - 1] = 0;
	return (const char *)host->args;
}

void hal_frame(void)
{
	host->frames++;
	host->ms = ms;
}

void hal_profile(int enable)
{
	if (enable) {
		host->prof_base    = (uint32_t)(uintptr_t)_ftext;
		host->prof_shift   = 5;
		host->prof_buckets = LAYOUT_PROF_SIZE/4;
		host->prof_samples = 0;
		for (unsigned i = 0; i < LAYOUT_PROF_SIZE/4; i++)
			prof_hist[i] = 0;
		host->prof_magic   = HOST_BENCH_MAGIC;
	}
	prof_enabled = enable;
}
