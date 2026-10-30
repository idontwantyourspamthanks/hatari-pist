/*
 * Hatari - paltrace.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * paltrace.c - record each shifter palette write as it happens.
 *
 * The note is taken inside Video_ColorReg_WriteWord, after the bus sync,
 * when the masked color and the even register address are known. The HBL,
 * the cycle on that line, and the cycles since the VBL come from
 * Video_GetPosition_OnWriteAccess at that moment, together with nVBLs and
 * regs.instruction_pc. Those fields are what 'save' writes. Recording does
 * not stop the CPU. This is not a memory snapshot and not a stop hook.
 */
const char PalTrace_fileid[] = "Hatari paltrace.c";

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
#include "m68000.h"
#include "video.h"
#include "paltrace.h"

struct pt_event {
	uint32_t seq;
	uint32_t pc;
	uint32_t reg;
	uint16_t color;
	int vbl;
	int hbl;
	int pos;
	int frame;
};

bool PalTrace_Active;

static struct pt_event *events;
static uint32_t count;
static uint32_t capacity;
static uint32_t next_seq = 1;

const char PalTrace_Description[] =
	"[on|off|save <file>]\n"
	"\n"
	"\tRecord each shifter palette write as it happens.\n"
	"\t'on' clears the log and arms it.  'off' disarms; while\n"
	"\tdisarmed, palette writes are not logged.  'save <file>'\n"
	"\twrites the log and returns to this prompt.\n"
	"\n"
	"\tThe PC, register, masked color, VBL, HBL, and position on\n"
	"\tthat line are taken at the write.  The line comes from the\n"
	"\tvideo timing then.  50 Hz and 60 Hz do not share a 512-cycle\n"
	"\tline.  This does not stop the CPU.  It is not a scanline\n"
	"\tinspector and not a stop hook.\n";


static bool ensure(uint32_t need)
{
	struct pt_event *fresh;
	uint32_t cap;

	if (events && need <= capacity)
		return true;
	cap = capacity ? capacity : 32u;
	while (cap < need)
	{
		if (cap > (UINT32_MAX / 2u))
			return false;
		cap *= 2u;
	}
	fresh = realloc(events, (size_t)cap * sizeof(*fresh));
	if (!fresh)
	{
		fprintf(stderr, "paltrace: could not allocate %u events.\n", cap);
		return false;
	}
	events = fresh;
	capacity = cap;
	return true;
}

void PalTrace_Note(uint32_t addr, uint16_t color)
{
	struct pt_event *ev;
	int frame, hbl, pos;

	if (!PalTrace_Active)
		return;

	/*
	 * Sample while the write access is still the current bus cycle.
	 * Save prints these fields; it does not ask the video code again.
	 */
	Video_GetPosition_OnWriteAccess(&frame, &hbl, &pos);

	if (!ensure(count + 1u))
		return;

	ev = &events[count++];
	ev->seq = next_seq++;
	ev->pc = regs.instruction_pc;
	ev->reg = addr & 0x00fffffeu;
	ev->color = color;
	ev->vbl = nVBLs;
	ev->hbl = hbl;
	ev->pos = pos;
	ev->frame = frame;
}

static bool write_pal_log(FILE *fp)
{
	uint32_t i;

	if (fprintf(fp, "HPISTPAL 1\n") < 0)
		return false;
	for (i = 0; i < count; i++)
	{
		const struct pt_event *ev = &events[i];

		if (fprintf(fp,
			    "event %" PRIu32 " pc %08" PRIx32
			    " reg %06" PRIx32 " value %04" PRIx32
			    " vbl %d hbl %d pos %d frame %d\n",
			    ev->seq, ev->pc, ev->reg, (uint32_t)ev->color,
			    ev->vbl, ev->hbl, ev->pos, ev->frame) < 0)
			return false;
	}
	return ferror(fp) == 0;
}

static int cmd_on(void)
{
	count = 0;
	next_seq = 1;
	PalTrace_Active = true;
	return DEBUGGER_CMDDONE;
}

static int cmd_off(void)
{
	PalTrace_Active = false;
	return DEBUGGER_CMDDONE;
}

static int cmd_save(const char *path)
{
	FILE *fp;
	bool ok;

	fp = fopen(path, "w");
	if (!fp)
	{
		fprintf(stderr, "paltrace: cannot open '%s' for writing.\n", path);
		perror(NULL);
		return DEBUGGER_CMDDONE;
	}
	ok = write_pal_log(fp);
	if (fclose(fp) != 0)
		ok = false;
	if (!ok)
	{
		fprintf(stderr, "paltrace: writing '%s' failed.\n", path);
		perror(NULL);
	}
	return DEBUGGER_CMDDONE;
}

char *PalTrace_Match(const char *text, int state)
{
	static const char *subs[] = { "off", "on", "save" };
	char *ret;

	ret = DebugUI_MatchHelper(subs, ARRAY_SIZE(subs), text, state);
	if (ret)
		return ret;
	return rl_filename_completion_function(text, state);
}

int PalTrace_Command(int nArgc, char *psArgs[])
{
	if (nArgc < 2)
		return DebugUI_PrintCmdHelp(psArgs[0]);

	if (strcmp(psArgs[1], "on") == 0)
	{
		if (nArgc != 2)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		return cmd_on();
	}
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
	return DebugUI_PrintCmdHelp(psArgs[0]);
}
