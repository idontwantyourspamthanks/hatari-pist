/*
 * Hatari - readtrace.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * readtrace.c - record CPU data reads in one inclusive RAM range.
 *
 * Instruction fetches are not reads of the program's data. A blitter
 * transfer does not come through the CPU data-read path, so it is not
 * in this log. Recording does not stop the CPU.
 */
const char ReadTrace_fileid[] = "Hatari readtrace.c";

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "config.h"

#if HAVE_LIBREADLINE
# include <readline/readline.h>
#else
# define rl_filename_completion_function(x,y) NULL
#endif

#include "main.h"
#include "configuration.h"
#include "debug_priv.h"
#include "debugui.h"
#include "evaluate.h"
#include "readtrace.h"

#define RT_DEFAULT_CAPACITY 256u

struct rt_event {
	uint32_t seq;
	uint32_t addr;
	uint32_t value;
	uint32_t pc;
	uint8_t width;
};

bool ReadTrace_Active;

static bool armed;
static bool have_range;
static bool timing;
static bool interval_armed;
static bool in_note;

static uint32_t range_lo;
static uint32_t range_hi;
static uint32_t capacity = RT_DEFAULT_CAPACITY;
static uint32_t next_seq = 1;
static uint64_t dropped;

static struct rt_event *events;
static uint32_t count;
static uint32_t events_cap;

static uint64_t interval_start_us;
static uint64_t interval_reads;
static uint64_t cost_off_us;
static uint64_t cost_on_us;
static uint64_t cost_reads;

const char ReadTrace_Description[] =
	"[<start> <end>|save <file>|off]\n"
	"\n"
	"\tRecord CPU data reads in one inclusive RAM range.\n"
	"\t'<start> <end>' arms the range and clears the ring.\n"
	"\t'save <file>' writes the log and returns to this prompt.\n"
	"\t'off' disarms. Instruction fetches are not recorded.\n"
	"\tThis does not stop the CPU.\n";


static void refresh_active(void)
{
	ReadTrace_Active = armed || timing;
}

static uint64_t host_us(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}

static uint32_t mask_addr(uint32_t addr)
{
	if (ConfigureParams.System.bAddressSpace24)
		addr &= 0x00ffffff;
	return addr;
}

static bool ensure_ring(void)
{
	struct rt_event *fresh;

	if (events && events_cap == capacity)
		return true;
	fresh = calloc((size_t)capacity, sizeof(*fresh));
	if (!fresh)
	{
		fprintf(stderr, "readtrace: could not allocate %u events.\n", capacity);
		return false;
	}
	free(events);
	events = fresh;
	events_cap = capacity;
	count = 0;
	return true;
}

static void clear_ring(void)
{
	count = 0;
	dropped = 0;
	next_seq = 1;
}

void ReadTrace_NoteSlow(uint32_t addr, int width, uint32_t value, uint32_t pc)
{
	struct rt_event *ev;
	uint32_t mask;

	if (in_note)
		return;
	if (width != 1 && width != 2 && width != 4)
		return;
	addr = mask_addr(addr);
	if (!have_range || addr < range_lo || addr > range_hi)
		return;
	if (timing)
		interval_reads++;
	if (!armed)
		return;
	mask = width == 1 ? 0xffu : width == 2 ? 0xffffu : 0xffffffffu;
	if (count >= capacity)
	{
		dropped++;
		return;
	}
	if (!events || events_cap != capacity)
		return;
	in_note = true;
	ev = &events[count++];
	ev->seq = next_seq++;
	ev->addr = addr;
	ev->value = value & mask;
	ev->pc = mask_addr(pc);
	ev->width = (uint8_t)width;
	in_note = false;
}

void ReadTrace_DebugEnter(void)
{
	uint64_t us;

	if (!timing)
		return;
	us = host_us() - interval_start_us;
	timing = false;
	refresh_active();
	if (interval_armed)
	{
		cost_on_us = us;
		cost_reads = interval_reads;
	}
	else
		cost_off_us = us;
}

void ReadTrace_DebugLeave(void)
{
	if (!have_range)
		return;
	interval_reads = 0;
	interval_armed = armed;
	interval_start_us = host_us();
	timing = true;
	refresh_active();
}

static bool write_log(FILE *fp)
{
	uint32_t i;

	if (fprintf(fp, "HPISTREAD 1\n") < 0)
		return false;
	if (fprintf(fp, "range %08x %08x\n", range_lo, range_hi) < 0)
		return false;
	if (fprintf(fp, "capacity %" PRIu32 "\n", capacity) < 0)
		return false;
	if (fprintf(fp, "dropped %" PRIu64 "\n", dropped) < 0)
		return false;
	for (i = 0; i < count; i++)
	{
		struct rt_event *ev = &events[i];
		int digits = ev->width * 2;

		if (fprintf(fp,
			    "event %" PRIu32 " cpu %u %08x %0*x pc %08x\n",
			    ev->seq, (unsigned)ev->width, ev->addr, digits,
			    (unsigned)ev->value, ev->pc) < 0)
			return false;
	}
	if (fprintf(fp, "cost off %" PRIu64 " on %" PRIu64 " reads %" PRIu64 "\n",
		    cost_off_us, cost_on_us, cost_reads) < 0)
		return false;
	return ferror(fp) == 0;
}

static int cmd_arm(const char *start, const char *end)
{
	uint32_t lo, hi;

	if (!Eval_Number(start, &lo, NUM_TYPE_CPU) ||
	    !Eval_Number(end, &hi, NUM_TYPE_CPU))
	{
		fprintf(stderr, "readtrace: bad range.\n");
		return DEBUGGER_CMDDONE;
	}
	lo = mask_addr(lo);
	hi = mask_addr(hi);
	if (lo > hi)
	{
		fprintf(stderr, "readtrace: start $%x is above end $%x.\n", lo, hi);
		return DEBUGGER_CMDDONE;
	}
	if (!ensure_ring())
		return DEBUGGER_CMDDONE;
	range_lo = lo;
	range_hi = hi;
	have_range = true;
	armed = true;
	clear_ring();
	refresh_active();
	return DEBUGGER_CMDDONE;
}

static int cmd_off(void)
{
	armed = false;
	refresh_active();
	fprintf(stderr, "readtrace: off\n");
	return DEBUGGER_CMDDONE;
}

static int cmd_save(const char *path)
{
	FILE *fp;

	if (!have_range)
	{
		fprintf(stderr, "readtrace: no range is armed.\n");
		return DEBUGGER_CMDDONE;
	}
	fp = fopen(path, "w");
	if (!fp)
	{
		fprintf(stderr, "readtrace: cannot open '%s' for writing.\n", path);
		return DEBUGGER_CMDDONE;
	}
	if (!write_log(fp))
	{
		fprintf(stderr, "readtrace: writing '%s' failed.\n", path);
		fclose(fp);
		return DEBUGGER_CMDDONE;
	}
	fclose(fp);
	fprintf(stderr, "readtrace: wrote '%s'\n", path);
	return DEBUGGER_CMDDONE;
}

char *ReadTrace_Match(const char *text, int state)
{
	static const char *subs[] = { "off", "save" };
	char *ret;

	ret = DebugUI_MatchHelper(subs, ARRAY_SIZE(subs), text, state);
	if (ret)
		return ret;
	return rl_filename_completion_function(text, state);
}

int ReadTrace_Command(int nArgc, char *psArgs[])
{
	if (nArgc < 2)
		return DebugUI_PrintCmdHelp(psArgs[0]);
	if (strcmp(psArgs[1], "off") == 0)
	{
		if (nArgc != 2)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		return cmd_off();
	}
	if (strcmp(psArgs[1], "save") == 0)
	{
		if (nArgc != 3)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		return cmd_save(psArgs[2]);
	}
	if (nArgc == 3)
		return cmd_arm(psArgs[1], psArgs[2]);
	return DebugUI_PrintCmdHelp(psArgs[0]);
}
