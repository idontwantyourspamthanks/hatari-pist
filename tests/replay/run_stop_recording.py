#!/usr/bin/env python3
"""Record while the debugger stops once, then continue to the end.

The breakpoint matches the space spin's addq only while D2 is still 0, so
it fires once, with that addq not yet executed. Continuing leaves the
breakpoint in place, so the per-instruction debugger flag stays set.
The per-VBL trace must match a recording that never entered the debugger.
Replaying this file with the flag on, and one changed key, must diverge
after that key.
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
PC_SPACE = 0x111E
OP_SPACE = 0x5282  # addq.l #1,d2
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


def machine_args(hatari, prg, record_keys):
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
        "--run-vbls", str(RUN_VBLS),
    ]
    if record_keys:
        args += [
            "--replay-key", "%d:39:down" % KEY_FRAME,
            "--replay-key", "25:39:up",
        ]
    args.append(prg)
    return args


class Session:
    def __init__(self, hatari, prg, home, parse_path, rec):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "replay-stop-recording"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        args = machine_args(hatari, prg, True)
        args[-1:-1] = [
            "--replay-record", rec,
            "--parse", parse_path,
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

    def continue_to_end(self, timeout=60):
        if self.proc.poll() is not None:
            fail("hatari exited before continue", self.stdout, self.stderr)
        self.proc.stdin.write(b"c\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        quiet_at = None
        while time.monotonic() < deadline:
            got = self._read(0.05)
            if self.proc.poll() is not None:
                while self._read(0.05):
                    pass
                return self.proc.returncode
            if self.stderr.endswith(PROMPT):
                if got or quiet_at is None:
                    quiet_at = time.monotonic()
                elif time.monotonic() - quiet_at >= PROMPT_QUIET:
                    fail("breakpoint fired again after continue", self.stdout, self.stderr)
            else:
                quiet_at = None
        fail("recording did not finish after continue", self.stdout, self.stderr)


def parse_regs(text):
    regs = {}
    for name, value in re.findall(r"\b([DA][0-7]) ([0-9A-F]{8})\b", text):
        regs[name] = int(value, 16)
    sr = re.findall(r"SR=([0-9A-F]{4})", text)
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    if "D1" not in regs or "D2" not in regs or not sr or not dis:
        raise RuntimeError("register dump is incomplete\n" + text)
    pc, opcode, insn = dis[-1]
    return {
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "sr": int(sr[-1], 16),
        "d1": regs["D1"],
        "d2": regs["D2"],
    }


def trace_lines(path):
    lines = []
    with open(path, "r", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("trace "):
                lines.append(line.rstrip("\n"))
    return lines


def run_batch(hatari, prg, home, extra, record_keys):
    env = os.environ.copy()
    env["HATARI_TEST"] = "replay-stop-recording"
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    env["HOME"] = home
    args = machine_args(hatari, prg, record_keys)
    args[-1:-1] = extra
    return subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)


def record_clean(hatari, prg, home, rec):
    proc = run_batch(hatari, prg, home, ["--replay-record", rec], True)
    if proc.returncode != 0 or b"replay recorded\n" not in proc.stdout:
        fail("debugger-off record did not complete", proc.stdout, proc.stderr)


def record_stopping(hatari, prg, home, parse_path, rec):
    session = Session(hatari, prg, home, parse_path, rec)
    try:
        session.wait_prompt()
        stopped = parse_regs(session.cmd("r"))
        if stopped["pc"] != PC_SPACE or stopped["opcode"] != OP_SPACE or stopped["d1"] != 2 or stopped["d2"] != 0:
            fail("stop is not the unexecuted space addq: pc=%08X op=%04X d1=%X d2=%X insn=%s"
                 % (stopped["pc"], stopped["opcode"], stopped["d1"], stopped["d2"], stopped["insn"]),
                 session.stdout, session.stderr)
        code = session.continue_to_end()
        if code != 0 or b"replay recorded\n" not in session.stdout:
            fail("recording did not complete after the stop (exit %s)" % code,
                 session.stdout, session.stderr)
        return stopped
    finally:
        session.close()


def replay(hatari, prg, home, parse_path, rec):
    return run_batch(hatari, prg, home, ["--replay", rec, "--parse", parse_path], False)


def diverged_frame(stdout):
    text = stdout.decode("latin-1", errors="replace")
    found = re.findall(r"^replay diverged at frame (\d+)$", text, re.M)
    if len(found) != 1:
        return None
    return int(found[0])


def main():
    if len(sys.argv) != 3:
        fail("usage: run_stop_recording.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-stop-rec-") as home:
        clean = os.path.join(home, "clean.txt")
        rec = os.path.join(home, "rec.txt")
        other = os.path.join(home, "other.txt")
        stop_parse = os.path.join(home, "stop.txt")
        replay_parse = os.path.join(home, "replay.txt")
        with open(stop_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $%X && d2 = 0 :quiet\n" % PC_SPACE)
        with open(replay_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $1\n")

        record_clean(hatari, prg, home, clean)
        port = hold_hrdb_port()
        try:
            stopped = record_stopping(hatari, prg, home, stop_parse, rec)
        finally:
            port.close()

        lines = open(rec, "r", encoding="ascii").read().splitlines()
        if "debugger yes" not in lines:
            fail("stopped recording does not say the debugger was on")
        got = trace_lines(rec)
        want = trace_lines(clean)
        if len(got) != RUN_VBLS or got != want:
            first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), None)
            fail("stopped trace does not match the run that never entered the debugger "
                 "(stopped %d, clean %d, first frame %s)"
                 % (len(got), len(want), "none" if first is None else first + 1))

        matched = replay(hatari, prg, home, replay_parse, rec)
        if matched.returncode != 0 or b"replay matched\n" not in matched.stdout:
            fail("replay of the stopped recording did not match", matched.stdout, matched.stderr)
        if b"Debugger:" in matched.stdout or b"Debugger:" in matched.stderr:
            fail("matching replay entered the debugger", matched.stdout, matched.stderr)

        replaced = [("input %d 1c down" % KEY_FRAME) if line == ("input %d 39 down" % KEY_FRAME) else line
                    for line in lines]
        if replaced == lines:
            fail("negative recording did not change the key")
        with open(other, "w", encoding="ascii") as fh:
            fh.write("\n".join(replaced) + "\n")
        changed = replay(hatari, prg, home, replay_parse, other)
        frame = diverged_frame(changed.stdout)
        if changed.returncode == 0 or frame is None:
            fail("changed key did not diverge", changed.stdout, changed.stderr)
        if frame <= KEY_FRAME or frame > RUN_VBLS:
            fail("changed key diverged at frame %d" % frame, changed.stdout, changed.stderr)

    print("pass stop-during-record stop_pc=%08X d1=%X d2=%X frames=%d diverged=%d insn=%s"
          % (stopped["pc"], stopped["d1"], stopped["d2"], len(got), frame, stopped["insn"]))


if __name__ == "__main__":
    main()
