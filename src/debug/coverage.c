/*
 * Hatari - coverage.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * coverage.c - record executed ST RAM instruction addresses.
 *
 * An address is counted when its instruction has finished. DebugCpu_Check
 * runs after the PC has already advanced, so the count is for the previous
 * PC. The first sample after recording is turned on has no previous PC and
 * is discarded. 'coverage clear' zeros the counts and keeps that previous
 * PC, so the instruction the CPU is about to run is not skipped.
 *
 * This is not the CPU profiler. It stores a count per even ST RAM address
 * and never disassembles.
 */
const char Coverage_fileid[] = "Hatari coverage.c";

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>

#include "config.h"

#if HAVE_LIBREADLINE
# include <readline/readline.h>
#else
# define rl_filename_completion_function(x,y) NULL
#endif

#include "main.h"
#include "configuration.h"
#include "debugui.h"
#include "debug_priv.h"
#include "m68000.h"
#include "stMemory.h"
#include "coverage.h"

static bool recording;          /* true between 'coverage on' and 'off' */
static bool have_prev;          /* false until the first sample after 'on' */
static uint32_t prev_pc;
static uint32_t *counts;        /* one count per even address */
static uint32_t entries;        /* STRamEnd/2 at the moment recording started */
static uint32_t ram_end;        /* STRamEnd captured with the table */
static uint32_t nonzero;        /* how many addresses have a count */

const char Coverage_Description[] =
	"[on|off|clear|save <file>]\n"
	"\n"
	"\tRecord every ST RAM instruction that finishes while recording is\n"
	"\ton.  'on' starts recording and 'off' stops it without forgetting\n"
	"\tthe counts.  The first instruction after 'on' is not counted.\n"
	"\t'clear' zeros the counts and still counts the instruction the CPU\n"
	"\tis about to run.  'save <file>' writes the non-zero counts.\n"
	"\tWith no argument, print whether recording is on and how many\n"
	"\taddresses have a count.\n"
	"\n"
	"\tThis is not the CPU profiler: it does not disassemble, and\n"
	"\t'coverage on' does not enable 'profile'.\n";


/**
 * Allocate the count table for the ST RAM size right now.
 * A later 'on' keeps the table when that size has not changed.
 */
static bool Coverage_Alloc(void)
{
	uint32_t ram, n;

	ram = STRamEnd;
	n = ram / 2;
	if (counts && ram == ram_end && entries == n)
		return true;

	free(counts);
	counts = NULL;
	entries = 0;
	ram_end = 0;
	nonzero = 0;
	if (n == 0)
	{
		fprintf(stderr, "ERROR: no ST RAM, coverage not enabled.\n");
		return false;
	}
	counts = calloc((size_t)n, sizeof(*counts));
	if (!counts)
	{
		perror("ERROR: coverage table allocation failed");
		return false;
	}
	entries = n;
	ram_end = ram;
	return true;
}

bool Coverage_IsRecording(void)
{
	return recording;
}

/**
 * Count the instruction that just finished.
 *
 * M68000_GetPC() is already the next instruction. The first call after
 * 'coverage on' only stores that PC. Later calls count the stored one.
 * The PC is masked to 24 bits when the machine's address space is 24-bit,
 * as Profile_CpuUpdate does. Odd addresses and anything outside the ST RAM
 * table (TOS, the cartridge, TT RAM) are ignored. Counts saturate.
 */
void Coverage_Update(void)
{
	uint32_t pc, prev, idx;

	if (!recording || !counts)
		return;

	pc = M68000_GetPC();
	if (ConfigureParams.System.bAddressSpace24)
		pc &= 0xffffff;

	prev = prev_pc;
	prev_pc = pc;
	if (!have_prev)
	{
		have_prev = true;
		return;
	}

	if (prev & 1)
		return;
	if (prev >= ram_end)
		return;
	idx = prev >> 1;
	if (idx >= entries)
		return;
	if (counts[idx] == UINT32_MAX)
		return;
	if (counts[idx] == 0)
		nonzero++;
	counts[idx]++;
}

/**
 * Build a temporary name in the same directory as path.
 */
static bool Coverage_TempPath(char *tmp, size_t tmpsz, const char *path)
{
	static unsigned seq;
	const char *slash;
	size_t dirlen;
	int wrote;

	slash = strrchr(path, PATHSEP);
	dirlen = slash ? (size_t)(slash - path + 1) : 0;
	if (dirlen >= tmpsz)
		return false;
	if (dirlen)
		memcpy(tmp, path, dirlen);
	wrote = snprintf(tmp + dirlen, tmpsz - dirlen,
			 ".hatari-coverage-%u.tmp", ++seq);
	return wrote > 0 && (size_t)wrote < tmpsz - dirlen;
}

/**
 * Write the stable text format. Only counts greater than zero, in
 * ascending address order. A run that recorded nothing is the two
 * header lines and "Addresses: 0".
 */
static bool Coverage_Write(FILE *fp)
{
	uint32_t i;

	if (fprintf(fp, "Hatari CPU coverage\nAddresses: %" PRIu32 "\n",
		    nonzero) < 0)
		return false;
	if (!counts)
		return true;
	for (i = 0; i < entries; i++)
	{
		if (!counts[i])
			continue;
		if (fprintf(fp, "$%06x %" PRIu32 "\n",
			    (unsigned)(i << 1), counts[i]) < 0)
			return false;
	}
	return ferror(fp) == 0;
}

/**
 * Replace path with the finished temporary. The destination is not
 * opened until the temporary is complete, so a failure leaves an
 * existing file alone.
 */
static bool Coverage_Replace(const char *tmp, const char *path)
{
	if (rename(tmp, path) == 0)
		return true;
#ifdef WIN32
	/* rename() will not replace an existing file. Remove it only
	 * after the temporary is complete, then try once more. */
	if (remove(path) != 0 && errno != ENOENT)
	{
		fprintf(stderr, "ERROR: replacing '%s' failed!\n", path);
		perror(NULL);
		return false;
	}
	if (rename(tmp, path) == 0)
		return true;
#endif
	fprintf(stderr, "ERROR: replacing '%s' failed!\n", path);
	perror(NULL);
	return false;
}

/**
 * Write counts to path. On failure the previous file, if any, is kept.
 */
static bool Coverage_Save(const char *path)
{
	char tmp[FILENAME_MAX];
	FILE *fp;
	bool ok;

	if (!Coverage_TempPath(tmp, sizeof(tmp), path))
	{
		fprintf(stderr, "ERROR: coverage path too long: '%s'\n", path);
		return false;
	}
	fp = fopen(tmp, "w");
	if (!fp)
	{
		fprintf(stderr, "ERROR: opening '%s' for writing failed!\n", path);
		perror(NULL);
		return false;
	}
	ok = Coverage_Write(fp);
	if (fclose(fp) != 0)
		ok = false;
	if (!ok)
	{
		fprintf(stderr, "ERROR: writing '%s' failed!\n", path);
		perror(NULL);
		remove(tmp);
		return false;
	}
	if (!Coverage_Replace(tmp, path))
	{
		remove(tmp);
		return false;
	}
	fprintf(stderr, "Coverage saved to '%s'.\n", path);
	return true;
}

static int Coverage_On(void)
{
	if (!Coverage_Alloc())
		return DEBUGGER_CMDDONE;
	if (!recording)
	{
		recording = true;
		/* No previous instruction yet, so the next sample is dropped. */
		have_prev = false;
	}
	fprintf(stderr, "Coverage recording on.\n");
	return DEBUGGER_CMDDONE;
}

static int Coverage_Off(void)
{
	recording = false;
	fprintf(stderr, "Coverage recording off.\n");
	return DEBUGGER_CMDDONE;
}

static int Coverage_Clear(void)
{
	if (counts && entries)
		memset(counts, 0, (size_t)entries * sizeof(*counts));
	nonzero = 0;
	/* have_prev and prev_pc stay. Clearing at a stop must still count
	 * the instruction the CPU is about to run. */
	fprintf(stderr, "Coverage counts cleared.\n");
	return DEBUGGER_CMDDONE;
}

char *Coverage_Match(const char *text, int state)
{
	static const char *subs[] = { "clear", "off", "on", "save" };
	char *ret;

	ret = DebugUI_MatchHelper(subs, ARRAY_SIZE(subs), text, state);
	if (ret)
		return ret;
	return rl_filename_completion_function(text, state);
}

/**
 * Command: coverage [on|off|clear|save <file>]
 */
int Coverage_Command(int nArgc, char *psArgs[])
{
	if (nArgc < 2)
	{
		fprintf(stderr, "Coverage is %s, %" PRIu32 " %s.\n",
			recording ? "on" : "off", nonzero,
			nonzero == 1 ? "address" : "addresses");
		return DEBUGGER_CMDDONE;
	}
	if (strcmp(psArgs[1], "on") == 0)
		return Coverage_On();
	if (strcmp(psArgs[1], "off") == 0)
		return Coverage_Off();
	if (strcmp(psArgs[1], "clear") == 0)
		return Coverage_Clear();
	if (strcmp(psArgs[1], "save") == 0)
	{
		if (nArgc < 3)
			return DebugUI_PrintCmdHelp(psArgs[0]);
		Coverage_Save(psArgs[2]);
		return DEBUGGER_CMDDONE;
	}
	return DebugUI_PrintCmdHelp(psArgs[0]);
}
