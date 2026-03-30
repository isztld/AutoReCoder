# AutoReCoder — Implementation Plan

## Overview

This document is the authoritative step-by-step plan for implementing AutoReCoder from scratch.
Each phase builds on the previous. Complete phases in order. Mark tasks done as you go.

Reference architecture: `CLAUDE.md`. Do not modify `CLAUDE.md` during implementation.

---

## Phase 0 — Repository Initialization

> Goal: Clean git repo with c2rust available and Python environment ready.

- [x] **0.1** `git init` in the project root (`autorecoder/`)
- [x] **0.2** Add c2rust as a git submodule:
  ```bash
  git submodule add https://github.com/immunant/c2rust.git vendor/c2rust
  git submodule update --init --recursive
  ```
- [x] **0.3** Build c2rust (requires Rust nightly + LLVM):
  ```bash
  cd vendor/c2rust
  LLVM_CONFIG_PATH=/opt/homebrew/opt/llvm/bin/llvm-config cargo build --release -p c2rust
  cd ../..
  ```
  The binary will be at `vendor/c2rust/target/release/c2rust`. Verify with `./vendor/c2rust/target/release/c2rust --version`.
  Note: build with `-p c2rust` to skip `c2rust-refactor` (requires Python 2 AST scripts).
- [x] **0.4** Create `.gitignore` (see stub at end of this file)
- [x] **0.5** Verify `pyproject.toml` dependencies install cleanly: `uv sync`
- [x] **0.6** Initial commit:
  ```bash
  git add .
  git commit -m "chore: initial project scaffold with c2rust submodule"
  ```

---

## Phase 1 — Oracle Implementation (`oracle.py`)

> Goal: A locked, uncheatble evaluation engine that returns `(pass_rate, unsafe_count)`.
> This is the equivalent of autoresearch's `prepare.py` + `evaluate_bpb()`.
> Implement in `oracle.py`. After Phase 1 is complete, this file is frozen.

### 1.1 — Unsafe Counter ✓

Implement `count_unsafe(src_dir: Path) -> int`:
- Walk all `.rs` files under `src_dir`
- Count occurrences of the `unsafe` keyword in non-comment, non-string positions
- Count both `unsafe fn` declarations and `unsafe { ... }` blocks
- Return total count
- Use `syn` (via subprocess calling a small Rust helper) or simple regex as fallback

Implement `count_unsafe_fns(src_dir: Path) -> list[str]`:
- Return list of function names that are `unsafe fn` or contain `unsafe {}` blocks
- Used by the agent to pick targets

### 1.2 — Binary Builder ✓

Implement `build_c_binary(original_dir: Path, out: Path) -> bool`:
- Detect build system in `original_dir` (Makefile, CMake, configure script)
- Build with: `-fsanitize=address,undefined -g -O1` flags
- Output binary to `out`
- Return True on success

Implement `build_rust_binary(workspace_src: Path, out: Path) -> bool`:
- Run `cargo build --release` in the workspace
- Capture stderr for error code extraction
- Return True on success
- On failure: extract rustc error codes from stderr and print structured summary

### 1.3 — Differential Test Runner ✓

Implement `run_differential(c_bin: Path, rust_bin: Path, corpus_dir: Path) -> float`:
- For each input file in `corpus_dir/`:
  - Run `c_bin < input` with timeout, capture stdout+stderr+exit_code
  - Run `rust_bin < input` with timeout, capture stdout+stderr+exit_code
  - Compare: stdout must match exactly, exit code must match
  - Count matches / total → `pass_rate`
- Inputs that cause the C binary to crash/timeout are skipped (not counted)
- Return `pass_rate ∈ [0.0, 1.0]`

### 1.4 — Miri Runner (optional, secondary signal) ✓

Implement `run_miri(workspace_src: Path) -> int`:
- Run `cargo +nightly miri test` in workspace
- Count UB errors found by Miri in remaining unsafe blocks
- Return error count (0 = clean)
- This is informational only — does not affect keep/revert decision

### 1.5 — Main Oracle Entry Point ✓

```python
# oracle.py main: called as `uv run oracle.py`
# Prints to stdout (parsed by agent from run.log):
#
#   pass_rate: 1.000
#   unsafe_count: 247
#   unsafe_fns: ["parse_header", "alloc_node", ...]
#   compile_status: ok
#   miri_errors: 0
#   peak_memory_mb: 142
```

All output goes to stdout. Stderr is for debugging only. Format must be stable — the agent parses it.

---

## Phase 2 — Bootstrap Implementation (`bootstrap/bootstrap.py`)

> Goal: One-time setup script. Given a C/C++ codebase in `workspace/original/`,
> produce: (1) c2rust translation in `workspace/src/`, (2) locked oracle corpus in `workspace/corpus/`.
> After bootstrap, `workspace/corpus/` is never touched again.

### 2.1 — Build System Detection ✓

Implement `detect_build_system(src_dir: Path) -> str`:
- Check for: `CMakeLists.txt` → "cmake", `Makefile` → "make", `configure` → "autoconf", `meson.build` → "meson"
- Return build system name
- Raise `UnsupportedBuildSystem` if none found

### 2.2 — Compile Commands Extraction ✓

Implement `extract_compile_commands(src_dir: Path, build_system: str) -> Path`:
- For cmake: `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...`
- For make: use `bear` to intercept compiler calls → `compile_commands.json`
- For autoconf: `./configure && bear -- make`
- Returns path to `compile_commands.json`
- This is required by `c2rust transpile`

### 2.3 — c2rust Invocation ✓

Implement `run_c2rust(compile_commands: Path, out_dir: Path) -> bool`:
- Invoke `vendor/c2rust/target/release/c2rust transpile <compile_commands> -o <out_dir>`
- Capture output
- Verify that `out_dir/src/lib.rs` or equivalent exists
- Return True on success
- The output is valid Rust that compiles — but fully unsafe. This is our baseline.

### 2.4 — Baseline Verification ✓

Implement `verify_baseline(workspace: Path) -> tuple[float, int]`:
- Build C binary with `oracle.build_c_binary()`
- Build Rust baseline with `oracle.build_rust_binary()`
- If either fails: abort with clear error message
- Run differential on a small random corpus (100 inputs)
- Assert `pass_rate == 1.0` — if not, c2rust translation is broken, abort
- Return `(pass_rate=1.0, unsafe_count)` — the starting point

### 2.5 — Corpus Generation ✓

Implement `generate_corpus(c_bin: Path, corpus_dir: Path, n: int = 500)`:
- Strategy 1 (preferred if `radamsa` available): generate random inputs via radamsa mutation
- Strategy 2 (fallback): generate random byte strings of varying lengths (8B–4KB)
- For each candidate input: run `c_bin < input`, keep if it exits cleanly (not segfault)
- Save kept inputs to `corpus_dir/input_XXXX.bin`
- Target: `n` valid inputs in corpus
- After generation, validate all on C binary and record expected outputs to `corpus_dir/expected/`

### 2.6 — Bootstrap Entry Point ✓

```bash
# Usage: uv run bootstrap/bootstrap.py [--src workspace/original] [--n-corpus 500]
# Output:
#   [OK] Build system: cmake
#   [OK] compile_commands.json generated
#   [OK] c2rust translation complete → workspace/src/ (1,247 unsafe blocks)
#   [OK] Baseline pass_rate: 1.000
#   [OK] Corpus generated: 500 inputs → workspace/corpus/
#   [OK] Bootstrap complete. Starting unsafe_count: 1247
```

After this script exits successfully, run `git add workspace/corpus/ workspace/src/` and commit.
Tag this commit: `git tag baseline`

---

## Phase 3 — Migration Agent Loop (`program.md`)

> Goal: The `program.md` directives that drive the autonomous agent.
> This file is what the human customizes per migration session.
> Implement by writing the final `program.md` template.

### 3.1 — Function Dependency Graph ✓

The agent needs to understand call order to migrate leaf functions first.
Implement `bootstrap/extract_callgraph.py`:
- Parse `workspace/src/*.rs` using `syn` (via subprocess) or tree-sitter
- Build a directed graph: function → functions it calls
- Output `workspace/callgraph.json`
- Identify leaf functions (no callees in the same codebase) — these are the first targets

### 3.2 — Unsafe Density Ranking ✓

The agent must pick the best function to tackle next:
- Priority 1: leaf functions (safe to migrate in isolation)
- Priority 2: functions with the most `unsafe` blocks (highest leverage)
- Priority 3: functions that match known patterns in `patterns/patterns.md`

This ranking logic lives in `oracle.py` (exposed as a helper, not the locked eval path).

### 3.3 — Pattern Library Format (`patterns/patterns.md`) ✓

The agent maintains this file. Define its schema:

```markdown
## Pattern: raw-pointer-array-access
- C idiom: `ptr[i]` where ptr is `*mut T` or `*const T`
- Rust safe equivalent: `slice[i]` after establishing `let slice = unsafe { std::slice::from_raw_parts(ptr, len) }`
  ... or better: restructure to pass `&[T]` from caller
- Success rate: 0/0
- Notes:

## Pattern: malloc-free-pair
...
```

Each experiment updates the relevant pattern's success rate.

### 3.4 — `program.md` Template ✓

Write the actual `program.md` file (see the stub already in the project).
It must encode:
- The setup verification steps
- The exact experiment loop (pick → edit → commit → oracle → keep/revert → log → repeat)
- The results.tsv format
- Constraints the agent must obey
- "Never stop" instruction (runs until `unsafe_count == 0` or human halts)

### 3.5 — Results TSV Schema ✓

```
commit_hash  pass_rate  unsafe_before  unsafe_after  compile_status  function_name  pattern_used  description
```

Tab-separated. One row per experiment. Written by agent after every experiment, keep or discard.

---

## Phase 4 — First Migration: Proof of Concept

> Goal: Validate the full pipeline end-to-end on a small, well-understood C library.

### 4.1 — Choose Target Library

Recommended options (in order of difficulty):
1. `miniz` (single-file zlib implementation, ~2000 lines) — **recommended for first test**
2. `libpng` (medium complexity, well-tested)
3. `sqlite3` amalgamation (hard, but famous)

For the proof of concept, use `miniz`. Copy `miniz.c` + `miniz.h` into `workspace/original/`.

### 4.2 — Write a Minimal Test Driver

Create `workspace/original/driver.c`:
- Reads from stdin, calls the library's main entry point (e.g., compress/decompress)
- Writes result to stdout
- This is what the oracle will fuzz and diff

### 4.3 — Run Full Pipeline

```bash
uv run bootstrap/bootstrap.py
uv run oracle.py   # should print: pass_rate: 1.000, unsafe_count: N
```

Verify oracle output is parseable and sensible.

### 4.4 — First Manual Experiment

Manually perform one experiment to validate the loop:
1. Pick the simplest leaf function in `workspace/src/`
2. Remove one `unsafe` block, replacing with safe Rust
3. `git commit -m "experiment: migrate <fn_name> - raw pointer to slice"`
4. `uv run oracle.py > run.log`
5. Parse run.log manually, verify pass_rate and unsafe_count
6. Confirm keep/revert logic works

---

## Phase 5 — Analysis (`analysis/analysis.ipynb`)

> Goal: Visualize migration progress. Mirrors autoresearch's analysis notebook.

### 5.1 — Load results.tsv ✓

- Parse tab-separated log
- Filter to `keep` rows for progress curve, include `discard` for context

### 5.2 — Progress Curve ✓

Plot: experiment number (x) vs. `unsafe_count` (y).
Show: keep experiments (green), discard experiments (red X), compile failures (orange).
This is the equivalent of autoresearch's `val_bpb` curve.

### 5.3 — Pattern Effectiveness Table ✓

Table: pattern_name | attempts | successes | success_rate | avg_unsafe_reduction
Sorted by success_rate descending.

### 5.4 — Summary Statistics ✓

```
Starting unsafe_count:  1247
Current unsafe_count:    843
Total experiments:        89
Kept:                     47 (52.8%)
Discarded:                31 (34.8%)
Compile failures:         11 (12.4%)
Net improvement:        32.3%
```

---

## .gitignore (for Phase 0.4)

```
__pycache__/
*.pyc
.venv/
target/
*.egg-info/
run.log
workspace/src/target/
workspace/original/build/
vendor/c2rust/target/
```

---

## Dependency Notes for pyproject.toml

Python packages needed:
- `requests` — HTTP (corpus download if needed)
- `numpy` — numerical utilities
- `pandas` — TSV parsing in analysis
- `matplotlib` — plotting in analysis
- `networkx` — call graph representation
- `tree-sitter` + `tree-sitter-c` — C AST parsing for corpus generation
- `pytest` — test harness for oracle unit tests

System dependencies (document, not pip-installable):
- `bear` — build interceptor for compile_commands.json (brew install bear)
- `radamsa` — fuzzer for corpus generation (optional but preferred)
- `rustup` with nightly + miri component
- LLVM ≥ 14 (required by c2rust)
- `cmake` (required by c2rust build)

---

## Implementation Order Summary

```
Phase 0  →  Phase 1  →  Phase 2  →  Phase 3  →  Phase 4  →  Phase 5
 Repo         Oracle     Bootstrap    program.md    PoC test    Analysis
 setup        locked     one-time     agent loop    miniz       notebook
```

Do not skip phases. Phase 1 (oracle) must be complete and verified before Phase 2.
Phase 4 is the validation gate — if the full loop doesn't work on miniz, debug before continuing.
