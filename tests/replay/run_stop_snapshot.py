#!/usr/bin/env python3
"""statesave from the one stop inside a recording, then reststop it.

The breakpoint stays armed across the save, so continuing does not clear
the debugger flag. The per-VBL trace must still match a recording that
never entered the debugger. A second process reststops that file and
must be back on the same unexecuted addq.
"""

import os
import sys
import tempfile

import run_stop_recording as recording
import run_stopped as stopped

PC_ENTRY = 0x1100


def record_and_save(hatari, prg, home, parse_path, rec, snap):
    session = recording.Session(hatari, prg, home, parse_path, rec)
    try:
        session.wait_prompt()
        here = recording.parse_regs(session.cmd("r"))
        if (here["pc"] != recording.PC_SPACE or here["opcode"] != recording.OP_SPACE
                or here["d1"] != 2 or here["d2"] != 0):
            recording.fail("stop is not the unexecuted space addq: pc=%08X op=%04X d1=%X d2=%X insn=%s"
                           % (here["pc"], here["opcode"], here["d1"], here["d2"], here["insn"]),
                           session.stdout, session.stderr)
        session.cmd("statesave %s" % snap)
        if not os.path.isfile(snap) or os.path.getsize(snap) < 64:
            recording.fail("statesave did not write the snapshot", session.stdout, session.stderr)
        code = session.continue_to_end()
        if code != 0 or b"replay recorded\n" not in session.stdout:
            recording.fail("recording did not complete after statesave (exit %s)" % code,
                           session.stdout, session.stderr)
        return here
    finally:
        session.close()


def restore(hatari, prg, home, parse_path, snap):
    session = stopped.Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        session.cmd("b all")
        session.cmd("reststop %s" % snap)
        return stopped.parse_regs(session.cmd("r"))
    finally:
        session.close()


def main():
    if len(sys.argv) != 3:
        recording.fail("usage: run_stop_snapshot.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        recording.fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-stop-snap-") as home:
        clean = os.path.join(home, "clean.txt")
        rec = os.path.join(home, "rec.txt")
        snap = os.path.join(home, "snap.sav")
        stop_parse = os.path.join(home, "stop.txt")
        entry_parse = os.path.join(home, "entry.txt")
        with open(stop_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $%X && d2 = 0 :quiet\n" % recording.PC_SPACE)
        with open(entry_parse, "w", encoding="ascii") as fh:
            fh.write("b pc=$%x :quiet\n" % PC_ENTRY)

        recording.record_clean(hatari, prg, home, clean)
        port = recording.hold_hrdb_port()
        try:
            here = record_and_save(hatari, prg, home, stop_parse, rec, snap)
            got = recording.trace_lines(rec)
            want = recording.trace_lines(clean)
            if len(got) != recording.RUN_VBLS or got != want:
                first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), None)
                recording.fail("statesave perturbed the recording "
                               "(stopped %d, clean %d, first frame %s)"
                               % (len(got), len(want), "none" if first is None else first + 1))
            back = restore(hatari, prg, home, entry_parse, snap)
        finally:
            port.close()

    if (back["pc"] != here["pc"] or back["opcode"] != here["opcode"]
            or back["d1"] != here["d1"] or back["d2"] != here["d2"]
            or (back["sr"] & 0x2000) == 0):
        recording.fail("reststop is not the saved stop: pc=%08X op=%04X d1=%X d2=%X sr=%04X insn=%s"
                       % (back["pc"], back["opcode"], back["d1"], back["d2"], back["sr"], back["insn"]))
    print("pass statesave-during-stop stop_pc=%08X d1=%X d2=%X frames=%d insn=%s"
          % (back["pc"], back["d1"], back["d2"], recording.RUN_VBLS, back["insn"]))


if __name__ == "__main__":
    main()
