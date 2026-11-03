#!/usr/bin/env python3
"""Restore a mid-recording snapshot and replay the keys that come after it.

The recording is the replay fixture: a key at VBL 15 chooses the space spin.
The snapshot is VBL 10, still in the wait loop, after that frame's keys
(there are none). reststop puts that state back. replaykeys applies the
later keys. A breakpoint on the space spin's addq returns to the prompt
before that addq executes: PC is the addq, D1 is 2, D2 is still 0.

A second session replays a different key and must stop on the other spin
instead, with its addq also not yet executed.
"""

import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time

SNAP_FRAME = 10
KEY_FRAME = 15
RUN_VBLS = 40
PC_ENTRY = 0x1100
PC_SPACE = 0x111E
PC_OTHER = 0x1122
OP_SPACE = 0x5282  # addq.l #1,d2
OP_OTHER = 0x5283  # addq.l #1,d3
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
        env["HATARI_TEST"] = "replay-resume"
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
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    if "D1" not in regs or "D2" not in regs or "D3" not in regs or not dis:
        raise RuntimeError("register dump is incomplete\n" + text)
    pc, opcode, insn = dis[-1]
    return {
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "d1": regs["D1"],
        "d2": regs["D2"],
        "d3": regs["D3"],
    }


def record(hatari, prg, home, rec, snap):
    env = os.environ.copy()
    env["HATARI_TEST"] = "replay-resume"
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
        "--replay-snapshot", snap,
        "--replay-snapshot-at", str(SNAP_FRAME),
        "--replay-key", "%d:39:down" % KEY_FRAME,
        "--replay-key", "25:39:up",
        prg,
    ]
    proc = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    out, err = proc.stdout, proc.stderr
    if proc.returncode != 0:
        fail("record exited %s" % proc.returncode, out, err)
    if b"replay recorded\n" not in out:
        fail("record did not report replay recorded", out, err)
    text = open(rec, "r", encoding="ascii").read().splitlines()
    if "debugger no" not in text:
        fail("recording was not taken with the debugger off", out, err)
    if "input %d 39 down" % KEY_FRAME not in text:
        fail("recording is missing the space key", out, err)
    if not os.path.isfile(snap) or os.path.getsize(snap) < 64:
        fail("snapshot was not written", out, err)


def run_stop(hatari, prg, home, parse_path, rec, snap, stop_pc):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        session.cmd("b all")
        session.cmd("reststop %s" % snap)
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
        fail("usage: run_resume.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-resume-") as home:
        rec = os.path.join(home, "rec.txt")
        snap = os.path.join(home, "snap.sav")
        other = os.path.join(home, "other.txt")
        parse_path = os.path.join(home, "break.txt")
        with open(parse_path, "w", encoding="ascii") as fh:
            fh.write("b pc=$%x :quiet\n" % PC_ENTRY)
        record(hatari, prg, home, rec, snap)
        # The debugger must own stdin. Hold the port only after the record,
        # which is allowed to open HRDB and does not stop on it.
        port = hold_hrdb_port()
        try:
            space = run_stop(hatari, prg, home, parse_path, rec, snap, PC_SPACE)
            lines = open(rec, "r", encoding="ascii").read().splitlines()
            replaced = [("input %d 1c down" % KEY_FRAME) if line == ("input %d 39 down" % KEY_FRAME) else line
                        for line in lines]
            if replaced == lines:
                fail("negative recording did not change the key")
            with open(other, "w", encoding="ascii") as fh:
                fh.write("\n".join(replaced) + "\n")
            wrong = run_stop(hatari, prg, home, parse_path, other, snap, PC_OTHER)
            # A log written while the machine is running has the same input
            # lines and no trace. replaykeys has to accept that and still
            # apply the key. The two runs above keep their trace lines.
            keys_only = os.path.join(home, "keys.txt")
            kept = [line for line in lines if line.startswith("HPISTREPLAY ")
                    or line.startswith("input ") or line.startswith("mouse ")]
            if not any(line.startswith("trace ") for line in lines):
                fail("recording had no trace to strip")
            if not any(line.startswith("input ") for line in kept):
                fail("keys-only log kept no input")
            with open(keys_only, "w", encoding="ascii") as fh:
                fh.write("\n".join(kept) + "\n")
            logged = run_stop(hatari, prg, home, parse_path, keys_only, snap, PC_SPACE)
        finally:
            port.close()

    if space["pc"] != PC_SPACE or space["opcode"] != OP_SPACE or space["d1"] != 2 or space["d2"] != 0:
        fail("space path is not the unexecuted addq: pc=%08X op=%04X d1=%X d2=%X insn=%s"
             % (space["pc"], space["opcode"], space["d1"], space["d2"], space["insn"]))
    if wrong["pc"] != PC_OTHER or wrong["opcode"] != OP_OTHER or wrong["d1"] != 1 or wrong["d3"] != 0:
        fail("other key is not the unexecuted addq: pc=%08X op=%04X d1=%X d3=%X insn=%s"
             % (wrong["pc"], wrong["opcode"], wrong["d1"], wrong["d3"], wrong["insn"]))
    if logged["pc"] != PC_SPACE or logged["opcode"] != OP_SPACE or logged["d1"] != 2 or logged["d2"] != 0:
        fail("keys-only log is not the unexecuted addq: pc=%08X op=%04X d1=%X d2=%X insn=%s"
             % (logged["pc"], logged["opcode"], logged["d1"], logged["d2"], logged["insn"]))
    print("pass replaykeys snapshot_vbl=%d stop_pc=%08X d1=%X d2=%X insn=%s"
          % (SNAP_FRAME, space["pc"], space["d1"], space["d2"], space["insn"]))
    print("other key stop_pc=%08X d1=%X d3=%X insn=%s" % (wrong["pc"], wrong["d1"], wrong["d3"], wrong["insn"]))


if __name__ == "__main__":
    main()
