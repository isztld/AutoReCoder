# AutoReCoder

Autonomous C/C++ → Rust code migration system.

AutoReCoder takes a C codebase, produces a provably-correct (but fully unsafe) Rust translation
via [c2rust](https://github.com/immunant/c2rust), then runs an agent loop that **incrementally
removes** `unsafe` blocks — one function at a time — without ever breaking correctness.

## How It Works

The key insight: instead of translating C to Rust in one shot (which fails for large, complex
codebases), start from a working but fully-unsafe baseline and make it safe incrementally.

```
C source
    │
    ▼  bootstrap.py (runs once)
workspace/src/    ← c2rust translation [100% unsafe, but correct]
workspace/corpus/ ← locked fuzzing corpus
    │
    ▼  oracle.py (locked ground truth)
    ├── pass_rate    = Rust output matches C output on every corpus input
    └── unsafe_count = number of `unsafe` blocks remaining
    │
    ▼  agent loop via program.md (runs until unsafe_count == 0)
    ├── pass_rate == 1.0 AND unsafe_count ↓  →  keep commit, log, update patterns
    └── otherwise                            →  git reset --hard HEAD~1, try again
```

Two metrics drive everything:
- **`pass_rate`** — hard constraint, must stay at 1.000. The Rust binary must produce identical
  output to the original C binary on every corpus input.
- **`unsafe_count`** — optimization target. Minimize while `pass_rate` stays at 1.000.

## Prerequisites

**System dependencies (macOS):**

```bash
brew install llvm@16 cmake bear radamsa
```

> c2rust requires **LLVM 16** specifically — LLVM 17+ has breaking API changes in its AST exporter.
> `bear` is needed to extract `compile_commands.json` from Makefile-based projects.
> `radamsa` is optional but improves corpus quality.

**Rust toolchain:**

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup install nightly
rustup install nightly-2022-08-08   # required by c2rust's build scripts
rustup component add miri --toolchain nightly
```

**Python package manager:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

## Quickstart

### 0. Clone the repository

```bash
git clone --recurse-submodules https://github.com/isztld/AutoReCoder.git
cd AutoReCoder
```

The `--recurse-submodules` flag is required to pull in `vendor/c2rust`.

### 1. Build c2rust (required once)

```bash
cd vendor/c2rust
LLVM_CONFIG_PATH=/opt/homebrew/opt/llvm@16/bin/llvm-config \
  cargo +nightly-2022-08-08 build --release -p c2rust
cd ../..
```

> The `-p c2rust` flag skips `c2rust-refactor`, which requires additional Python 2 code
> generation scripts and is not needed for transpilation.

### 2. Prepare your C source

Copy your C/C++ source into `workspace/original/`. You must also write a **driver** — a small
program that reads from stdin, calls your library's main entry point, and writes to stdout.
This is what the oracle fuzzes and diffs against the Rust translation.

`workspace/original/` contains a ready-to-use example: [miniz](https://github.com/richgel999/miniz)
(a single-file zlib implementation) with a compress/decompress round-trip driver.

### 3. Run bootstrap (once per codebase)

```bash
uv run bootstrap/bootstrap.py
```

This detects your build system, runs c2rust, verifies the baseline passes at `pass_rate: 1.000`,
and generates a locked corpus of 500 test inputs. It prints progress as it goes:

```
[OK] Build system: make
[OK] compile_commands.json generated
[OK] c2rust translation complete → workspace/src/ (1,247 unsafe blocks)
[OK] Baseline pass_rate: 1.000
[OK] Corpus generated: 500 inputs → workspace/corpus/
[OK] Bootstrap complete. Starting unsafe_count: 1247
```

After bootstrap, commit the generated baseline and tag it:

```bash
git add workspace/src/ workspace/callgraph.json
git commit -m "bootstrap: c2rust baseline"
git tag baseline
uv run oracle.py   # verify: pass_rate: 1.000
```

> `workspace/corpus/` is in `.gitignore` by design — corpus inputs are binary files that
> bloat the repository. They live only on the machine running the migration.

### 4. Run the migration agent

Open the repo in [Claude Code](https://claude.ai/code) and use this prompt to start:

```
Read program.md and run the migration agent loop. Start from setup verification,
then run experiments continuously. Log every result to results/results.tsv.
```

Claude Code automatically loads `CLAUDE.md` as its system context, so all constraints
and output formats are already in place. The agent will:

1. Call `uv run oracle.py --list-targets` to rank unsafe functions
2. Edit `workspace/src/` — one function per experiment
3. Commit, run `uv run oracle.py`, check pass_rate and unsafe_count
4. Keep the commit if both metrics are satisfied; otherwise `git reset --hard HEAD~1`
5. Log every attempt to `results/results.tsv` and update `patterns/patterns.md`
6. Repeat until `unsafe_count == 0` or you halt it

To watch progress in real time:
```bash
tail -f results/results.tsv
```

You can also run experiments manually — the oracle and git loop work the same way.

## Oracle Interface

```bash
uv run oracle.py                  # full evaluation (build, diff test, unsafe count)
uv run oracle.py --unsafe-only    # count unsafe blocks only (fast, no compile)
uv run oracle.py --list-targets   # ranked JSON list of functions to migrate next
```

Output format (stable — the agent parses this):
```
pass_rate: 1.000
unsafe_count: 247
unsafe_fns: ["parse_header", "alloc_node", "compress_block"]
compile_status: ok
miri_errors: 0
peak_memory_mb: 142
```

## Pattern Library

`patterns/patterns.md` is a growing library of safe Rust equivalents for common C idioms,
maintained by the agent across experiments. Each entry tracks success rate so the agent
can prioritise approaches that have worked before.

Patterns included by default:
- `redundant-inner-unsafe` — c2rust blanket-wraps function bodies in `unsafe {}`; if the body
  contains no unsafe operations (pure arithmetic, comparisons, constant ops), just remove the block
- `raw-pointer-array-access` — `ptr[i]` → slice indexing via `from_raw_parts`
- `malloc-free-pair` → `Box::new()` / `Vec`
- `manual-string-buffer` → `format!()` / `String::with_capacity()`
- `linked-list-node` → `Option<Box<Node>>`
- `global-mutable-state` → `AtomicI32` / `OnceLock<Mutex<T>>`
- `transmute-cast` → `f32::to_bits()`, `from_be_bytes()`
- `arena-allocator` → `bumpalo` or index-based approach

## Supported Build Systems

| Build System | Detection file | How compile_commands.json is extracted |
|---|---|---|
| CMake | `CMakeLists.txt` | `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| Make | `Makefile` | `bear -- make` |
| Autoconf | `configure` | `./configure && bear -- make` |
| Meson | `meson.build` | `meson setup` (native support) |

## Further Reading

- [`CLAUDE.md`](CLAUDE.md) — agent instructions (constraints, experiment loop, done conditions)
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — technical design of each component
- [`patterns/patterns.md`](patterns/patterns.md) — C→Rust migration pattern library
- [`program.md`](program.md) — agent loop directives (customise per migration session)

## License

MIT
