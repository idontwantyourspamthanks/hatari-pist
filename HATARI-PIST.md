# hatari-pist

PiST's fork of Hatari. See docs/PLAN.md §12 in the PiST repository for the plan.

## Pin

- Upstream: Hatari **v2.6.1** (tag archive, not `main` — 2.6.1 is the baseline PiST's
  drift table and capability probes are written against).
- Source: `https://framagit.org/hatari/hatari/-/archive/v2.6.1/hatari-v2.6.1.tar.bz2`
- sha256: `de2fd445c48ab1c79aebdebf722e1c9e6c8b9cc291a777409d8eb01d145da4f1`
- Fetched: 2026-09-27

## Build

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_DISABLE_FIND_PACKAGE_Readline=ON
cmake --build build
```

Readline is disabled deliberately: PiST's bundled emulator must not link GNU
Readline (PiST PLAN.md §10), and a non-readline build fixes which stream carries
the debugger's `> ` prompt (stderr), making the transport framing deterministic.

## Constraints

- The shipped binary keeps the name **`hatari`** — PiST's `canProbeByProcess()`
  matches that literal to avoid process-probing a GUI binary on the GUI thread.
- Diff discipline: one self-contained new file plus small hooks (the hrdb-main
  precedent), so rebasing onto a newer upstream is a bounded job.

## Licence

GPL, same terms as upstream Hatari. New files carry upstream's "version 2 or at
your option any later version" header, but the work as a whole can only be
conveyed under GPLv2 while `src/cpu/uae/{attributes,types,vm}.h` remain
GPL-2.0-only (PiST PLAN.md §10).
