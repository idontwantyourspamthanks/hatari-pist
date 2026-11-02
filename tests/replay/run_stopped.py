#!/usr/bin/env python3
"""statesave from the debugger prompt, then replay the later keys.

The previous proof wrote its snapshot from the VBL handler while the
recording was running. This one stops on the fixture's first instruction,
sets supervisor mode (a user-mode ACIA read bus-errors), and statesave
from that prompt. A second process reststops that file, replaykeys the
recording, and breaks on the spin the key selects before its addq runs.
A different key stops on the other spin the same way.
"""

import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time

KEY_FRAME = 15
RUN_VBLS = 40
PC_ENTRY = 0x1100
OP_ENTRY = 0x0839  # btst.b #0,$fffffc00.w
PC_SPACE = 0x111E
PC_OTHER = 0x1122
OP_SPACE = 0x5282
OP_OTHER = 0x5283
PROMPT = b"> "
PROMPT_QUIET = 0.05


def fail(msg, stdout=b"", stderr=b""):
    print("FAIL: " + msg, file=sys.stderr)
    if stdout:
        print("---- stdout ----", file=sys.stderr)
        sys.stderr.buffer.write(stdout[-4000:])
        if not stdout.endswith(b"\n"):
            print(file=sys.stderr)
    if stderr:
        print("---- stderr ----", file=sys.stderr)
        sys.stderr.buffer.write(stderr[-8000:])
        if not stderr.endswith(b"\n"):
            print(file=sys.stderr)
    sys.exit(1)


def hold_hrdb_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 56001))
    except OSError as exc:
        fail("cannot hold 127.0.0.1:56001 (%s); HRDB would own the debugger" % exc)
    sock.listen(1)
    return sock


class Session:
    def __init__(self, hatari, prg, home, parse_path):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "replay-stopped"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        args = [
            hatari,
            "--log-level", "error",
            "--sound", "off",
            "--fast-forward", "on",
            "--confirm-quit", "off",
            "--machine", "st",
            "--cpulevel", "0",
            "--cpuclock", "8",
            "--compatible", "on",
            "--cpu-exact", "on",
            "--data-cache", "off",
            "--addr24", "on",
            "--fpu", "none",
            "--mmu", "off",
            "--blitter", "off",
            "--memsize", "1024",
            "--ttram", "0",
            "--video-timing", "ws3",
            "--monitor", "rgb",
            "--borders", "off",
            "--frameskips", "0",
            "--gemdos-time", "atari",
            "--tos", "none",
            "--joy0", "none",
            "--joy1", "none",
            "--joy2", "none",
            "--joy3", "none",
            "--joy4", "none",
            "--joy5", "none",
            "--parse", parse_path,
            prg,
        ]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def _read(self, timeout):
        streams = []
        if self.proc.stdout is not None:
            streams.append(self.proc.stdout)
        if self.proc.stderr is not None:
            streams.append(self.proc.stderr)
        if not streams:
            return False
        ready, _, _ = select.select(streams, [], [], timeout)
        got = False
        for stream in ready:
            chunk = os.read(stream.fileno(), 65536)
            got = True
            if stream is self.proc.stdout:
                self.stdout += chunk
            else:
                self.stderr += chunk
            if chunk == b"":
                if stream is self.proc.stdout:
                    self.proc.stdout.close()
                    self.proc.stdout = None
                else:
                    self.proc.stderr.close()
                    self.proc.stderr = None
        return got

    def wait_prompt(self, timeout=30):
        deadline = time.monotonic() + timeout
        quiet_at = None
        while time.monotonic() < deadline:
            if b"Remote Debug Listening" in self.stdout or b"Remote Debug Listening" in self.stderr:
                fail("HRDB is listening; it must not be what stops the CPU",
                     self.stdout, self.stderr)
            remain = deadline - time.monotonic()
            got = self._read(min(PROMPT_QUIET, max(0.0, remain)))
            if self.stderr.endswith(PROMPT):
                if got or quiet_at is None:
                    quiet_at = time.monotonic()
                elif time.monotonic() - quiet_at >= PROMPT_QUIET:
                    text = self.stderr[self.mark:]
                    self.mark = len(self.stderr)
                    if text.endswith(PROMPT):
                        text = text[:-len(PROMPT)]
                    return text.decode("latin-1")
            else:
                quiet_at = None
            if self.proc.poll() is not None and self.proc.stderr is None:
                break
        fail("debugger prompt did not return", self.stdout, self.stderr)

    def cmd(self, line, timeout=30):
        if self.proc.poll() is not None:
            fail("hatari exited before command %r" % line, self.stdout, self.stderr)
        self.proc.stdin.write((line + "\n").encode("ascii"))
        self.proc.stdin.flush()
        return self.wait_prompt(timeout)

    def quit(self):
        if self.proc.poll() is None and self.proc.stdin is not None:
            try:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return self.proc.returncode
            self._read(0.1)
        self.close()
        fail("quit did not leave the debugger", self.stdout, self.stderr)


def parse_regs(text):
    regs = {}
    for name, value in re.findall(r"\b([DA][0-7]) ([0-9A-F]{8})\b", text):
        regs[name] = int(value, 16)
    sr = re.findall(r"SR=([0-9A-F]{4})", text)
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    if "D1" not in regs or "D2" not in regs or "D3" not in regs or not sr or not dis:
        raise RuntimeError("register dump is incomplete\n" + text)
    pc, opcode, insn = dis[-1]
    return {
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "sr": int(sr[-1], 16),
        "d1": regs["D1"],
        "d2": regs["D2"],
        "d3": regs["D3"],
    }


def parse_vbl(text):
    found = re.findall(r"#(\d+) \(dec\)", text)
    if not found:
        raise RuntimeError("VBL value missing\n" + text)
    return int(found[-1])


def record(hatari, prg, home, rec):
    env = os.environ.copy()
    env["HATARI_TEST"] = "replay-stopped"
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    env["HOME"] = home
    args = [
        hatari,
        "--log-level", "error",
        "--sound", "off",
        "--fast-forward", "on",
        "--confirm-quit", "off",
        "--machine", "st",
        "--cpulevel", "0",
        "--cpuclock", "8",
        "--compatible", "on",
        "--cpu-exact", "on",
        "--data-cache", "off",
        "--addr24", "on",
        "--fpu", "none",
        "--mmu", "off",
        "--blitter", "off",
        "--memsize", "1024",
        "--video-timing", "ws3",
        "--monitor", "rgb",
        "--borders", "off",
        "--frameskips", "0",
        "--gemdos-time", "atari",
        "--tos", "none",
        "--run-vbls", str(RUN_VBLS),
        "--replay-record", rec,
        "--replay-key", "%d:39:down" % KEY_FRAME,
        "--replay-key", "25:39:up",
        prg,
    ]
    proc = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if proc.returncode != 0 or b"replay recorded\n" not in proc.stdout:
        fail("record did not complete", proc.stdout, proc.stderr)


def save_from_prompt(hatari, prg, home, parse_path, snap):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        # The entry breakpoint would be restored with the snapshot and
        # would fire again on the same instruction. Drop it first.
        session.cmd("b all")
        session.cmd("r sr=$2300")
        stopped = parse_regs(session.cmd("r"))
        vbl = parse_vbl(session.cmd("e VBL"))
        if stopped["pc"] != PC_ENTRY or stopped["opcode"] != OP_ENTRY:
            fail("prompt is not the unexecuted wait: pc=%08X op=%04X insn=%s"
                 % (stopped["pc"], stopped["opcode"], stopped["insn"]),
                 session.stdout, session.stderr)
        if (stopped["sr"] & 0x2000) == 0:
            fail("SR %04X is not supervisor" % stopped["sr"], session.stdout, session.stderr)
        if vbl >= KEY_FRAME:
            fail("VBL %d is already at or past the key on frame %d" % (vbl, KEY_FRAME),
                 session.stdout, session.stderr)
        session.cmd("statesave %s" % snap)
        if not os.path.isfile(snap) or os.path.getsize(snap) < 64:
            fail("statesave did not write the snapshot", session.stdout, session.stderr)
        session.quit()
        return vbl
    finally:
        session.close()


def run_stop(hatari, prg, home, parse_path, rec, snap, stop_pc):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        session.cmd("b all")
        session.cmd("reststop %s" % snap)
        restored = parse_regs(session.cmd("r"))
        if restored["pc"] != PC_ENTRY or restored["opcode"] != OP_ENTRY or (restored["sr"] & 0x2000) == 0:
            fail("reststop is not the saved prompt: pc=%08X op=%04X sr=%04X insn=%s"
                 % (restored["pc"], restored["opcode"], restored["sr"], restored["insn"]),
                 session.stdout, session.stderr)
        armed = session.cmd("replaykeys %s" % rec)
        if "replaykeys: 2 keys" not in armed:
            fail("replaykeys did not arm\n" + armed, session.stdout, session.stderr)
        session.cmd("b pc=$%x :quiet" % stop_pc)
        session.cmd("c", timeout=30)
        stopped = parse_regs(session.cmd("r"))
        session.quit()
        return stopped
    finally:
        session.close()


def main():
    if len(sys.argv) != 3:
        fail("usage: run_stopped.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-stopped-") as home:
        rec = os.path.join(home, "rec.txt")
        snap = os.path.join(home, "snap.sav")
        other = os.path.join(home, "other.txt")
        parse_path = os.path.join(home, "break.txt")
        with open(parse_path, "w", encoding="ascii") as fh:
            fh.write("b pc=$%x :quiet\n" % PC_ENTRY)
        record(hatari, prg, home, rec)
        port = hold_hrdb_port()
        try:
            vbl = save_from_prompt(hatari, prg, home, parse_path, snap)
            space = run_stop(hatari, prg, home, parse_path, rec, snap, PC_SPACE)
            lines = open(rec, "r", encoding="ascii").read().splitlines()
            replaced = [("input %d 1c down" % KEY_FRAME) if line == ("input %d 39 down" % KEY_FRAME) else line
                        for line in lines]
            if replaced == lines:
                fail("negative recording did not change the key")
            with open(other, "w", encoding="ascii") as fh:
                fh.write("\n".join(replaced) + "\n")
            wrong = run_stop(hatari, prg, home, parse_path, other, snap, PC_OTHER)
        finally:
            port.close()

    if space["pc"] != PC_SPACE or space["opcode"] != OP_SPACE or space["d1"] != 2 or space["d2"] != 0:
        fail("space path is not the unexecuted addq: pc=%08X op=%04X d1=%X d2=%X insn=%s"
             % (space["pc"], space["opcode"], space["d1"], space["d2"], space["insn"]))
    if wrong["pc"] != PC_OTHER or wrong["opcode"] != OP_OTHER or wrong["d1"] != 1 or wrong["d3"] != 0:
        fail("other key is not the unexecuted addq: pc=%08X op=%04X d1=%X d3=%X insn=%s"
             % (wrong["pc"], wrong["opcode"], wrong["d1"], wrong["d3"], wrong["insn"]))
    print("pass statesave-from-prompt vbl=%d stop_pc=%08X d1=%X d2=%X insn=%s"
          % (vbl, space["pc"], space["d1"], space["d2"], space["insn"]))
    print("other key stop_pc=%08X d1=%X d3=%X insn=%s" % (wrong["pc"], wrong["d1"], wrong["d3"], wrong["insn"]))


if __name__ == "__main__":
    main()
