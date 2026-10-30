#!/usr/bin/env python3
"""Restoring one stop and executing it again matches, including one VBL.

Save the fixture's increment, single-step eight times, restore, and repeat
those steps. Then continue N instructions across at least one vertical blank,
restore, and continue the same N. A second process must discover the same N
and the same trace. No new debugger command: reststop, statesave, s, and c.
"""

import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time

PC_ADDR = 0x1100
RAM_ADDR = 0x1104
VBL_CLOCK = 0x462
OPCODE = 0x5280
BEEF = 0xBEEF
POKED = 0xA5A5
STEPS = 8
# ST 50 Hz, 313 lines of 512 cycles. Wakeup state moves this by a few cycles,
# not by a frame. The margin is one scanline past the interrupt so the fake
# TOS handler's addq at $462 has retired.
NOMINAL_FRAME_CYCLES = 313 * 512
MARGIN_CYCLES = 512
MIN_CPI = 4
PROMPT = b"> "
PROMPT_QUIET = 0.05

# Field order is the order a mismatch is reported in.
STEP_FIELDS = (
    ("pc", "PC"),
    ("sr", "SR"),
    ("D0", "D0"), ("D1", "D1"), ("D2", "D2"), ("D3", "D3"),
    ("D4", "D4"), ("D5", "D5"), ("D6", "D6"), ("D7", "D7"),
    ("A0", "A0"), ("A1", "A1"), ("A2", "A2"), ("A3", "A3"),
    ("A4", "A4"), ("A5", "A5"), ("A6", "A6"), ("A7", "A7"),
    ("vbl", "VBL"),
    ("framecycles", "FrameCycles"),
    ("vbclock", "$462"),
    ("ram", "$1104"),
)


class DumpError(Exception):
    pass


def fail(msg, stdout=b"", stderr=b""):
    print("FAIL: " + msg, file=sys.stderr)
    if stdout:
        print("---- stdout ----", file=sys.stderr)
        sys.stderr.write(stdout.decode("latin-1", "replace")[-4000:])
        if not stdout.endswith(b"\n"):
            print(file=sys.stderr)
    if stderr:
        print("---- stderr ----", file=sys.stderr)
        sys.stderr.write(stderr.decode("latin-1", "replace")[-8000:])
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


def prg_ok(path):
    data = open(path, "rb").read()
    if len(data) < 0x1C + 6 or data[0:2] != b"\x60\x1a":
        fail("fixture is not a TOS PRG: %s" % path)
    text = int.from_bytes(data[2:6], "big")
    if text < 6:
        fail("fixture text size is %d, expected at least 6" % text)
    body = data[0x1C:0x1C + 6]
    if body != bytes([0x52, 0x80, 0x60, 0xFC, 0xBE, 0xEF]):
        fail("fixture text is %s, expected 5280 60fc beef" % body.hex())


def fmt_field(label, value):
    if label in ("VBL", "FrameCycles"):
        return "%d" % value
    if label == "SR" or label == "$1104":
        return "$%04X" % value
    return "$%08X" % value


def first_diff(a, b):
    for key, label in STEP_FIELDS:
        if a[key] != b[key]:
            return label, a[key], b[key]
    return None


def diff_text(where, a, b):
    found = first_diff(a, b)
    if not found:
        return None
    label, av, bv = found
    return "%s field %s: %s vs %s" % (
        where, label, fmt_field(label, av), fmt_field(label, bv))


class Session:
    def __init__(self, hatari, prg, home, parse_path):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "roundtrip"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        args = [
            hatari,
            "--log-level", "error",
            "--sound", "off",
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
    nxt = re.findall(r"Next PC: ([0-9A-Fa-f]{8})", text)
    missing = [n for n in ["D%d" % i for i in range(8)] + ["A%d" % i for i in range(8)] if n not in regs]
    if missing or not sr or not dis or not nxt:
        raise DumpError("register dump is incomplete (%s)\n%s" % (missing, text))
    pc, opcode, insn = dis[-1]
    return {
        "regs": regs,
        "sr": int(sr[-1], 16),
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "next": int(nxt[-1], 16),
    }


def parse_eval(text):
    if "ERROR" in text:
        raise DumpError("evaluate failed\n%s" % text)
    found = re.findall(r"\$([0-9A-Fa-f]+) \(hex\)", text)
    if not found:
        raise DumpError("evaluate value missing\n%s" % text)
    return int(found[-1], 16)


def parse_word(text, addr):
    found = re.findall(r"^%08X: ([0-9A-Fa-f]{4})\b" % addr, text, re.M)
    if not found:
        raise DumpError("RAM word at $%x missing from memdump\n%s" % (addr, text))
    return int(found[-1], 16)


def parse_long(text, addr):
    found = re.findall(r"^%08X: ([0-9A-Fa-f]{8})\b" % addr, text, re.M)
    if not found:
        raise DumpError("RAM long at $%x missing from memdump\n%s" % (addr, text))
    return int(found[-1], 16)


def observe(session):
    state = parse_regs(session.cmd("r"))
    vbl = parse_eval(session.cmd("e VBL"))
    framecycles = parse_eval(session.cmd("e FrameCycles"))
    vbclock = parse_long(session.cmd("m l $%X 1" % VBL_CLOCK), VBL_CLOCK)
    ram = parse_word(session.cmd("m w $%X 1" % RAM_ADDR), RAM_ADDR)
    sample = {
        "pc": state["pc"],
        "sr": state["sr"],
        "opcode": state["opcode"],
        "insn": state["insn"],
        "vbl": vbl,
        "framecycles": framecycles,
        "vbclock": vbclock,
        "ram": ram,
    }
    sample.update(state["regs"])
    return sample


def require_stopped_on_increment(sample, where, session):
    if sample["pc"] != PC_ADDR or sample["opcode"] != OPCODE:
        fail("%s is not the increment: pc=%08X opcode=%04X insn=%s"
             % (where, sample["pc"], sample["opcode"], sample["insn"]),
             session.stdout, session.stderr)
    if sample["sr"] != 0x0300:
        fail("%s SR is %04X, expected 0300" % (where, sample["sr"]),
             session.stdout, session.stderr)
    if sample["D0"] != 0:
        fail("%s D0 is %08X, expected 0; the increment already executed"
             % (where, sample["D0"]),
             session.stdout, session.stderr)


def require_match(where, expect, got, session):
    text = diff_text(where, expect, got)
    if text:
        fail(text, session.stdout, session.stderr)


def clear_breakpoints(session):
    """c N counts instructions. A PC breakpoint would stop the continue."""
    session.cmd("b all")
    listed = session.cmd("b")
    if "No conditional CPU breakpoints." not in listed:
        fail("breakpoint remains; c N would not run N instructions\n" + listed,
             session.stdout, session.stderr)


def instruction_cost(saved, trace):
    prev_fc = saved["framecycles"]
    prev_vbl = saved["vbl"]
    deltas = []
    for step in trace:
        if step["vbl"] == prev_vbl and step["framecycles"] >= prev_fc:
            delta = step["framecycles"] - prev_fc
            if delta > 0:
                deltas.append(delta)
        prev_fc = step["framecycles"]
        prev_vbl = step["vbl"]
    if not deltas:
        return MIN_CPI
    return max(MIN_CPI, min(deltas))


def instructions_for(cycles, cpi):
    return (cycles + cpi - 1) // cpi


def initial_n(saved, trace):
    cpi = instruction_cost(saved, trace)
    fc = saved["framecycles"]
    if fc < NOMINAL_FRAME_CYCLES:
        need = NOMINAL_FRAME_CYCLES - fc + MARGIN_CYCLES
    else:
        need = NOMINAL_FRAME_CYCLES + MARGIN_CYCLES
    return max(1, instructions_for(need, cpi)), cpi


def reststop_saved(session, snap, saved):
    session.cmd("reststop %s" % snap)
    got = observe(session)
    require_stopped_on_increment(got, "reststop", session)
    if got["ram"] != POKED:
        fail("reststop RAM word is %04X, expected poked %04X" % (got["ram"], POKED),
             session.stdout, session.stderr)
    if got["vbl"] != saved["vbl"]:
        fail("reststop VBL is %d, saved VBL is %d" % (got["vbl"], saved["vbl"]),
             session.stdout, session.stderr)
    require_match("reststop", saved, got, session)
    return got


def step_trace(session, count):
    trace = []
    for _ in range(count):
        session.cmd("s")
        trace.append(observe(session))
    return trace


def handler_ran(saved, end):
    return end["vbl"] >= saved["vbl"] + 1 and end["vbclock"] != saved["vbclock"]


def cross_vbl(session, snap, saved, trace):
    """One c N from the saved stop. Retries stay on c, never single-step the frame."""
    n, cpi = initial_n(saved, trace)
    extra = instructions_for(NOMINAL_FRAME_CYCLES, cpi)
    last = None
    ran = n
    for _ in range(6):
        ran = n
        reststop_saved(session, snap, saved)
        session.cmd("c %d" % ran, timeout=90)
        end = observe(session)
        last = end
        if handler_ran(saved, end):
            return ran, end
        if end["vbl"] >= saved["vbl"] + 1:
            # Interrupt count moved before the handler's addq retired.
            n = ran + 1
        else:
            n = ran + extra
    fail("c N did not cross a VBL whose handler ran (last N=%d VBL %d->%d $462 %08X->%08X)"
         % (ran, saved["vbl"], last["vbl"], saved["vbclock"], last["vbclock"]),
         session.stdout, session.stderr)


def run_once(hatari, prg, home, parse_path, snap_path):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        help_text = session.cmd("h reststop")
        if "Unknown command 'reststop'" in help_text or "'reststop'" not in help_text:
            fail("reststop is not a debugger command\n" + help_text,
                 session.stdout, session.stderr)
        unknown = session.cmd("nosuchcmd")
        if "Command 'nosuchcmd' not found." not in unknown:
            fail("unknown command text changed\n" + unknown,
                 session.stdout, session.stderr)

        stopped = observe(session)
        require_stopped_on_increment(stopped, "initial stop", session)
        if stopped["ram"] != BEEF:
            fail("word at $1104 is %04X, expected beef before the poke" % stopped["ram"],
                 session.stdout, session.stderr)

        session.cmd("w w $%X $%X" % (RAM_ADDR, POKED))
        clear_breakpoints(session)
        saved = observe(session)
        require_stopped_on_increment(saved, "stop before statesave", session)
        if saved["ram"] != POKED:
            fail("poke did not store %04X at $1104 (got %04X)" % (POKED, saved["ram"]),
                 session.stdout, session.stderr)

        session.cmd("statesave %s" % snap_path)
        if not os.path.isfile(snap_path):
            fail("statesave did not write %s" % snap_path, session.stdout, session.stderr)
        after_save = observe(session)
        require_match("statesave", saved, after_save, session)
        if after_save["opcode"] != OPCODE:
            fail("statesave executed the increment (%s)" % after_save["insn"],
                 session.stdout, session.stderr)

        trace = step_trace(session, STEPS)
        reststop_saved(session, snap_path, saved)
        for index, expect in enumerate(trace, 1):
            session.cmd("s")
            got = observe(session)
            require_match("step %d" % index, expect, got, session)

        n, endpoint = cross_vbl(session, snap_path, saved, trace)
        reststop_saved(session, snap_path, saved)
        session.cmd("c %d" % n, timeout=90)
        again = observe(session)
        require_match("endpoint", endpoint, again, session)
        if not handler_ran(saved, again):
            if again["vbl"] != saved["vbl"] and again["vbclock"] == saved["vbclock"]:
                fail("VBL moved while $462 did not", session.stdout, session.stderr)
            fail("repeat c %d did not run the VBL handler (VBL %d->%d $462 %08X->%08X)"
                 % (n, saved["vbl"], again["vbl"], saved["vbclock"], again["vbclock"]),
                 session.stdout, session.stderr)

        code = session.quit()
        if code != 0:
            fail("hatari quit with status %s" % code, session.stdout, session.stderr)
        return {"n": n, "trace": trace, "endpoint": endpoint, "saved": saved}
    except DumpError as exc:
        fail(str(exc), session.stdout, session.stderr)
    finally:
        session.close()


def compare_runs(first, second):
    for index, (a, b) in enumerate(zip(first["trace"], second["trace"]), 1):
        text = diff_text("step %d" % index, a, b)
        if text:
            fail("processes disagree: " + text)
    if first["n"] != second["n"]:
        fail("processes discovered different N: %d vs %d (saved FrameCycles %d vs %d)"
             % (first["n"], second["n"],
                first["saved"]["framecycles"], second["saved"]["framecycles"]))
    text = diff_text("endpoint", first["endpoint"], second["endpoint"])
    if text:
        fail("processes disagree: " + text)


def step_sequence(trace):
    return " ".join("$%08X/$%08X" % (step["pc"], step["D0"]) for step in trace)


def main():
    if len(sys.argv) != 3:
        fail("usage: run_roundtrip.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)
    prg_ok(prg)

    port = hold_hrdb_port()
    try:
        with tempfile.TemporaryDirectory(prefix="hatari-roundtrip-") as home:
            parse_path = os.path.join(home, "break.txt")
            with open(parse_path, "w", encoding="ascii") as fh:
                fh.write("b pc=$%x :once :quiet\n" % PC_ADDR)
            first = run_once(hatari, prg, home, parse_path, os.path.join(home, "one.sav"))
            second = run_once(hatari, prg, home, parse_path, os.path.join(home, "two.sav"))
    finally:
        port.close()

    compare_runs(first, second)
    end = first["endpoint"]
    print("pass roundtrip N=%d" % first["n"])
    print("steps %s" % step_sequence(first["trace"]))
    print("endpoint pc=$%08X d0=$%08X VBL=%d FrameCycles=%d"
          % (end["pc"], end["D0"], end["vbl"], end["framecycles"]))


if __name__ == "__main__":
    main()
