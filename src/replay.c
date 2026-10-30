/*
 * Hatari - replay.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * replay.c - record one run's scripted IKBD keys and a per-VBL trace of PC
 * and registers, then replay those keys and compare the trace.
 *
 * This is not a debugger stop hook. The sample is taken from the VBL handler,
 * once per frame, with the per-instruction debugger left off. Turning that
 * debugger on (SPCFLAG_DEBUGGER) changes when the CPU is checked and is
 * recorded so a replay can refuse it. The HRDB listen socket this fork opens
 * at boot does not set that flag and does not stop the CPU.
 *
 * Keys are applied after the sample for that frame, so frame N in the trace
 * does not yet show the key scheduled at frame N. Hatari bus-errors a
 * user-mode IO read, and fake TOS enters the program with SR $0300, so the
 * one "move #$0300,sr" in that image is rewritten to SR $2300 for this run.
 * Host key, mouse, and
 * joystick events are ignored for the run: they are not on the guest clock.
 * Hatari_rand is reseeded from a constant so the time() seed in main() is
 * not an input. Real TOS, a cartridge, VDI, a floppy, and GEMDOS host
 * timestamps are refused. Those are outside this gate.
 *
 * Format 1 ("HPISTREPLAY 1") is text, one fact per line:
 *   build <sha256 of this executable>
 *   debugger yes|no
 *   seed <decimal>
 *   machine, mem_kb, ttram_kb, cpu_*, fpu, mmu, blitter
 *   video_timing, monitor, borders, frameskips, sound, fast_forward
 *   tos none, vdi no, cartridge no, floppy no
 *   gemdos_hd, gemdos_boot, gemdos_hosttime, gemdos_drive
 *   program <sha256 of the PRG>
 *   frames <count>
 *   input <frame> <scancode hex> <down|up>
 *   trace <frame> pc <8 hex> sr <4 hex> d0..d7 a0..a7 <8 hex each>
 * A replay whose build line does not match this executable is refused.
 */
const char Replay_fileid[] = "Hatari replay.c";

#include "config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "main.h"
#include "configuration.h"
#include "ikbd.h"
#include "lilo.h"
#include "m68000.h"
#include "replay.h"
#include "stMemory.h"
#include "tos.h"
#include "utils.h"
#include "vdi.h"

#define REPLAY_MAGIC   "HPISTREPLAY 1"
#define REPLAY_SEED    1u
#define MAX_FRAMES     256
#define MAX_KEYS       64
#define TRACE_LEN      320
#define LINE_LEN       512

#if defined(__GNUC__)
#define REPLAY_NORETURN __attribute__((noreturn))
#else
#define REPLAY_NORETURN
#endif

enum {
	MODE_OFF = 0,
	MODE_RECORD,
	MODE_PLAY
};

enum {
	ID_BUILD = 0,
	ID_DEBUGGER,
	ID_SEED,
	ID_MACHINE,
	ID_MEM_KB,
	ID_TTRAM_KB,
	ID_CPU_LEVEL,
	ID_CPU_CLOCK,
	ID_COMPATIBLE,
	ID_CYCLE_EXACT,
	ID_DATA_CACHE,
	ID_ADDR24,
	ID_FPU,
	ID_MMU,
	ID_BLITTER,
	ID_VIDEO_TIMING,
	ID_MONITOR,
	ID_BORDERS,
	ID_FRAMESKIPS,
	ID_SOUND,
	ID_FAST_FORWARD,
	ID_TOS,
	ID_VDI,
	ID_CARTRIDGE,
	ID_FLOPPY,
	ID_GEMDOS_HD,
	ID_GEMDOS_BOOT,
	ID_GEMDOS_HOSTTIME,
	ID_GEMDOS_DRIVE,
	ID_PROGRAM,
	ID_FRAMES,
	ID_COUNT
};

struct replay_key {
	int frame;
	int code;          /* 1..127, break is "up" rather than the high bit */
	bool down;
};

static int mode;
static bool armed;
static bool debugger_attached;
static int run_frames;
static int trace_count;
static int key_count;
static char record_path[FILENAME_MAX];
static char play_path[FILENAME_MAX];
static char build_hex[65];
static char program_hex[65];
static char reject_token[32];
static struct replay_key keys[MAX_KEYS];
static char trace_line[MAX_FRAMES][TRACE_LEN];

/* ----------------------------------------------------------------------- */
/* SHA-256 of the running executable. A different build must not replay.  */

struct sha256_ctx {
	uint32_t h[8];
	uint8_t block[64];
	size_t n;
	uint64_t total;
};

static uint32_t rotr32(uint32_t x, int n)
{
	return (x >> n) | (x << (32 - n));
}

static uint32_t be32(const uint8_t *p)
{
	return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
	       ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void sha256_block(struct sha256_ctx *ctx, const uint8_t block[64])
{
	static const uint32_t k[64] = {
		0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
		0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
		0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
		0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
		0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
		0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
		0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
		0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
	};
	uint32_t w[64];
	uint32_t a, b, c, d, e, f, g, h;
	int i;

	for (i = 0; i < 16; i++)
		w[i] = be32(block + (size_t)i * 4);
	for (i = 16; i < 64; i++) {
		uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
		uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}

	a = ctx->h[0];
	b = ctx->h[1];
	c = ctx->h[2];
	d = ctx->h[3];
	e = ctx->h[4];
	f = ctx->h[5];
	g = ctx->h[6];
	h = ctx->h[7];
	for (i = 0; i < 64; i++) {
		uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
		uint32_t ch = (e & f) ^ ((~e) & g);
		uint32_t t1 = h + s1 + ch + k[i] + w[i];
		uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
		uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
		uint32_t t2 = s0 + maj;

		h = g;
		g = f;
		f = e;
		e = d + t1;
		d = c;
		c = b;
		b = a;
		a = t1 + t2;
	}
	ctx->h[0] += a;
	ctx->h[1] += b;
	ctx->h[2] += c;
	ctx->h[3] += d;
	ctx->h[4] += e;
	ctx->h[5] += f;
	ctx->h[6] += g;
	ctx->h[7] += h;
}

static void sha256_init(struct sha256_ctx *ctx)
{
	ctx->h[0] = 0x6a09e667;
	ctx->h[1] = 0xbb67ae85;
	ctx->h[2] = 0x3c6ef372;
	ctx->h[3] = 0xa54ff53a;
	ctx->h[4] = 0x510e527f;
	ctx->h[5] = 0x9b05688c;
	ctx->h[6] = 0x1f83d9ab;
	ctx->h[7] = 0x5be0cd19;
	ctx->n = 0;
	ctx->total = 0;
}

static void sha256_update(struct sha256_ctx *ctx, const void *data, size_t len)
{
	const uint8_t *p = data;

	ctx->total += len;
	while (len > 0) {
		size_t take = 64 - ctx->n;

		if (take > len)
			take = len;
		memcpy(ctx->block + ctx->n, p, take);
		ctx->n += take;
		p += take;
		len -= take;
		if (ctx->n == 64) {
			sha256_block(ctx, ctx->block);
			ctx->n = 0;
		}
	}
}

static void sha256_final(struct sha256_ctx *ctx, uint8_t dig[32])
{
	uint64_t bits = ctx->total * 8;
	int i;

	ctx->block[ctx->n++] = 0x80;
	if (ctx->n > 56) {
		while (ctx->n < 64)
			ctx->block[ctx->n++] = 0;
		sha256_block(ctx, ctx->block);
		ctx->n = 0;
	}
	while (ctx->n < 56)
		ctx->block[ctx->n++] = 0;
	for (i = 7; i >= 0; i--)
		ctx->block[ctx->n++] = (uint8_t)(bits >> (i * 8));
	sha256_block(ctx, ctx->block);
	for (i = 0; i < 8; i++) {
		dig[i * 4] = (uint8_t)(ctx->h[i] >> 24);
		dig[i * 4 + 1] = (uint8_t)(ctx->h[i] >> 16);
		dig[i * 4 + 2] = (uint8_t)(ctx->h[i] >> 8);
		dig[i * 4 + 3] = (uint8_t)ctx->h[i];
	}
}

static void sha256_buf(const void *data, size_t len, uint8_t dig[32])
{
	struct sha256_ctx ctx;

	sha256_init(&ctx);
	if (len)
		sha256_update(&ctx, data, len);
	sha256_final(&ctx, dig);
}

static void hex_encode(const uint8_t dig[32], char *hex)
{
	static const char *digits = "0123456789abcdef";
	int i;

	for (i = 0; i < 32; i++) {
		hex[i * 2] = digits[dig[i] >> 4];
		hex[i * 2 + 1] = digits[dig[i] & 0x0f];
	}
	hex[64] = '\0';
}

static bool sha256_self_check(void)
{
	static const uint8_t abc[3] = { 'a', 'b', 'c' };
	uint8_t dig[32];
	char hex[65];

	sha256_buf(abc, 3, dig);
	hex_encode(dig, hex);
	if (strcmp(hex, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") != 0)
		return false;
	sha256_buf(NULL, 0, dig);
	hex_encode(dig, hex);
	return strcmp(hex, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0;
}

static bool sha256_file(const char *path, char *hex)
{
	FILE *fp;
	struct sha256_ctx ctx;
	uint8_t buf[4096];
	uint8_t dig[32];
	size_t n;

	fp = fopen(path, "rb");
	if (!fp)
		return false;
	sha256_init(&ctx);
	while ((n = fread(buf, 1, sizeof(buf), fp)) > 0)
		sha256_update(&ctx, buf, n);
	if (ferror(fp)) {
		fclose(fp);
		return false;
	}
	fclose(fp);
	sha256_final(&ctx, dig);
	hex_encode(dig, hex);
	return true;
}

static bool hash_executable(char *hex)
{
#if defined(__linux__)
	return sha256_file("/proc/self/exe", hex);
#else
	(void)hex;
	return false;
#endif
}

/* ----------------------------------------------------------------------- */

static const char *yes_no(bool v)
{
	return v ? "yes" : "no";
}

static const char *machine_name(void)
{
	switch (ConfigureParams.System.nMachineType) {
	case MACHINE_ST: return "st";
	case MACHINE_MEGA_ST: return "megast";
	case MACHINE_STE: return "ste";
	case MACHINE_MEGA_STE: return "megaste";
	case MACHINE_TT: return "tt";
	case MACHINE_FALCON: return "falcon";
	default: return NULL;
	}
}

static const char *fpu_name(void)
{
	switch (ConfigureParams.System.n_FPUType) {
	case FPU_NONE: return "none";
	case FPU_68881: return "68881";
	case FPU_68882: return "68882";
	case FPU_CPU: return "internal";
	default: return NULL;
	}
}

static const char *video_name(void)
{
	switch (ConfigureParams.System.VideoTimingMode) {
	case VIDEO_TIMING_MODE_RANDOM: return "random";
	case VIDEO_TIMING_MODE_WS1: return "ws1";
	case VIDEO_TIMING_MODE_WS2: return "ws2";
	case VIDEO_TIMING_MODE_WS3: return "ws3";
	case VIDEO_TIMING_MODE_WS4: return "ws4";
	default: return NULL;
	}
}

static const char *monitor_name(void)
{
	switch (ConfigureParams.Screen.nMonitorType) {
	case MONITOR_TYPE_MONO: return "mono";
	case MONITOR_TYPE_RGB: return "rgb";
	case MONITOR_TYPE_VGA: return "vga";
	case MONITOR_TYPE_TV: return "tv";
	default: return NULL;
	}
}

static bool floppy_present(void)
{
	int i;

	for (i = 0; i < MAX_FLOPPYDRIVES; i++) {
		if (ConfigureParams.DiskImage.szDiskFileName[i][0] != '\0')
			return true;
	}
	return false;
}

static bool vdi_on(void)
{
	return bUseVDIRes || ConfigureParams.Screen.bUseExtVdiResolutions;
}

static bool cartridge_present(void)
{
	return ConfigureParams.Rom.szCartridgeImageFileName[0] != '\0';
}

static bool debugger_now(void)
{
	return (regs.spcflags & SPCFLAG_DEBUGGER) != 0;
}

static void disable_host_joysticks(void);

/* Hatari bus-errors a user-mode read of IO. Fake TOS enters the test
 * program with SR $0300, so this program could not see the ACIA. Rewrite
 * that one instruction to SR $2300: still IPL 3, so the VBL stays enabled,
 * but the program is supervisor. Stock --tos none is unchanged; this runs
 * only after the recorder has accepted the setup. */
static bool enter_supervisor(void)
{
	uint32_t addr, at;
	int found;

	if (TosSize < 4)
		return false;
	found = 0;
	at = 0;
	for (addr = TosAddress; addr + 3 < TosAddress + TosSize; addr += 2) {
		if (STMemory_ReadWord(addr) == 0x46fc &&
		    STMemory_ReadWord(addr + 2) == 0x0300) {
			found++;
			at = addr;
		}
	}
	if (found != 1)
		return false;
	STMemory_WriteWord(at + 2, 0x2300);
	return STMemory_ReadWord(at + 2) == 0x2300;
}

static void identity_format(int index, char *dst, size_t n)
{
	const char *name = NULL;

	switch (index) {
	case ID_BUILD:
		snprintf(dst, n, "build %s", build_hex);
		break;
	case ID_DEBUGGER:
		snprintf(dst, n, "debugger %s", yes_no(debugger_attached));
		break;
	case ID_SEED:
		snprintf(dst, n, "seed %u", REPLAY_SEED);
		break;
	case ID_MACHINE:
		name = machine_name();
		snprintf(dst, n, "machine %s", name ? name : "unknown");
		break;
	case ID_MEM_KB:
		snprintf(dst, n, "mem_kb %d", ConfigureParams.Memory.STRamSize_KB);
		break;
	case ID_TTRAM_KB:
		snprintf(dst, n, "ttram_kb %d", ConfigureParams.Memory.TTRamSize_KB);
		break;
	case ID_CPU_LEVEL:
		snprintf(dst, n, "cpu_level %d", ConfigureParams.System.nCpuLevel);
		break;
	case ID_CPU_CLOCK:
		snprintf(dst, n, "cpu_clock %d", ConfigureParams.System.nCpuFreq);
		break;
	case ID_COMPATIBLE:
		snprintf(dst, n, "compatible %s", yes_no(ConfigureParams.System.bCompatibleCpu));
		break;
	case ID_CYCLE_EXACT:
		snprintf(dst, n, "cycle_exact %s", yes_no(ConfigureParams.System.bCycleExactCpu));
		break;
	case ID_DATA_CACHE:
		snprintf(dst, n, "data_cache %s", yes_no(ConfigureParams.System.bCpuDataCache));
		break;
	case ID_ADDR24:
		snprintf(dst, n, "addr24 %s", yes_no(ConfigureParams.System.bAddressSpace24));
		break;
	case ID_FPU:
		name = fpu_name();
		snprintf(dst, n, "fpu %s", name ? name : "unknown");
		break;
	case ID_MMU:
		snprintf(dst, n, "mmu %s", yes_no(ConfigureParams.System.bMMU));
		break;
	case ID_BLITTER:
		snprintf(dst, n, "blitter %s", yes_no(ConfigureParams.System.bBlitter));
		break;
	case ID_VIDEO_TIMING:
		name = video_name();
		snprintf(dst, n, "video_timing %s", name ? name : "unknown");
		break;
	case ID_MONITOR:
		name = monitor_name();
		snprintf(dst, n, "monitor %s", name ? name : "unknown");
		break;
	case ID_BORDERS:
		snprintf(dst, n, "borders %s", yes_no(ConfigureParams.Screen.bAllowOverscan));
		break;
	case ID_FRAMESKIPS:
		snprintf(dst, n, "frameskips %d", ConfigureParams.Screen.nFrameSkips);
		break;
	case ID_SOUND:
		snprintf(dst, n, "sound %s", yes_no(ConfigureParams.Sound.bEnableSound));
		break;
	case ID_FAST_FORWARD:
		snprintf(dst, n, "fast_forward %s", yes_no(ConfigureParams.System.bFastForward));
		break;
	case ID_TOS:
		snprintf(dst, n, "tos %s", bUseTos ? "image" : "none");
		break;
	case ID_VDI:
		snprintf(dst, n, "vdi %s", yes_no(vdi_on()));
		break;
	case ID_CARTRIDGE:
		snprintf(dst, n, "cartridge %s", yes_no(cartridge_present()));
		break;
	case ID_FLOPPY:
		snprintf(dst, n, "floppy %s", yes_no(floppy_present()));
		break;
	case ID_GEMDOS_HD:
		snprintf(dst, n, "gemdos_hd %s", yes_no(ConfigureParams.HardDisk.bUseHardDiskDirectories));
		break;
	case ID_GEMDOS_BOOT:
		snprintf(dst, n, "gemdos_boot %s", yes_no(ConfigureParams.HardDisk.bBootFromHardDisk));
		break;
	case ID_GEMDOS_HOSTTIME:
		snprintf(dst, n, "gemdos_hosttime %s", yes_no(ConfigureParams.HardDisk.bGemdosHostTime));
		break;
	case ID_GEMDOS_DRIVE:
		snprintf(dst, n, "gemdos_drive %d", ConfigureParams.HardDisk.nGemdosDrive);
		break;
	case ID_PROGRAM:
		snprintf(dst, n, "program %s", program_hex);
		break;
	case ID_FRAMES:
		snprintf(dst, n, "frames %d", run_frames);
		break;
	default:
		snprintf(dst, n, "unknown");
		break;
	}
}

static const char *token_of(const char *line)
{
	size_t i;

	for (i = 0; line[i] != '\0' && line[i] != ' ' && i + 1 < sizeof(reject_token); i++)
		reject_token[i] = line[i];
	reject_token[i] = '\0';
	return reject_token;
}

static void REPLAY_NORETURN reject_before_run(const char *why)
{
	fprintf(stderr, "replay: refused (%s)\n", why);
	printf("replay rejected: %s\n", why);
	fflush(stdout);
	fflush(stderr);
	Main_ErrorExit(NULL, NULL, 1);
	exit(1);
}

static void REPLAY_NORETURN diverge(int frame, const char *expected, const char *observed)
{
	fprintf(stderr, "replay: expected %s\nreplay: observed %s\n", expected, observed);
	printf("replay diverged at frame %d\n", frame);
	fflush(stdout);
	fflush(stderr);
	exit(2);
}

static bool copy_path(char *dst, size_t n, const char *path)
{
	size_t len;

	if (!path || !path[0])
		return false;
	len = strlen(path);
	if (len + 1 > n)
		return false;
	memcpy(dst, path, len + 1);
	return true;
}

const char *Replay_SetRecord(const char *path)
{
	if (mode == MODE_PLAY || play_path[0])
		return "cannot combine --replay and --replay-record";
	if (!copy_path(record_path, sizeof(record_path), path))
		return "recording path missing or too long";
	mode = MODE_RECORD;
	return NULL;
}

const char *Replay_SetPlay(const char *path)
{
	if (mode == MODE_RECORD || record_path[0])
		return "cannot combine --replay and --replay-record";
	if (!copy_path(play_path, sizeof(play_path), path))
		return "recording path missing or too long";
	mode = MODE_PLAY;
	return NULL;
}

const char *Replay_AddKey(const char *spec)
{
	unsigned frame, code;
	char state[8];
	char extra;

	if (mode == MODE_PLAY)
		return "--replay-key is only valid with --replay-record";
	if (key_count >= MAX_KEYS)
		return "too many --replay-key events";
	if (!spec || sscanf(spec, "%u:%x:%7s%c", &frame, &code, state, &extra) != 3)
		return "key must be <frame>:<hex-scancode>:<down|up>";
	if (frame < 1 || frame > 1000000)
		return "key frame must be >= 1";
	if (code < 1 || code > 0x7f)
		return "scancode must be 01..7f (break is 'up', not the high bit)";
	if (strcmp(state, "down") != 0 && strcmp(state, "up") != 0)
		return "key state must be down or up";

	keys[key_count].frame = (int)frame;
	keys[key_count].code = (int)code;
	keys[key_count].down = (strcmp(state, "down") == 0);
	key_count++;
	return NULL;
}

bool Replay_IgnoresHostInput(void)
{
	return mode != MODE_OFF;
}

bool Replay_Startup(void)
{
	if (mode == MODE_OFF) {
		if (key_count != 0) {
			fprintf(stderr, "replay: --replay-key needs --replay-record\n");
			return false;
		}
		return true;
	}
	if (mode == MODE_PLAY && key_count != 0) {
		fprintf(stderr, "replay: --replay-key is only valid with --replay-record\n");
		return false;
	}
	if (!sha256_self_check()) {
		fprintf(stderr, "replay: sha256 self-check failed\n");
		printf("replay rejected: sha256\n");
		fflush(stdout);
		return false;
	}
	/* Drop the time() seed from main(). Anything that still calls
	 * Hatari_rand during this run draws from this constant. */
	Hatari_srand(REPLAY_SEED);
	/* Before reset. A real stick is host state, and the default is to
	 * enable joystick 1 when SDL sees one. */
	disable_host_joysticks();
	return true;
}

static bool read_line(FILE *fp, char *dst, size_t n)
{
	size_t len;

	if (!fgets(dst, (int)n, fp))
		return false;
	if (strchr(dst, '\n') == NULL)
		return false;
	len = strlen(dst);
	while (len > 0 && (dst[len - 1] == '\n' || dst[len - 1] == '\r')) {
		dst[len - 1] = '\0';
		len--;
	}
	return true;
}

static const char *parse_input_line(const char *line)
{
	unsigned frame, code;
	char state[8];
	char rebuilt[64];
	int wrote;

	if (key_count >= MAX_KEYS)
		return "keys";
	if (sscanf(line, "input %u %x %7s", &frame, &code, state) != 3)
		return "format";
	wrote = snprintf(rebuilt, sizeof(rebuilt), "input %u %02x %s", frame, code, state);
	if (wrote < 0 || (size_t)wrote >= sizeof(rebuilt) || strcmp(rebuilt, line) != 0)
		return "format";
	if (frame < 1 || (int)frame > run_frames)
		return "keys";
	if (code < 1 || code > 0x7f)
		return "keys";
	if (strcmp(state, "down") != 0 && strcmp(state, "up") != 0)
		return "format";

	keys[key_count].frame = (int)frame;
	keys[key_count].code = (int)code;
	keys[key_count].down = (strcmp(state, "down") == 0);
	key_count++;
	return NULL;
}

static bool store_trace(const char *line)
{
	size_t len;

	if (strncmp(line, "trace ", 6) != 0)
		return false;
	len = strlen(line);
	if (len + 1 > TRACE_LEN)
		return false;
	memcpy(trace_line[trace_count], line, len + 1);
	trace_count++;
	return true;
}

/* NULL on success. Otherwise a short token for "replay rejected:". */
static const char *load_recording(void)
{
	FILE *fp;
	char line[LINE_LEN];
	char expect[LINE_LEN];
	const char *err;
	int i;
	int saved_keys;

	fp = fopen(play_path, "r");
	if (!fp)
		return "open";

	if (!read_line(fp, line, sizeof(line)) || strcmp(line, REPLAY_MAGIC) != 0) {
		fclose(fp);
		return "format";
	}
	for (i = 0; i < ID_COUNT; i++) {
		identity_format(i, expect, sizeof(expect));
		if (!read_line(fp, line, sizeof(line))) {
			fclose(fp);
			return "format";
		}
		if (strcmp(line, expect) != 0) {
			err = token_of(expect);
			fclose(fp);
			return err;
		}
	}

	/* Identity matched, including the program hash. Keys in the file
	 * replace the (empty) command-line list. */
	saved_keys = key_count;
	key_count = 0;
	trace_count = 0;
	for (;;) {
		if (!read_line(fp, line, sizeof(line))) {
			fclose(fp);
			return "format";
		}
		if (strncmp(line, "trace ", 6) == 0)
			break;
		err = parse_input_line(line);
		if (err) {
			fclose(fp);
			return err;
		}
	}
	if (!store_trace(line)) {
		fclose(fp);
		return "format";
	}
	for (i = 1; i < run_frames; i++) {
		if (!read_line(fp, line, sizeof(line)) || !store_trace(line)) {
			fclose(fp);
			return "format";
		}
	}
	if (read_line(fp, line, sizeof(line))) {
		fclose(fp);
		return "format";
	}
	fclose(fp);
	if (trace_count != run_frames) {
		key_count = saved_keys;
		return "frames";
	}
	/* Playback compares from the start. The loaded lines stay put. */
	trace_count = 0;
	(void)saved_keys;
	return NULL;
}

static const char *write_recording(void)
{
	FILE *fp;
	char line[LINE_LEN];
	int i;

	fp = fopen(record_path, "w");
	if (!fp)
		return "write";
	if (fprintf(fp, "%s\n", REPLAY_MAGIC) < 0) {
		fclose(fp);
		remove(record_path);
		return "write";
	}
	for (i = 0; i < ID_COUNT; i++) {
		identity_format(i, line, sizeof(line));
		if (fprintf(fp, "%s\n", line) < 0) {
			fclose(fp);
			remove(record_path);
			return "write";
		}
	}
	for (i = 0; i < key_count; i++) {
		if (fprintf(fp, "input %d %02x %s\n", keys[i].frame, keys[i].code,
			    keys[i].down ? "down" : "up") < 0) {
			fclose(fp);
			remove(record_path);
			return "write";
		}
	}
	for (i = 0; i < trace_count; i++) {
		if (fprintf(fp, "%s\n", trace_line[i]) < 0) {
			fclose(fp);
			remove(record_path);
			return "write";
		}
	}
	if (fclose(fp) != 0) {
		remove(record_path);
		return "write";
	}
	return NULL;
}

static void format_trace(char *dst, size_t n, int frame)
{
	size_t left = n;
	char *p = dst;
	int k, wrote;

	MakeSR();
	wrote = snprintf(p, left, "trace %d pc %08x sr %04x",
			 frame,
			 (unsigned)(uint32_t)M68000_GetPC(),
			 (unsigned)(regs.sr & 0xffff));
	if (wrote < 0 || (size_t)wrote >= left) {
		dst[0] = '\0';
		return;
	}
	p += wrote;
	left -= (size_t)wrote;
	for (k = 0; k < 8; k++) {
		wrote = snprintf(p, left, " d%d %08x", k, (unsigned)regs.regs[k]);
		if (wrote < 0 || (size_t)wrote >= left) {
			dst[0] = '\0';
			return;
		}
		p += wrote;
		left -= (size_t)wrote;
	}
	for (k = 0; k < 8; k++) {
		wrote = snprintf(p, left, " a%d %08x", k, (unsigned)regs.regs[8 + k]);
		if (wrote < 0 || (size_t)wrote >= left) {
			dst[0] = '\0';
			return;
		}
		p += wrote;
		left -= (size_t)wrote;
	}
}

static void disable_host_joysticks(void)
{
	int i;

	for (i = 0; i < JOYSTICK_COUNT; i++)
		ConfigureParams.Joysticks.Joy[i].nJoystickMode = JOYSTICK_DISABLED;
}

void Replay_Arm(void)
{
	const char *err;
	uint32_t n;
	int i;

	if (mode == MODE_OFF)
		return;

	/* A real stick is host state. Force it off before the CPU runs. */
	disable_host_joysticks();

	if (bUseTos || bUseLilo)
		reject_before_run("tos");
	if (!psTestPrg || !psTestPrg[0])
		reject_before_run("program");
	if (floppy_present())
		reject_before_run("floppy");
	if (vdi_on())
		reject_before_run("vdi");
	if (cartridge_present())
		reject_before_run("cartridge");
	if (ConfigureParams.HardDisk.bGemdosHostTime)
		reject_before_run("gemdos_hosttime");
	if (!machine_name())
		reject_before_run("machine");
	if (!fpu_name())
		reject_before_run("fpu");
	if (!video_name())
		reject_before_run("video_timing");
	if (!monitor_name())
		reject_before_run("monitor");
	if (!enter_supervisor())
		reject_before_run("supervisor");

	n = Main_SetRunVBLs(0);
	if (n < 1 || n > MAX_FRAMES)
		reject_before_run("frames");
	run_frames = (int)n;

	if (mode == MODE_RECORD) {
		for (i = 0; i < key_count; i++) {
			if (keys[i].frame < 1 || keys[i].frame > run_frames)
				reject_before_run("keys");
		}
	}

	if (!hash_executable(build_hex))
		reject_before_run("executable");
	if (!sha256_file(psTestPrg, program_hex))
		reject_before_run("program");

	debugger_attached = debugger_now();
	trace_count = 0;

	if (mode == MODE_PLAY) {
		err = load_recording();
		if (err)
			reject_before_run(err);
	}

	armed = true;
}

static void apply_keys(int frame)
{
	int i;

	for (i = 0; i < key_count; i++) {
		if (keys[i].frame == frame)
			IKBD_PressSTKey((uint8_t)keys[i].code, keys[i].down);
	}
}

static void REPLAY_NORETURN finish_run(void)
{
	const char *err;

	if (trace_count != run_frames) {
		printf("replay rejected: frames\n");
		fflush(stdout);
		exit(1);
	}
	if (mode == MODE_RECORD) {
		err = write_recording();
		if (err) {
			printf("replay rejected: %s\n", err);
			fflush(stdout);
			exit(1);
		}
		printf("replay recorded\n");
		fflush(stdout);
	} else {
		printf("replay matched\n");
		fflush(stdout);
	}
	/* --run-vbls exits immediately after this returns. */
	exit(0);
}

void Replay_OnVbl(int n_vbls, uint32_t vbl_count, uint32_t run_vbls)
{
	char got[TRACE_LEN];

	if (!armed)
		return;

	if (debugger_now() != debugger_attached) {
		fprintf(stderr, "replay: per-instruction debugger flag changed\n");
		printf("replay rejected: debugger\n");
		fflush(stdout);
		fflush(stderr);
		exit(1);
	}

	if (trace_count >= run_frames) {
		printf("replay rejected: frames\n");
		fflush(stdout);
		exit(1);
	}

	format_trace(got, sizeof(got), n_vbls);
	if (got[0] == '\0') {
		printf("replay rejected: trace\n");
		fflush(stdout);
		exit(1);
	}

	if (mode == MODE_RECORD) {
		memcpy(trace_line[trace_count], got, strlen(got) + 1);
	} else if (strcmp(got, trace_line[trace_count]) != 0) {
		diverge(n_vbls, trace_line[trace_count], got);
	}
	trace_count++;

	/* The sample above does not include this frame's keys. */
	apply_keys(n_vbls);

	if (run_vbls && vbl_count >= run_vbls)
		finish_run();
}
