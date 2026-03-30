# AutoReCoder — Agent Instructions

This file is read automatically by Claude Code. It defines the rules and context
for the autonomous C→Rust migration agent.

---

## What You Are Doing

You are an autonomous migration agent. Your job is to reduce the number of `unsafe`
blocks in `workspace/src/` while keeping `pass_rate` at exactly 1.000 as measured
by `oracle.py`. You run continuously without stopping.

## The Three Core Files

| File | Role | Mutable? |
|---|---|---|
| `oracle.py` | Locked evaluation engine | **Never** |
| `workspace/src/` | Migrated Rust code | **You only** |
| `program.md` | Migration directives | **Human only** |

## The Two Metrics

- **`pass_rate`** — hard constraint. Must stay at 1.000. The Rust binary must produce
  identical output to the original C binary on every corpus input.
- **`unsafe_count`** — optimization target. Minimize while `pass_rate` stays at 1.000.

## Session Workflow

### One-time setup
```bash
# Put your C/C++ source in workspace/original/ with a driver.c
uv run bootstrap/bootstrap.py   # generates workspace/src/ and workspace/corpus/
uv run oracle.py                # verify: pass_rate: 1.000
```

### The experiment loop (autonomous)
```
1. Run: uv run oracle.py --list-targets   → pick highest-priority function
2. Read the function, identify the unsafe pattern
3. Consult patterns/patterns.md for known approaches
4. Edit workspace/src/ — one function per experiment
5. git add workspace/src/ && git commit -m "experiment: <fn> — <approach>"
6. uv run oracle.py > run.log
7. Parse run.log:
     pass_rate == 1.000 AND unsafe_count decreased?
       YES → keep (commit stays), log to results.tsv, update patterns.md
       NO  → git reset --hard HEAD~1, log to results.tsv, try different approach
8. Repeat from step 1. Never stop.
```

## What You May Modify

| Path | Allowed? |
|---|---|
| `workspace/src/**/*.rs` | YES |
| `patterns/patterns.md` | YES |
| `results/results.tsv` | YES (append only) |
| `oracle.py` | **NEVER** |
| `bootstrap/bootstrap.py` | **NEVER** |
| `workspace/corpus/` | **NEVER** |
| `Cargo.toml` | Only with human approval |

## Key Constraints

1. One function per experiment — keep diffs small and reviewable
2. Never add `unsafe` blocks that weren't there before — that's backward progress
3. Never use `std::mem::transmute` as a workaround
4. `workspace/corpus/` is sacred — generated once by bootstrap, never regenerated
5. Every experiment must end with either a commit (keep) or `git reset --hard HEAD~1`
6. Log every experiment to `results/results.tsv` — even compile failures

## Rust Compiler Error Codes as Signal

Compile failures are not dead ends — `rustc` tells you exactly what's wrong:

| Error | Meaning | Resolution |
|---|---|---|
| `E0502` / `E0503` / `E0505` | Borrow conflict | Split scope, reborrow, or owned copy |
| `E0382` | Use after move | `.clone()`, use reference, or restructure |
| `E0499` | Multiple mutable borrows | `RefCell`, split data, restructure loop |
| `E0716` | Temporary dropped too early | Bind to named `let` variable |
| `E0308` | Type mismatch | Check pointer/slice confusion |

After 3 consecutive failures on the same function: skip it, mark `[DEFERRED]` in patterns.md.

## results.tsv Format

Tab-separated, one row per experiment:
```
commit_hash  pass_rate  unsafe_before  unsafe_after  compile_status  function_name  pattern_used  description
```

Use `"reverted"` as commit_hash for discarded experiments.

## Irreducible Unsafe

Some unsafe cannot be eliminated — mark with a comment and skip:
```rust
// IRREDUCIBLE UNSAFE: FFI boundary — called from C code
// IRREDUCIBLE UNSAFE: hardware interface — mmap/ioctl
// IRREDUCIBLE UNSAFE: inline assembly
```

These do not count against the goal.

## Done Condition

Stop when:
- `unsafe_count == 0` — full migration achieved, or
- All remaining unsafe is marked `IRREDUCIBLE UNSAFE`, or
- Human halts the session

Print a summary when done:
```
Migration complete.
Starting unsafe_count: <N>
Final unsafe_count:    <M>
Irreducible unsafe:    <K>
Total experiments:     <T>  (kept: <K1>, discarded: <K2>, compile failures: <K3>)
```
