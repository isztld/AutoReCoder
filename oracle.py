"""
oracle.py — AutoReCoder Evaluation Engine

THIS FILE IS LOCKED. The agent must never modify it.

Equivalent to prepare.py in autoresearch. Provides the ground-truth evaluation
of the current Rust translation:
  - pass_rate: fraction of corpus inputs where Rust output == C output
  - unsafe_count: number of unsafe blocks/fns in workspace/src/

Usage:
    uv run oracle.py                  # full evaluation, prints metrics
    uv run oracle.py --list-targets   # ranked list of unsafe functions to migrate
    uv run oracle.py --unsafe-only    # skip differential test, just count unsafe

Output format (stdout, stable — agent parses this):
    pass_rate: 1.000
    unsafe_count: 247
    unsafe_fns: ["parse_header", "alloc_node", "compress_block"]
    compile_status: ok
    miri_errors: 0
    peak_memory_mb: 142
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Project layout constants — do not change
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
WORKSPACE = ROOT / "workspace"
CORPUS_DIR = WORKSPACE / "corpus"
ORIGINAL_DIR = WORKSPACE / "original"
SRC_DIR = WORKSPACE / "src"

C_BINARY = WORKSPACE / "build" / "c_target"
RUST_BINARY = SRC_DIR / "target" / "release" / "autorecoder_target"

TIMEOUT_PER_INPUT = 5  # seconds per corpus input
MAX_CORPUS_INPUTS = 500


# ---------------------------------------------------------------------------
# 1. Unsafe Counter
# ---------------------------------------------------------------------------

def _strip_comments_and_strings(source: str) -> str:
    """
    Remove line comments, block comments, and string/char literals from Rust source.
    Returns a sanitised string where only real code remains.
    """
    result = []
    i = 0
    n = len(source)
    while i < n:
        # Line comment
        if source[i:i+2] == "//":
            while i < n and source[i] != "\n":
                i += 1
            continue
        # Block comment (can be nested in Rust)
        if source[i:i+2] == "/*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if source[i:i+2] == "/*":
                    depth += 1
                    i += 2
                elif source[i:i+2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        # Raw string literal r"..." or r#"..."#
        if source[i] == "r" and i + 1 < n and source[i+1] in ('"', "#"):
            hashes = 0
            j = i + 1
            while j < n and source[j] == "#":
                hashes += 1
                j += 1
            if j < n and source[j] == '"':
                i = j + 1
                end_pat = '"' + "#" * hashes
                while i < n:
                    if source[i:i+len(end_pat)] == end_pat:
                        i += len(end_pat)
                        break
                    i += 1
                result.append(" ")
                continue
        # Regular string literal
        if source[i] == '"':
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == '"':
                    i += 1
                    break
                i += 1
            result.append(" ")
            continue
        # Char literal
        if source[i] == "'":
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == "'":
                    i += 1
                    break
                i += 1
            result.append(" ")
            continue
        result.append(source[i])
        i += 1
    return "".join(result)


def count_unsafe(src_dir: Path) -> int:
    """
    Count total unsafe blocks and unsafe fn declarations in all .rs files under src_dir.
    Excludes occurrences inside comments and string literals.
    """
    total = 0
    for rs_file in src_dir.rglob("*.rs"):
        try:
            source = rs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = _strip_comments_and_strings(source)
        # Match: `unsafe {`, `unsafe fn`, `unsafe impl`, `unsafe trait`, `unsafe extern`
        total += len(re.findall(r"\bunsafe\b", cleaned))
    return total


def list_unsafe_functions(src_dir: Path) -> list[dict]:
    """
    Return ranked list of functions containing unsafe code.

    Each entry: {"name": str, "file": str, "line": int, "unsafe_count": int, "is_leaf": bool}
    Sorted by: leaf-first, then by unsafe_count descending.
    """
    callgraph_path = WORKSPACE / "callgraph.json"
    leaf_set: set[str] = set()
    if callgraph_path.exists():
        try:
            cg = json.loads(callgraph_path.read_text())
            leaf_set = set(cg.get("leaves", []))
        except (json.JSONDecodeError, KeyError):
            pass

    results = []
    # Regex to find function definitions
    fn_re = re.compile(
        r"(?:pub\s+)?(?:(?:unsafe|async|const|extern(?:\s+\"[^\"]*\")?)\s+)*fn\s+(\w+)"
    )
    unsafe_re = re.compile(r"\bunsafe\b")

    for rs_file in src_dir.rglob("*.rs"):
        try:
            source = rs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = source.splitlines()
        rel_path = str(rs_file.relative_to(src_dir))

        # Find function boundaries by tracking brace depth
        i = 0
        while i < len(lines):
            fn_match = fn_re.search(lines[i])
            if fn_match:
                fn_name = fn_match.group(1)
                fn_line = i + 1  # 1-indexed
                # Collect function body by brace counting
                body_lines = []
                depth = 0
                started = False
                j = i
                while j < len(lines):
                    for ch in lines[j]:
                        if ch == "{":
                            depth += 1
                            started = True
                        elif ch == "}":
                            depth -= 1
                    body_lines.append(lines[j])
                    if started and depth <= 0:
                        break
                    j += 1

                body = "\n".join(body_lines)
                cleaned_body = _strip_comments_and_strings(body)
                unsafe_count = len(re.findall(r"\bunsafe\b", cleaned_body))
                if unsafe_count > 0:
                    results.append({
                        "name": fn_name,
                        "file": rel_path,
                        "line": fn_line,
                        "unsafe_count": unsafe_count,
                        "is_leaf": fn_name in leaf_set,
                    })
                i = j + 1
            else:
                i += 1

    # Sort: leaf-first, then by unsafe_count descending
    results.sort(key=lambda x: (not x["is_leaf"], -x["unsafe_count"]))
    return results


# ---------------------------------------------------------------------------
# 2. Binary Builder
# ---------------------------------------------------------------------------

def _detect_build_system(src_dir: Path) -> str:
    if (src_dir / "CMakeLists.txt").exists():
        return "cmake"
    if (src_dir / "meson.build").exists():
        return "meson"
    if (src_dir / "configure").exists():
        return "autoconf"
    if (src_dir / "Makefile").exists():
        return "make"
    raise RuntimeError(f"No recognised build system in {src_dir}")


def build_c_binary(original_dir: Path, out: Path) -> tuple[bool, str]:
    """
    Build the original C codebase as a binary with sanitizers enabled.
    Returns (success: bool, stderr: str)
    """
    build_system = _detect_build_system(original_dir)
    build_dir = WORKSPACE / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    cflags = "-fsanitize=address,undefined -g -O1"
    env_extra = {"CFLAGS": cflags, "CXXFLAGS": cflags}

    import os
    env = {**os.environ, **env_extra}

    stderr_acc = []

    try:
        if build_system == "cmake":
            r = subprocess.run(
                ["cmake", "-S", str(original_dir), "-B", str(build_dir),
                 "-DCMAKE_BUILD_TYPE=Debug",
                 f"-DCMAKE_C_FLAGS={cflags}",
                 f"-DCMAKE_CXX_FLAGS={cflags}"],
                capture_output=True, text=True, env=env
            )
            stderr_acc.append(r.stderr)
            if r.returncode != 0:
                return False, "\n".join(stderr_acc)
            r = subprocess.run(
                ["cmake", "--build", str(build_dir)],
                capture_output=True, text=True, env=env
            )
            stderr_acc.append(r.stderr)
            if r.returncode != 0:
                return False, "\n".join(stderr_acc)
            # Find the resulting binary
            candidates = list(build_dir.glob("*"))
            bins = [f for f in candidates if f.is_file() and os.access(f, os.X_OK)
                    and not f.suffix]
            if not bins:
                return False, "cmake build succeeded but no binary found"
            shutil.copy2(bins[0], out)

        elif build_system == "make":
            r = subprocess.run(
                ["make", "-C", str(original_dir)],
                capture_output=True, text=True, env=env
            )
            stderr_acc.append(r.stderr)
            if r.returncode != 0:
                return False, "\n".join(stderr_acc)
            # Try to locate binary in original_dir
            import os as _os
            candidates = [f for f in original_dir.iterdir()
                          if f.is_file() and _os.access(f, _os.X_OK) and not f.suffix]
            if not candidates:
                # Look for compiled output by Makefile convention
                candidates = list(original_dir.glob("*.out"))
            if candidates:
                shutil.copy2(candidates[0], out)
            else:
                return False, "make succeeded but no binary found"

        elif build_system == "autoconf":
            r = subprocess.run(
                ["./configure"], cwd=str(original_dir),
                capture_output=True, text=True, env=env
            )
            stderr_acc.append(r.stderr)
            if r.returncode != 0:
                return False, "\n".join(stderr_acc)
            r = subprocess.run(
                ["make"], cwd=str(original_dir),
                capture_output=True, text=True, env=env
            )
            stderr_acc.append(r.stderr)
            if r.returncode != 0:
                return False, "\n".join(stderr_acc)
            import os as _os
            candidates = [f for f in original_dir.iterdir()
                          if f.is_file() and _os.access(f, _os.X_OK) and not f.suffix]
            if candidates:
                shutil.copy2(candidates[0], out)
            else:
                return False, "autoconf build succeeded but no binary found"

        else:
            return False, f"Unsupported build system: {build_system}"

        return True, "\n".join(stderr_acc)

    except FileNotFoundError as e:
        return False, str(e)


def _get_rust_binary_name(src_dir: Path) -> str | None:
    """Read the binary name from Cargo.toml."""
    cargo_toml = src_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return None
    content = cargo_toml.read_text()
    # Look for [[bin]] name or package name
    m = re.search(r'^\s*\[\[bin\]\].*?name\s*=\s*"([^"]+)"', content,
                  re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r'^\s*\[package\].*?name\s*=\s*"([^"]+)"', content,
                  re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1)
    return None


def build_rust_binary(src_dir: Path, out: Path) -> tuple[bool, str]:
    """
    Build the current Rust workspace with `cargo build --release`.
    Returns (success: bool, stderr: str)
    On failure, prints structured error code summary.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(src_dir),
        capture_output=True, text=True
    )
    if r.returncode != 0:
        codes = re.findall(r"error\[E(\d+)\]", r.stderr)
        if codes:
            unique_codes = sorted(set(f"E{c}" for c in codes))
            print(f"Compile failed. Error codes: {', '.join(unique_codes)}", file=sys.stderr)
        else:
            print("Compile failed (no error codes extracted).", file=sys.stderr)
        return False, r.stderr

    # Locate the built binary
    release_dir = src_dir / "target" / "release"
    bin_name = _get_rust_binary_name(src_dir)
    if bin_name:
        candidate = release_dir / bin_name
        if candidate.exists():
            if candidate.resolve() != out.resolve():
                shutil.copy2(candidate, out)
            return True, r.stderr

    # Fallback: find any executable in release/
    import os as _os
    candidates = [
        f for f in release_dir.iterdir()
        if f.is_file() and _os.access(f, _os.X_OK)
        and not f.suffix and not f.name.startswith(".")
        and not f.name.startswith("build-script")
    ]
    if candidates:
        if candidates[0].resolve() != out.resolve():
            shutil.copy2(candidates[0], out)
        return True, r.stderr

    return False, "cargo build --release succeeded but binary not found in target/release/"


# ---------------------------------------------------------------------------
# 3. Differential Test Runner
# ---------------------------------------------------------------------------

def run_differential(c_bin: Path, rust_bin: Path, corpus_dir: Path) -> float:
    """
    Run both binaries on every input in corpus_dir and compare outputs.
    Returns pass_rate ∈ [0.0, 1.0]
    """
    input_files = sorted(corpus_dir.glob("input_*.bin"))
    if not input_files:
        print("WARNING: corpus is empty — pass_rate is undefined, returning 0.0",
              file=sys.stderr)
        return 0.0

    expected_dir = corpus_dir / "expected"
    use_stored = expected_dir.exists() and any(expected_dir.iterdir())

    passed = 0
    counted = 0

    for inp in input_files[:MAX_CORPUS_INPUTS]:
        inp_bytes = inp.read_bytes()

        if use_stored:
            # Compare Rust output against stored expected output
            out_name = "output_" + inp.name[len("input_"):]
            exp_file = expected_dir / out_name
            if not exp_file.exists():
                continue
            exp_stdout = exp_file.read_bytes()
            exp_exit_code_file = expected_dir / (out_name + ".exit")
            exp_exit = 0
            if exp_exit_code_file.exists():
                try:
                    exp_exit = int(exp_exit_code_file.read_text().strip())
                except ValueError:
                    exp_exit = 0

            try:
                rust_r = subprocess.run(
                    [str(rust_bin)], input=inp_bytes,
                    capture_output=True, timeout=TIMEOUT_PER_INPUT
                )
            except subprocess.TimeoutExpired:
                counted += 1
                continue
            except OSError:
                counted += 1
                continue

            counted += 1
            if rust_r.stdout == exp_stdout and rust_r.returncode == exp_exit:
                passed += 1

        else:
            # Live comparison: run C and Rust simultaneously
            try:
                c_r = subprocess.run(
                    [str(c_bin)], input=inp_bytes,
                    capture_output=True, timeout=TIMEOUT_PER_INPUT
                )
            except subprocess.TimeoutExpired:
                continue  # skip — C timed out
            except OSError:
                continue

            # Skip C crashes (negative return codes are signals on Unix)
            if c_r.returncode < 0:
                continue

            try:
                rust_r = subprocess.run(
                    [str(rust_bin)], input=inp_bytes,
                    capture_output=True, timeout=TIMEOUT_PER_INPUT
                )
            except subprocess.TimeoutExpired:
                counted += 1
                continue
            except OSError:
                counted += 1
                continue

            counted += 1
            if rust_r.stdout == c_r.stdout and rust_r.returncode == c_r.returncode:
                passed += 1

    if counted == 0:
        return 0.0
    return passed / counted


# ---------------------------------------------------------------------------
# 4. Miri Runner (secondary signal, informational)
# ---------------------------------------------------------------------------

def run_miri(src_dir: Path) -> int:
    """
    Run `cargo +nightly miri test` in src_dir.
    Returns count of UB errors found (0 = clean, -1 = miri not available).
    """
    if not shutil.which("cargo"):
        return -1

    try:
        r = subprocess.run(
            ["cargo", "+nightly", "miri", "test", "--lib"],
            cwd=str(src_dir),
            capture_output=True, text=True,
            timeout=60
        )
        combined = r.stdout + r.stderr
        # Miri reports "error[E..." or "error: " lines for UB
        error_count = len(re.findall(r"^error", combined, re.MULTILINE))
        return error_count
    except subprocess.TimeoutExpired:
        print("Miri timed out after 60s", file=sys.stderr)
        return -1
    except FileNotFoundError:
        return -1


# ---------------------------------------------------------------------------
# 5. Main Entry Point
# ---------------------------------------------------------------------------

def main():
    """
    Main oracle entry point. Called as `uv run oracle.py [flags]`.
    """
    parser = argparse.ArgumentParser(description="AutoReCoder oracle evaluation engine")
    parser.add_argument("--list-targets", action="store_true",
                        help="Print ranked JSON list of unsafe functions and exit")
    parser.add_argument("--unsafe-only", action="store_true",
                        help="Skip build and differential test; only count unsafe blocks")
    args = parser.parse_args()

    start_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # --- Unsafe-only fast path ---
    if args.unsafe_only:
        if not SRC_DIR.exists():
            print(f"ERROR: {SRC_DIR} does not exist", file=sys.stderr)
            sys.exit(1)
        unsafe_count = count_unsafe(SRC_DIR)
        unsafe_fns = list_unsafe_functions(SRC_DIR)
        fn_names = [f["name"] for f in unsafe_fns[:20]]
        print(f"unsafe_count: {unsafe_count}")
        print(f"unsafe_fns: {json.dumps(fn_names)}")
        peak_mb = _peak_memory_mb(start_mem)
        print(f"peak_memory_mb: {peak_mb}")
        return

    # --- List-targets fast path ---
    if args.list_targets:
        if not SRC_DIR.exists():
            print(f"ERROR: {SRC_DIR} does not exist", file=sys.stderr)
            sys.exit(1)
        targets = list_unsafe_functions(SRC_DIR)
        print(json.dumps(targets, indent=2))
        return

    # --- Full evaluation ---
    compile_status = "ok"
    pass_rate = 0.0
    miri_errors = 0

    # Count unsafe (always)
    if not SRC_DIR.exists():
        print(f"ERROR: {SRC_DIR} does not exist", file=sys.stderr)
        sys.exit(1)
    unsafe_count = count_unsafe(SRC_DIR)
    unsafe_fns = list_unsafe_functions(SRC_DIR)
    fn_names = [f["name"] for f in unsafe_fns[:20]]

    # Build Rust binary
    ok, stderr = build_rust_binary(SRC_DIR, RUST_BINARY)
    if not ok:
        compile_status = "fail"
        print(f"pass_rate: 0.000")
        print(f"unsafe_count: {unsafe_count}")
        print(f"unsafe_fns: {json.dumps(fn_names)}")
        print(f"compile_status: fail")
        print(f"miri_errors: -1")
        peak_mb = _peak_memory_mb(start_mem)
        print(f"peak_memory_mb: {peak_mb}")
        sys.exit(0)  # exit 0 — oracle ran fine, compile just failed

    # Run differential if corpus exists
    if CORPUS_DIR.exists() and any(CORPUS_DIR.glob("input_*.bin")):
        # Build C binary only if not already built or stale
        if not C_BINARY.exists():
            if ORIGINAL_DIR.exists():
                c_ok, c_err = build_c_binary(ORIGINAL_DIR, C_BINARY)
                if not c_ok:
                    print(f"WARNING: C binary build failed: {c_err[:200]}", file=sys.stderr)
                    print(f"WARNING: Running differential against stored expected outputs only",
                          file=sys.stderr)
            else:
                print(f"WARNING: {ORIGINAL_DIR} not found; using stored expected outputs",
                      file=sys.stderr)
        pass_rate = run_differential(C_BINARY, RUST_BINARY, CORPUS_DIR)
    else:
        print("WARNING: corpus not found — skipping differential test", file=sys.stderr)
        pass_rate = -1.0  # sentinel: no corpus

    # Run Miri (informational)
    miri_errors = run_miri(SRC_DIR)

    # --- Output (stable format, parsed by agent) ---
    if pass_rate < 0:
        print(f"pass_rate: N/A")
    else:
        print(f"pass_rate: {pass_rate:.3f}")
    print(f"unsafe_count: {unsafe_count}")
    print(f"unsafe_fns: {json.dumps(fn_names)}")
    print(f"compile_status: {compile_status}")
    print(f"miri_errors: {miri_errors}")
    peak_mb = _peak_memory_mb(start_mem)
    print(f"peak_memory_mb: {peak_mb}")


def _peak_memory_mb(start_mem: int) -> int:
    """Return peak RSS in MB."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is in bytes on Linux, kilobytes on macOS
    import platform
    if platform.system() == "Darwin":
        return max(0, (usage - start_mem)) // (1024 * 1024)
    else:
        return max(0, (usage - start_mem)) // 1024


if __name__ == "__main__":
    main()
