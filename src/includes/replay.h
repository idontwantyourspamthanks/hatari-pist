/*
 * Hatari - replay.h
 *
 * Copyright (C) 2026 by PiST contributors
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 *
 * Guest-clock record/replay of scripted IKBD keys plus a per-VBL trace of
 * PC and registers. Not a debugger stop hook.
 */

#ifndef HATARI_REPLAY_H
#define HATARI_REPLAY_H

#include <stdbool.h>
#include <stdint.h>

/* NULL on success. The string is a static error for Opt_ShowError. */
extern const char *Replay_SetRecord(const char *path);
extern const char *Replay_SetPlay(const char *path);
extern const char *Replay_SetSnapshot(const char *path);
extern const char *Replay_SetSnapshotAt(const char *text);
extern const char *Replay_AddKey(const char *spec);

/* After option parsing, before Configuration_Apply. Reseeds Hatari_rand
 * when recording or replaying so the time() seed at process start is not
 * an input. Returns false when the options cannot be combined. */
extern bool Replay_Startup(void);

/* After Main_Init, before the CPU runs. May exit on a refused setup. */
extern void Replay_Arm(void);

/* Once per VBL, from Main_WaitOnVbl. Samples first, then injects keys
 * scheduled for this frame. n_vbls is the video frame counter. */
extern void Replay_OnVbl(int n_vbls, uint32_t vbl_count, uint32_t run_vbls);

/* True while recording or replaying, so host keyboard and mouse events
 * are not a second, unsynchronised input. */
extern bool Replay_IgnoresHostInput(void);

/* A key from the media channel. Called from the screen poll, which runs
 * before this vertical blank is counted and before its keys are applied.
 * Recording queues it for that blank. Playback ignores it. With replay
 * off, the key is delivered immediately. */
extern void Replay_MediaKey(uint8_t code, bool down);

/* Debugger command. Loads a recording's keys after reststop. */
extern const char Replay_KeysDescription[];
extern int Replay_KeysCommand(int nArgc, char *psArgs[]);

#endif /* HATARI_REPLAY_H */
