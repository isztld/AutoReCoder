# AutoReCoder

Autonomous C/C++ → Rust code migration system.

AutoReCoder takes a C codebase, produces a provably-correct (but fully unsafe) Rust translation
via [c2rust](https://github.com/immunant/c2rust), then runs an agent loop that incrementally
removes `unsafe` blocks — one function at a time — without ever breaking correctness.

## How It Works

The key insight: instead of translating C to Rust in one shot (which fails for large, complex
codebases), start from a working but fully-unsafe baseline and make it safe incrementally.

```
C source
   │
   ▼ (bootstrap.py — runs once)
     c2rust translation → workspace/src/   [100% unsafe, but correct]
   │
   ▼ (oracle.py — locked ground truth)
   ├── pass_rate = fraction of corpus inputs where Rust output == C output
   └── unsafe_count = number of `unsafe` blocks in workspace/src/
   │
   ▼ (program.md — agent loop, runs until unsafe_count == 0)
      Pick unsafe function → propose safe Rust → commit → run oracle
   ├── pass_rate == 1.0 AND unsafe_count ↓  →  keep, log, update patterns
   └── otherwise →  git reset --hard HEAD~1, try again
```

Two metrics drive everything:
- **`pass_rate`** — hard constraint, must stay at 1.0. The Rust binary must produce identical
  output to the original C binary on every corpus input.
- **`unsafe_count`** — optimization target. Minimize it while `pass_rate` stays at 1.0.

## Prerequisites

**System dependencies:**

```bash
# macOS (homebrew)
brew install llvm@16 cmake bear radamsa

# Rust toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup install nightly
rustup component add miri --toolchain nightly

# Python package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Build c2rust** (required once, takes ~5 minutes):

```bash
git submodule update --init --recursive
cd vendor/c2rust
LLVM_CONFIG_PATH=/opt/homebrew/opt/llvm@16/bin/llvm-config \
  cargo +nightly-2022-08-08 build --release -p c2rust
cd ../..
```

> Note: c2rust requires LLVM 16 specifically. LLVM 17+ has breaking API changes in the
> AST exporter. The `-p c2rust` flag skips `c2rust-refactor` which has additional build
> requirements.

**Python dependencies:**

```bash
uv sync
```

## Quickstart

### 1. Prepare your C source

Copy your C/C++ source into `workspace/original/`. You must also provide a **driver** — a
small program that reads from stdin, calls your library, and writes output to stdout. This
is what the oracle fuzzes and diffs.

See `workspace/original/` for a complete example using [miniz](https://github.com/richgel999/miniz)
(a single-file zlib implementation).

### 2. Run bootstrap (once per codebase)

```bash
uv run bootstrap/bootstrap.py
```

This will:
1. Detect your build system (Makefile, CMake, autoconf, meson)
2. Extract `compile_commands.json` via cmake or `bear`
3. Run `c2rust transpile` to produce an unsafe-but-correct Rust translation
4. Verify the baseline: `pass_rate == 1.000`
5. Generate 500 random corpus inputs (using radamsa if available)
6. Extract the function call graph for target ranking

After bootstrap completes:
```bash
git add workspace/src/ workspace/corpus/ workspace/callgraph.json
git commit -m "bootstrap: c2rust baseline"
git tag baseline
uv run oracle.py   # verify: pass_rate: 1.000
```

### 3. Run the migration agent

Point Claude (or another LLM) at `program.md` and let it run. The agent:

1. Calls `uv run oracle.py --list-targets` to get ranked unsafe functions
2. Edits `workspace/src/` to remove one unsafe block per experiment
3. Commits, runs `uv run oracle.py`, checks pass_rate + unsafe_count
4. Keeps the commit if pass_rate == 1.0 AND unsafe_count decreased
5. Otherwise reverts with `git reset --hard HEAD~1`
6. Logs every experiment to `results/results.tsv`
7. Updates `patterns/patterns.md` with what worked

The agent runs continuously until `unsafe_count == 0` or you halt it.

## Oracle Interface

```bash
uv run oracle.py                  # Full evaluation
uv run oracle.py --unsafe-only    # Count unsafe blocks (no compile/test)
uv run oracle.py --list-targets   # Ranked JSON list of functions to migrate
```

Output format (stable, parsed by agent):
```
pass_rate: 1.000
unsafe_count: 247
unsafe_fns: ["parse_header", "alloc_node", "compress_block"]
compile_status: ok
miri_errors: 0
peak_memory_mb: 142
```

## Results Log

`results/results.tsv` — tab-separated, one row per experiment:

| Column | Description |
|---|---|
| `commit_hash` | Git hash (or `"reverted"` for discarded experiments) |
| `pass_rate` | Oracle pass_rate at time of experiment |
| `unsafe_before` | unsafe_count before the edit |
| `unsafe_after` | unsafe_count after (same as before for failures) |
| `compile_status` | `ok` or `fail` |
| `function_name` | Function that was modified |
| `pattern_used` | Pattern from `patterns/patterns.md` |
| `description` | One-line description of the approach |

## Pattern Library

`patterns/patterns.md` is a growing library of safe-Rust equivalents for common C idioms.
The agent maintains this file — updating success rates and adding new patterns after each experiment.

Current patterns:
- `redundant-inner-unsafe` — c2rust wraps pure arithmetic in `unsafe {}`; just remove the block
- `raw-pointer-array-access` — `ptr[i]` → `slice[i]` via `from_raw_parts`
- `malloc-free-pair` → `Box::new()` / `Vec`
- `manual-string-buffer` → `format!()` / `String::with_capacity()`
- `linked-list-node` → `Option<Box<Node>>`
- `global-mutable-state` → `AtomicI32` / `OnceLock<Mutex<T>>`
- `transmute-cast` → `f32::to_bits()`, `from_be_bytes()`, `bytemuck`
- `arena-allocator` → `bumpalo` / index-based

## Analysis

After running experiments, open the Jupyter notebook to visualize progress:

```bash
uv run jupyter notebook analysis/analysis.ipynb
```

Shows: migration progress curve, pattern effectiveness table, summary statistics.

## Constraints the Agent Follows

1. Only modifies `workspace/src/` and `patterns/patterns.md`
2. Never modifies `oracle.py`, `bootstrap/bootstrap.py`, or `workspace/corpus/`
3. Never adds crates to `Cargo.toml` that mask unsafe behavior
4. Every experiment ends with a git commit (keep) or `git reset --hard HEAD~1` (discard)
5. Logs every experiment to `results/results.tsv` — even failures
6. `rustc` error codes are treated as signal (E0502 → borrow conflict → try reborrow, etc.)

## Supported Build Systems

| Build System | Detection | compile_commands.json |
|---|---|---|
| CMake | `CMakeLists.txt` | `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| Make | `Makefile` | `bear -- make` |
| Autoconf | `configure` | `./configure && bear -- make` |
| Meson | `meson.build` | `meson setup` (native support) |

## Repository Layout

```
autorecoder/
├── oracle.py              ← Locked evaluation engine (never modify after bootstrap)
├── bootstrap/
│   ├── bootstrap.py       ← One-time setup: c2rust + corpus generation
│   └── extract_callgraph.py
├── program.md             ← Agent loop directives (human customizes per session)
├── patterns/
│   └── patterns.md        ← Growing pattern library (agent maintains this)
├── results/
│   └── results.tsv        ← Experiment log (one row per attempt)
├── analysis/
│   └── analysis.ipynb     ← Progress visualization
├── tests/
│   └── test_oracle.py     ← Oracle unit tests
├── vendor/
│   └── c2rust/            ← git submodule: immunant/c2rust
└── workspace/
    ├── original/          ← Your C/C++ source goes here
    ├── corpus/            ← Generated by bootstrap (locked after generation)
    └── src/               ← Generated Rust (agent modifies this)
```

## License

MIT
