"""
tests/test_oracle.py — Unit tests for oracle.py

Run with: uv run pytest tests/
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

import oracle


# ---------------------------------------------------------------------------
# 1. Unsafe Counter Tests
# ---------------------------------------------------------------------------

def test_count_unsafe_empty_dir(tmp_path):
    assert oracle.count_unsafe(tmp_path) == 0


def test_count_unsafe_basic(tmp_path):
    (tmp_path / "foo.rs").write_text("""
fn safe_fn() {
    let x = 1;
}

unsafe fn danger() {
    let _ = 0;
}

fn uses_unsafe() {
    unsafe {
        let _ = 0;
    }
}
""")
    count = oracle.count_unsafe(tmp_path)
    # Should count: `unsafe fn`, `unsafe {`
    assert count >= 2


def test_count_unsafe_ignores_comments(tmp_path):
    (tmp_path / "bar.rs").write_text("""
// This is not unsafe code
/* unsafe { } — still not real */
fn foo() {
    // unsafe fn bar() {}
    let x = 1;
}
""")
    count = oracle.count_unsafe(tmp_path)
    assert count == 0


def test_count_unsafe_ignores_strings(tmp_path):
    (tmp_path / "baz.rs").write_text("""
fn foo() {
    let s = "unsafe { }";
    let t = "unsafe fn bar";
}
""")
    count = oracle.count_unsafe(tmp_path)
    assert count == 0


def test_count_unsafe_real_code(tmp_path):
    (tmp_path / "real.rs").write_text("""
use std::ptr;

pub unsafe fn write_ptr(p: *mut u8, val: u8) {
    unsafe {
        ptr::write(p, val);
    }
}

fn safe_wrapper(slice: &mut [u8], idx: usize, val: u8) {
    slice[idx] = val;
}
""")
    count = oracle.count_unsafe(tmp_path)
    # unsafe fn + unsafe { — at least 2
    assert count >= 2


# ---------------------------------------------------------------------------
# 2. list_unsafe_functions Tests
# ---------------------------------------------------------------------------

def test_list_unsafe_functions_empty(tmp_path):
    result = oracle.list_unsafe_functions(tmp_path)
    assert result == []


def test_list_unsafe_functions_finds_unsafe(tmp_path):
    (tmp_path / "lib.rs").write_text("""
pub unsafe fn dangerous_alloc(n: usize) -> *mut u8 {
    unsafe {
        std::alloc::alloc(std::alloc::Layout::array::<u8>(n).unwrap())
    }
}

fn safe_fn() {}
""")
    result = oracle.list_unsafe_functions(tmp_path)
    assert len(result) > 0
    names = [r["name"] for r in result]
    assert "dangerous_alloc" in names


# ---------------------------------------------------------------------------
# 3. Differential Test Runner Tests
# ---------------------------------------------------------------------------

def _write_corpus(corpus_dir: Path, inputs: list[tuple[bytes, bytes, int]]):
    """Write (input, expected_output, exit_code) to corpus_dir."""
    expected_dir = corpus_dir / "expected"
    expected_dir.mkdir(parents=True, exist_ok=True)
    for i, (inp, out, code) in enumerate(inputs):
        (corpus_dir / f"input_{i:04d}.bin").write_bytes(inp)
        (expected_dir / f"output_{i:04d}.bin").write_bytes(out)
        (expected_dir / f"output_{i:04d}.bin.exit").write_text(str(code))


def _echo_binary(tmp_path: Path) -> Path:
    """Build a small binary that echoes stdin to stdout."""
    src = tmp_path / "echo.c"
    src.write_text("""
#include <stdio.h>
int main(void) {
    int c;
    while ((c = fgetc(stdin)) != EOF) fputc(c, stdout);
    return 0;
}
""")
    out = tmp_path / "echo_bin"
    r = subprocess.run(["clang", "-O0", "-o", str(out), str(src)],
                       capture_output=True)
    assert r.returncode == 0, f"Failed to compile echo binary: {r.stderr.decode()}"
    return out


def test_run_differential_perfect_match(tmp_path):
    """Two identical echo binaries → pass_rate 1.0"""
    echo = _echo_binary(tmp_path)
    corpus = tmp_path / "corpus"
    test_data = [(b"hello", b"hello", 0), (b"world", b"world", 0)]
    _write_corpus(corpus, test_data)

    rate = oracle.run_differential(echo, echo, corpus)
    assert rate == 1.0


def test_run_differential_empty_corpus(tmp_path):
    """Empty corpus → 0.0 (no inputs)"""
    echo = _echo_binary(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    rate = oracle.run_differential(echo, echo, corpus)
    assert rate == 0.0


def test_run_differential_mismatch(tmp_path):
    """A binary that always outputs 'X' vs expected 'hello' → rate < 1.0"""
    # Build a binary that outputs 'X' regardless of input
    src = tmp_path / "x_bin.c"
    src.write_text("""
#include <stdio.h>
int main(void) { fputc('X', stdout); return 0; }
""")
    x_bin = tmp_path / "x_bin"
    subprocess.run(["clang", "-O0", "-o", str(x_bin), str(src)], check=True)

    echo = _echo_binary(tmp_path)
    corpus = tmp_path / "corpus"
    _write_corpus(corpus, [(b"hello", b"hello", 0)])  # expected is "hello"

    rate = oracle.run_differential(echo, x_bin, corpus)
    assert rate < 1.0


# ---------------------------------------------------------------------------
# 4. Oracle Main Entry Point Tests (subprocess)
# ---------------------------------------------------------------------------

def test_oracle_unsafe_only_flag():
    """--unsafe-only should print unsafe_count line and exit 0"""
    r = subprocess.run(
        ["uv", "run", "oracle.py", "--unsafe-only"],
        capture_output=True, text=True,
        cwd="/Users/isztld/Downloads/autorecoder"
    )
    assert r.returncode == 0
    assert "unsafe_count:" in r.stdout
    assert "unsafe_fns:" in r.stdout


def test_oracle_list_targets_flag():
    """--list-targets should output valid JSON"""
    import json
    r = subprocess.run(
        ["uv", "run", "oracle.py", "--list-targets"],
        capture_output=True, text=True,
        cwd="/Users/isztld/Downloads/autorecoder"
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert isinstance(data, list)
