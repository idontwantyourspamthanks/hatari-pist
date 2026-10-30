/*
 * Hatari - reststop.c
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * reststop.c - restore one statesave snapshot and stop before its instruction.
 *
 * stateload still only schedules MemorySnapShot_Restore and lets m68k_go
 * resume the CPU. This command uses that same snapshot and that same
 * restore, then enters the stdin debugger before run_func(). The
 * instruction at the restored PC has not executed. One snapshot, not
 * step-back. A pending restore also leaves the CPU loop immediately:
 * do_specialties has already snapshotted spcflags, so the MODE_CHANGE
 * bit set by MemorySnapShot_Restore would otherwise be noticed only
 * after one more instruction.
 */
const char RestStop_fileid[] = "Hatari reststop.c";

#include <stdio.h>
#include <stdbool.h>
#include <stdint.h>

#include "config.h"

#include "configuration.h"
#include "debug_priv.h"
#include "debugui.h"
#include "file.h"
#include "main.h"
#include "memorySnapShot.h"
#include "reststop.h"

static bool pending;

const char RestStop_Description[] =
	"[filename]\n"
	"\tRestore one snapshot written by statesave and return to this\n"
	"\tprompt.  The instruction at the restored PC has not executed.\n"
	"\tOne snapshot, stdin debugger, not step-back.  stateload is\n"
	"\tunchanged and still resumes the CPU.\n";

int RestStop_Command(int nArgc, char *psArgs[])
{
	const char *file;

	if (nArgc > 2)
		return DebugUI_PrintCmdHelp(psArgs[0]);

	if (nArgc > 1)
		file = psArgs[1];
	else
		file = ConfigureParams.Memory.szMemoryCaptureFileName;

	if (!File_Exists(file))
	{
		fprintf(stderr, "reststop: snapshot file '%s' not found.\n", file);
		return DEBUGGER_CMDDONE;
	}

	pending = true;
	/* bConfirm false: a dialog would block the headless prompt. */
	MemorySnapShot_Restore(file, false);
	return DEBUGGER_END;
}

bool RestStop_LeaveLoop(void)
{
	return pending;
}

bool RestStop_EnterPrompt(void)
{
	if (!pending)
		return false;

	pending = false;
	DebugUI(REASON_USER);
	if (bQuitProgram)
		return true;
	return pending;
}
