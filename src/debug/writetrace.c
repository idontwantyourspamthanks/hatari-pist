/*
 * Hatari - writetrace.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * writetrace.c - record writes into one inclusive RAM range as they happen.
 *
 * The CPU store and the blitter store are tagged at the writer. Anything
 * else that lands in the range, including a debugger poke, is unknown and
 * has no PC. A missing tag is never filled from regs.pc. The ring keeps
 * the oldest events; further writes increment the drop count and are not
 * stored. Recording does not stop the CPU. This is not a memory snapshot
 * and not a stop hook.
 */
const char WriteTrace_fileid[] = "Hatari writetrace.c";

#include <errno.h>
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
#include "stMemory.h"
#include "writetrace.h"

#define WT_DEFAULT_CAPACITY 256u
#define WT_MAX_CAPACITY     (1u << 20)
#define WT_TAG_MAX          8

enum {
	WT_NONE = 0,
	WT_CPU,
	WT_BLITTER
};

struct wt_event {
	uint32_t seq;
	uint32_t addr;
	uint32_t oldv;
	uint32_t newv;
	uint32_t pc;
	uint8_t origin;
	uint8_t width;
};

struct wt_tag {
	int origin;
	uint32_t pc;
};

bool WriteTrace_Active;

static bool armed;
static bool have_range;
static bool timing;
static bool interval_armed;
static bool in_note;

static uint32_t range_lo;
static uint32_t range_hi;
static uint32_t capacity = WT_DEFAULT_CAPACITY;
static uint32_t next_seq = 1;
static uint64_t dropped;

static struct wt_event *events;
static uint32_t count;
static uint32_t events_cap;

static struct wt_tag tag_stack[WT_TAG_MAX];
static int tag_depth;
static int tag_skip;

static uint64_t interval_start_us;
static uint64_t interval_writes;
static uint64_t cost_off_us;
static uint64_t cost_on_us;
static uint64_t cost_writes;

const char WriteTrace_Description[] =
	"[<start> <end>|capacity <n>|save <file>|off]\n"
	"\n"
	"\tRecord writes into one inclusive RAM range as they happen.\n"
	"\t'<start> <end>' arms the range and clears the ring and the\n"
	"\tdrop count.  'capacity <n>' is how many events are kept\n"
	"\t(the oldest).  A full ring counts the rest as dropped.\n"
	"\t'save <file>' writes the log and returns to this prompt.\n"
	"\t'off' disarms; while disarmed nothing is logged.\n"
	"\n"
	"\tA CPU store is tagged at the writer and keeps that\n"
	"\tinstruction's PC.  A blitter store is tagged at the writer\n"
	"\tand has no PC.  Any other store, including a debugger poke,\n"
	"\tis unknown and has no PC.  A missing origin is not filled\n"
	"\tfrom the stopped PC.  This does not stop the CPU.  It is\n"
	"\tnot a memory-history view and not a stop hook.\n";


static void refresh_active(void)
{
	WriteTrace_Active = armed || timing;
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
	struct wt_event *fresh;
	uint32_t keep;

	if (events && events_cap == capacity)
		return true;

	fresh = calloc((size_t)capacity, sizeof(*fresh));
	if (!fresh)
	{
		fprintf(stderr, "writetrace: could not allocate %u events.\n", capacity);
		return false;
	}
	keep = count;
	if (keep > capacity)
	{
		dropped += (uint64_t)(keep - capacity);
		keep = capacity;
	}
	if (events && keep)
		memcpy(fresh, events, (size_t)keep * sizeof(*fresh));
	free(events);
	events = fresh;
	events_cap = capacity;
	count = keep;
	return true;
}

static void clear_ring(void)
{
	count = 0;
	dropped = 0;
	next_seq = 1;
}

static uint32_t old_value(uint32_t addr, int width)
{
	if (addr >= STRamEnd)
		return 0;
	if (width == 4)
		return STMemory_ReadLong(addr);
	if (width == 2)
		return STMemory_ReadWord(addr);
	return STMemory_ReadByte(addr);
}

static void push_tag(int origin, uint32_t pc)
{
	if (!WriteTrace_Active)
		return;
	if (tag_skip || tag_depth >= WT_TAG_MAX)
	{
		tag_skip++;
		return;
	}
	tag_stack[tag_depth].origin = origin;
	tag_stack[tag_depth].pc = pc;
	tag_depth++;
}

void WriteTrace_PushCpu(uint32_t pc)
{
	push_tag(WT_CPU, mask_addr(pc));
}

void WriteTrace_PushBlitter(void)
{
	push_tag(WT_BLITTER, 0);
}

void WriteTrace_Pop(void)
{
	if (!WriteTrace_Active)
		return;
	if (tag_skip)
	{
		tag_skip--;
		return;
	}
	if (tag_depth > 0)
		tag_depth--;
}

void WriteTrace_ResetTags(void)
{
	tag_depth = 0;
	tag_skip = 0;
}

void WriteTrace_NoteSlow(uint32_t addr, int width, uint32_t value)
{
	struct wt_event *ev;
	int origin;
	uint32_t pc;
	uint32_t mask;

	if (in_note)
		return;
	if (width != 1 && width != 2 && width != 4)
		return;

	addr = mask_addr(addr);
	if (!have_range || addr < range_lo || addr > range_hi)
		return;

	origin = WT_NONE;
	pc = 0;
	if (tag_depth > 0)
		origin = tag_stack[tag_depth - 1].origin;
	/* Only a CPU tag carries a PC. Never substitute regs.pc. */
	if (origin == WT_CPU)
		pc = tag_stack[tag_depth - 1].pc;

	if (timing && origin == WT_CPU)
		interval_writes++;
	if (!armed)
		return;

	mask = width == 1 ? 0xffu : width == 2 ? 0xffffu : 0xffffffffu;
	value &= mask;

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
	ev->oldv = old_value(addr, width) & mask;
	ev->newv = value;
	ev->pc = pc;
	ev->origin = (uint8_t)origin;
	ev->width = (uint8_t)width;
	in_note = false;
}

void WriteTrace_DebugEnter(void)
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
		cost_writes = interval_writes;
	}
	else
		cost_off_us = us;
}

void WriteTrace_DebugLeave(void)
{
	if (!have_range)
		return;
	interval_writes = 0;
	interval_armed = armed;
	interval_start_us = host_us();
	timing = true;
	refresh_active();
}

static int digits_for(int width)
{
	return width * 2;
}

static bool write_trace_log(FILE *fp)
{
	uint32_t i;
	const char *name;

	if (fprintf(fp, "HPISTWRITE 1\n") < 0)
		return false;
	if (fprintf(fp, "range %08x %08x\n", range_lo, range_hi) < 0)
		return false;
	if (fprintf(fp, "capacity %" PRIu32 "\n", capacity) < 0)
		return false;
	if (fprintf(fp, "dropped %" PRIu64 "\n", dropped) < 0)
		return false;

	for (i = 0; i < count; i++)
	{
		struct wt_event *ev = &events[i];
		int digits = digits_for(ev->width);

		if (ev->origin == WT_CPU)
			name = "cpu";
		else if (ev->origin == WT_BLITTER)
			name = "blitter";
		else
			name = "unknown";

		if (ev->origin == WT_CPU)
		{
			if (fprintf(fp,
				    "event %" PRIu32 " cpu %u %08x %0*x %0*x pc %08x\n",
				    ev->seq, ev->width, ev->addr,
				    digits, ev->oldv, digits, ev->newv, ev->pc) < 0)
				return false;
		}
		else if (fprintf(fp,
				 "event %" PRIu32 " %s %u %08x %0*x %0*x\n",
				 ev->seq, name, ev->width, ev->addr,
				 digits, ev->oldv, digits, ev->newv) < 0)
			return false;
	}

	if (fprintf(fp, "cost off %" PRIu64 " on %" PRIu64 " writes %" PRIu64 "\n",
		    cost_off_us, cost_on_us, cost_writes) < 0)
		return false;
	return ferror(fp) == 0;
}

static int cmd_arm(const char *start, const char *end)
{
	uint32_t lo, hi;

	if (!Eval_Number(start, &lo, NUM_TYPE_CPU) ||
	    !Eval_Number(end, &hi, NUM_TYPE_CPU))
	{
		fprintf(stderr, "writetrace: bad range.\n");
		return DEBUGGER_CMDDONE;
	}
	lo = mask_addr(lo);
	hi = mask_addr(hi);
	if (lo > hi)
	{
		fprintf(stderr, "writetrace: start $%x is above end $%x.\n", lo, hi);
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

static int cmd_capacity(const char *text)
{
	uint32_t n, previous;

	if (!Eval_Number(text, &n, NUM_TYPE_NORMAL) || n < 1 || n > WT_MAX_CAPACITY)
	{
		fprintf(stderr, "writetrace: capacity must be 1..%u.\n", WT_MAX_CAPACITY);
		return DEBUGGER_CMDDONE;
	}
	previous = capacity;
	capacity = n;
	if ((events || armed || have_range) && !ensure_ring())
	{
		capacity = previous;
		return DEBUGGER_CMDDONE;
	}
	return DEBUGGER_CMDDONE;
}

static int cmd_save(const char *path)
{
	FILE *fp;
	bool ok;

	fp = fopen(path, "w");
	if (!fp)
	{
		fprintf(stderr, "writetrace: cannot open '%s' for writing.\n", path);
		perror(NULL);
		return DEBUGGER_CMDDONE;
	}
	ok = write_trace_log(fp);
	if (fclose(fp) != 0)
		ok = false;
	if (!ok)
	{
		fprintf(stderr, "writetrace: writing '%s' failed.\n", path);
		perror(NULL);
	}
	return DEBUGGER_CMDDONE;
}

static int cmd_off(void)
{
	armed = false;
	refresh_active();
	return DEBUGGER_CMDDONE;
}

char *WriteTrace_Match(const char *text, int state)
{
	static const char *subs[] = { "capacity", "off", "save" };
	char *ret;

	ret = DebugUI_MatchHelper(subs, ARRAY_SIZE(subs), text, state);
	if (ret)
		return ret;
	return rl_filename_completion_function(text, state);
}

int WriteTrace_Command(int nArgc, char *psArgs[])
{
	if (nArgc < 2)
		return DebugUI_PrintCmdHelp(psArgs[0]);

	if (strcmp(psArgs[1], "off") == 0)
	{
		if (nArgc != 2)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		return cmd_off();
	}
	if (strcmp(psArgs[1], "capacity") == 0)
	{
		if (nArgc != 3)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		return cmd_capacity(psArgs[2]);
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
