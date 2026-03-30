"""
bootstrap/bootstrap.py — AutoReCoder One-Time Setup

Run once per new C codebase to migrate. Produces:
  1. workspace/src/         — c2rust translation (fully unsafe, but working)
  2. workspace/corpus/      — locked oracle corpus (never touched again)
  3. workspace/callgraph.json — function dependency graph for target ranking

Usage:
    uv run bootstrap/bootstrap.py [--src workspace/original] [--n-corpus 500]

After successful bootstrap:
    git add workspace/src/ workspace/corpus/ workspace/callgraph.json
    git commit -m "bootstrap: c2rust baseline + oracle corpus"
    git tag baseline

Output:
    [OK] Build system: cmake
    [OK] compile_commands.json generated
    [OK] c2rust translation complete → workspace/src/ (1,247 unsafe blocks)
    [OK] Baseline pass_rate: 1.000
    [OK] Corpus generated: 500 inputs → workspace/corpus/
    [OK] Call graph extracted: 143 functions, 37 leaves
    [OK] Bootstrap complete. Starting unsafe_count: 1247
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
WORKSPACE = ROOT / "workspace"
VENDOR_C2RUST = ROOT / "vendor" / "c2rust" / "target" / "release" / "c2rust"

# Import oracle for shared helpers
sys.path.insert(0, str(ROOT))
import oracle as _oracle


def ok(msg: str) -> None:
    print(f"[OK] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[ERR] {msg}", file=sys.stderr, flush=True)


class BootstrapError(Exception):
    pass


class UnsupportedBuildSystem(BootstrapError):
    pass


# ---------------------------------------------------------------------------
# 2.1 — Build System Detection
# ---------------------------------------------------------------------------

def detect_build_system(src_dir: Path) -> str:
    """
    Detect the build system used in src_dir.
    Returns one of: "cmake", "make", "autoconf", "meson"
    Raises UnsupportedBuildSystem if none detected.
    """
    if (src_dir / "CMakeLists.txt").exists():
        return "cmake"
    if (src_dir / "meson.build").exists():
        return "meson"
    if (src_dir / "configure").exists():
        return "autoconf"
    if (src_dir / "Makefile").exists():
        return "make"
    raise UnsupportedBuildSystem(
        f"No recognised build system in {src_dir}. "
        f"Expected: CMakeLists.txt, meson.build, configure, or Makefile."
    )


# ---------------------------------------------------------------------------
# 2.2 — Compile Commands Extraction
# ---------------------------------------------------------------------------

def extract_compile_commands(src_dir: Path, build_system: str, build_dir: Path) -> Path:
    """
    Generate compile_commands.json required by c2rust transpile.
    Returns: path to compile_commands.json
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    if build_system == "cmake":
        if not shutil.which("cmake"):
            raise BootstrapError("cmake not found — install with: brew install cmake")
        r = subprocess.run(
            ["cmake", "-S", str(src_dir), "-B", str(build_dir),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
             "-DCMAKE_BUILD_TYPE=Debug"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            raise BootstrapError(f"cmake configure failed:\n{r.stderr}")
        r = subprocess.run(
            ["cmake", "--build", str(build_dir)],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            # Non-fatal: compile_commands.json may still be generated even if build fails
            print(f"  (cmake build warnings/errors — continuing)\n{r.stderr[:500]}",
                  file=sys.stderr)
        cc = build_dir / "compile_commands.json"
        if not cc.exists():
            raise BootstrapError(
                f"compile_commands.json not found at {cc} after cmake run"
            )
        return cc

    elif build_system == "meson":
        if not shutil.which("meson"):
            raise BootstrapError("meson not found — install with: brew install meson")
        r = subprocess.run(
            ["meson", "setup", str(build_dir), str(src_dir)],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            raise BootstrapError(f"meson setup failed:\n{r.stderr}")
        cc = build_dir / "compile_commands.json"
        if not cc.exists():
            raise BootstrapError("meson did not generate compile_commands.json")
        return cc

    elif build_system in ("make", "autoconf"):
        if not shutil.which("bear"):
            raise BootstrapError(
                "bear not found — install with: brew install bear\n"
                "bear is required to extract compile_commands.json from Makefile projects."
            )
        if build_system == "autoconf":
            r = subprocess.run(
                ["./configure"], cwd=str(src_dir),
                capture_output=True, text=True
            )
            if r.returncode != 0:
                raise BootstrapError(f"./configure failed:\n{r.stderr}")

        # Clean first so bear can intercept a fresh build
        subprocess.run(["make", "clean"], cwd=str(src_dir),
                       capture_output=True, text=True)

        r = subprocess.run(
            ["bear", "--", "make"],
            cwd=str(src_dir),
            capture_output=True, text=True
        )
        if r.returncode != 0:
            raise BootstrapError(f"bear -- make failed:\n{r.stderr}")
        cc = src_dir / "compile_commands.json"
        if not cc.exists():
            raise BootstrapError(f"bear did not generate compile_commands.json in {src_dir}")
        # Copy to build_dir for consistency
        dest = build_dir / "compile_commands.json"
        shutil.copy2(cc, dest)
        return dest

    else:
        raise UnsupportedBuildSystem(f"Unknown build system: {build_system}")


# ---------------------------------------------------------------------------
# 2.3 — c2rust Invocation
# ---------------------------------------------------------------------------

def run_c2rust(compile_commands: Path, out_dir: Path) -> bool:
    """
    Invoke c2rust to transpile the C codebase to Rust.
    Returns True on success, False on failure.
    """
    if not VENDOR_C2RUST.exists():
        raise BootstrapError(
            f"c2rust binary not found at {VENDOR_C2RUST}\n"
            f"Build it with:\n"
            f"  cd vendor/c2rust\n"
            f"  LLVM_CONFIG_PATH=/opt/homebrew/opt/llvm/bin/llvm-config cargo build --release\n"
            f"  cd ../.."
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(
        [str(VENDOR_C2RUST), "transpile", str(compile_commands),
         "--output-dir", str(out_dir)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        err(f"c2rust failed:\n{r.stderr[:1000]}")
        return False

    # Verify output — c2rust 0.22+ generates source files but no Cargo.toml
    rs_files = list(out_dir.rglob("*.rs"))
    if not rs_files:
        err(f"c2rust ran but no .rs files found in {out_dir}")
        return False

    cargo_toml = out_dir / "Cargo.toml"
    if not cargo_toml.exists():
        # Generate Cargo.toml for the translated crate
        _generate_cargo_toml(out_dir, rs_files)

    unsafe_count = _oracle.count_unsafe(out_dir)
    ok(f"c2rust translation complete → {out_dir} ({unsafe_count} unsafe blocks)")
    return True


def _strip_crate_attrs(content: str) -> tuple[list[str], str]:
    """
    Extract crate-level inner attributes (#![...]) from a Rust source file.
    Returns (attrs, remaining_content).
    Crate-level attributes must not appear inside module files.
    """
    attrs = []
    lines = content.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("#!["):
            # Collect multi-line attribute
            attr_lines = [lines[i]]
            if stripped.endswith("]"):
                attrs.append(stripped)
                i += 1
                continue
            # Multi-line: collect until closing ]
            i += 1
            while i < len(lines):
                attr_lines.append(lines[i])
                if lines[i].strip().endswith("]"):
                    i += 1
                    break
                i += 1
            attrs.append("".join(attr_lines).strip())
        elif stripped == "" or stripped.startswith("//"):
            i += 1
        else:
            break
    remaining = "".join(lines[i:])
    return attrs, remaining


def _generate_cargo_toml(out_dir: Path, rs_files: list[Path]) -> None:
    """
    Generate a Cargo.toml for c2rust-translated source files.
    Creates a proper Rust crate structure:
    - main.rs with crate-level attributes + module declarations + main()
    - Each translated file as a separate named module
    """
    src_subdir = out_dir / "src"
    src_subdir.mkdir(exist_ok=True)

    # Ensure all rs_files are in src_subdir
    for f in rs_files:
        if f.parent != src_subdir:
            shutil.copy2(f, src_subdir / f.name)

    rs_in_src = sorted(src_subdir.glob("*.rs"))

    # Determine if binary (has main) or library
    main_file = next(
        (f for f in rs_in_src if "fn main()" in f.read_text(errors="replace")),
        None
    )
    has_main = main_file is not None

    if has_main:
        # Collect all unique crate-level attrs from all files
        all_attrs: list[str] = []
        file_bodies: dict[str, str] = {}
        for f in rs_in_src:
            content = f.read_text(errors="replace")
            attrs, body = _strip_crate_attrs(content)
            for a in attrs:
                if a not in all_attrs:
                    all_attrs.append(a)
            file_bodies[f.stem] = body

        # Build main.rs:
        # 1. Crate-level attrs
        # 2. mod declarations for non-main files
        # 3. Body of the main file (with its main() fn)
        non_main_mods = [f for f in rs_in_src if f != main_file]
        mod_decls = "\n".join(
            f"#[path = \"{f.name}\"]\nmod {f.stem};" for f in non_main_mods
        )
        # Rewrite each non-main .rs file to strip its crate-level attrs
        for f in non_main_mods:
            f.write_text(file_bodies[f.stem])

        main_rs = src_subdir / "main.rs"
        parts = []
        if all_attrs:
            parts.append("\n".join(all_attrs))
        if mod_decls:
            parts.append(mod_decls)
        parts.append(file_bodies[main_file.stem])
        main_rs.write_text("\n\n".join(parts) + "\n")

        # Remove the original driver.rs (replaced by main.rs)
        if main_file.name != "main.rs":
            main_file.unlink()

        toml_content = """[package]
name = "autorecoder_target"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "autorecoder_target"
path = "src/main.rs"
"""
    else:
        # Library crate — strip attrs from all files, create lib.rs
        all_attrs: list[str] = []
        file_bodies: dict[str, str] = {}
        for f in rs_in_src:
            content = f.read_text(errors="replace")
            attrs, body = _strip_crate_attrs(content)
            for a in attrs:
                if a not in all_attrs:
                    all_attrs.append(a)
            file_bodies[f.stem] = body
            f.write_text(body)

        lib_rs = src_subdir / "lib.rs"
        mod_decls = "\n".join(f"pub mod {stem};" for stem in file_bodies)
        lib_content = "\n".join(all_attrs) + "\n\n" + mod_decls + "\n"
        lib_rs.write_text(lib_content)

        toml_content = """[package]
name = "autorecoder_target"
version = "0.1.0"
edition = "2021"

[lib]
name = "autorecoder_target"
path = "src/lib.rs"
"""

    (out_dir / "Cargo.toml").write_text(toml_content)

    # c2rust output requires nightly features (extern_types, etc.)
    toolchain_toml = out_dir / "rust-toolchain.toml"
    if not toolchain_toml.exists():
        toolchain_toml.write_text('[toolchain]\nchannel = "nightly"\n')


# ---------------------------------------------------------------------------
# 2.4 — Baseline Verification
# ---------------------------------------------------------------------------

def verify_baseline(workspace: Path, n_quick: int = 100) -> tuple[float, int]:
    """
    Verify the c2rust baseline compiles and passes a quick corpus check.
    Returns (pass_rate=1.0, unsafe_count: int)
    Raises BootstrapError if pass_rate < 1.0
    """
    src_dir = workspace / "src"
    original_dir = workspace / "original"
    c_bin = workspace / "build" / "c_target"
    rust_bin = src_dir / "target" / "release" / "autorecoder_target"

    # Build C binary
    c_ok, c_err = _oracle.build_c_binary(original_dir, c_bin)
    if not c_ok:
        raise BootstrapError(f"C binary build failed:\n{c_err}")
    ok("C binary built with sanitizers")

    # Build Rust binary
    rust_ok, rust_err = _oracle.build_rust_binary(src_dir, rust_bin)
    if not rust_ok:
        raise BootstrapError(f"Rust baseline build failed:\n{rust_err[:500]}")
    ok("Rust baseline binary built")

    # Generate n_quick random inputs in a temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        expected_dir = tmp_path / "expected"
        expected_dir.mkdir()
        valid_count = 0
        attempts = 0
        random.seed(42)
        while valid_count < n_quick and attempts < n_quick * 5:
            attempts += 1
            length = random.randint(0, 512)
            data = bytes(random.randint(0, 255) for _ in range(length))
            try:
                c_r = subprocess.run(
                    [str(c_bin)], input=data,
                    capture_output=True, timeout=_oracle.TIMEOUT_PER_INPUT
                )
            except subprocess.TimeoutExpired:
                continue
            if c_r.returncode < 0:  # signal/crash
                continue
            # Save input + expected output
            inp_file = tmp_path / f"input_{valid_count:04d}.bin"
            inp_file.write_bytes(data)
            out_file = expected_dir / f"output_{valid_count:04d}.bin"
            out_file.write_bytes(c_r.stdout)
            exit_file = expected_dir / f"output_{valid_count:04d}.bin.exit"
            exit_file.write_text(str(c_r.returncode))
            valid_count += 1

        if valid_count == 0:
            raise BootstrapError("Could not generate any valid inputs for baseline verification")

        pass_rate = _oracle.run_differential(c_bin, rust_bin, tmp_path)

    if pass_rate < 1.0:
        raise BootstrapError(
            f"Baseline pass_rate={pass_rate:.3f} < 1.0 — c2rust translation is broken. "
            f"Cannot proceed with migration."
        )
    ok(f"Baseline pass_rate: {pass_rate:.3f}")

    unsafe_count = _oracle.count_unsafe(src_dir)
    return pass_rate, unsafe_count


# ---------------------------------------------------------------------------
# 2.5 — Corpus Generation
# ---------------------------------------------------------------------------

def generate_corpus(c_bin: Path, corpus_dir: Path, n: int = 500) -> int:
    """
    Generate and lock the oracle corpus: n valid inputs with expected outputs.
    Returns count of valid inputs saved.
    """
    if corpus_dir.exists():
        existing = list(corpus_dir.glob("input_*.bin"))
        if existing:
            raise BootstrapError(
                f"corpus_dir {corpus_dir} already has {len(existing)} inputs — "
                f"bootstrap should only run once. Remove it manually to regenerate."
            )
        # Directory exists but no inputs — proceed (remove placeholder files)
        for f in corpus_dir.iterdir():
            if f.name == ".gitkeep":
                f.unlink()
    else:
        corpus_dir.mkdir(parents=True)
    expected_dir = corpus_dir / "expected"
    expected_dir.mkdir()

    use_radamsa = shutil.which("radamsa") is not None

    saved = 0
    attempts = 0
    random.seed(12345)

    # Seed inputs for radamsa mutation
    seeds = [
        b"",
        b"hello\n",
        b"\x00" * 16,
        b"A" * 64,
        bytes(range(256)),
        b"\xff\xfe" + b"\x00" * 100,
    ]

    def _random_input() -> bytes:
        # Exponential distribution favouring small inputs, up to 4KB
        size = min(int(random.expovariate(1 / 256)), 4096)
        return bytes(random.randint(0, 255) for _ in range(size))

    def _radamsa_input(seed_bytes: bytes) -> bytes:
        try:
            r = subprocess.run(
                ["radamsa"], input=seed_bytes,
                capture_output=True, timeout=5
            )
            return r.stdout if r.stdout else _random_input()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return _random_input()

    while saved < n and attempts < n * 20:
        attempts += 1

        if use_radamsa and random.random() < 0.7:
            seed = random.choice(seeds)
            data = _radamsa_input(seed)
        else:
            data = _random_input()

        try:
            c_r = subprocess.run(
                [str(c_bin)], input=data,
                capture_output=True, timeout=_oracle.TIMEOUT_PER_INPUT
            )
        except subprocess.TimeoutExpired:
            continue

        # Skip signal exits (crashes)
        if c_r.returncode < 0:
            continue

        # Save input + expected output
        inp_file = corpus_dir / f"input_{saved:04d}.bin"
        inp_file.write_bytes(data)
        out_file = expected_dir / f"output_{saved:04d}.bin"
        out_file.write_bytes(c_r.stdout)
        exit_file = expected_dir / f"output_{saved:04d}.bin.exit"
        exit_file.write_text(str(c_r.returncode))
        saved += 1

        if saved % 50 == 0:
            print(f"  ... {saved}/{n} corpus inputs generated", flush=True)

    return saved


# ---------------------------------------------------------------------------
# 2.6 — Call Graph Extraction
# ---------------------------------------------------------------------------

def extract_callgraph(src_dir: Path, out: Path) -> dict:
    """
    Extract function call graph from the Rust source in src_dir.
    Falls back to regex-based extraction if tree-sitter is unavailable.
    """
    functions: list[dict] = []
    fn_names_in_codebase: set[str] = set()

    # Regex-based extraction (reliable fallback)
    fn_def_re = re.compile(
        r"(?:pub\s+)?(?:(?:unsafe|async|const|extern(?:\s+\"[^\"]*\")?)\s+)*fn\s+(\w+)"
    )
    call_re = re.compile(r"\b(\w+)\s*\(")

    # First pass: collect all function names defined in this codebase
    for rs_file in sorted(src_dir.rglob("*.rs")):
        try:
            source = rs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in fn_def_re.finditer(source):
            fn_names_in_codebase.add(m.group(1))

    # Second pass: extract callees per function
    for rs_file in sorted(src_dir.rglob("*.rs")):
        try:
            source = rs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_path = str(rs_file.relative_to(src_dir))
        lines = source.splitlines()

        i = 0
        while i < len(lines):
            m = fn_def_re.search(lines[i])
            if m:
                fn_name = m.group(1)
                fn_line = i + 1
                # Collect body
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

                body = "\n".join(body_lines[1:])  # skip the fn declaration line
                callees = sorted(set(
                    cm.group(1) for cm in call_re.finditer(body)
                    if cm.group(1) in fn_names_in_codebase
                    and cm.group(1) != fn_name
                    and cm.group(1) not in {"if", "while", "for", "loop", "match",
                                            "Some", "None", "Ok", "Err", "vec", "assert",
                                            "panic", "println", "print", "format", "todo",
                                            "unimplemented", "unreachable"}
                ))
                functions.append({
                    "name": fn_name,
                    "file": rel_path,
                    "line": fn_line,
                    "callees": callees,
                    "is_leaf": len(callees) == 0,
                })
                i = j + 1
            else:
                i += 1

    leaves = [f["name"] for f in functions if f["is_leaf"]]
    result = {"functions": functions, "leaves": leaves}
    out.write_text(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# 2.6 — Main Entry Point
# ---------------------------------------------------------------------------

def main():
    """
    Bootstrap entry point.
    """
    parser = argparse.ArgumentParser(description="AutoReCoder bootstrap")
    parser.add_argument("--src", type=Path, default=WORKSPACE / "original",
                        help="Directory containing original C/C++ source")
    parser.add_argument("--n-corpus", type=int, default=500,
                        help="Number of corpus inputs to generate")
    args = parser.parse_args()

    src_dir = args.src.resolve()
    n_corpus = args.n_corpus

    print("=" * 60)
    print("AutoReCoder Bootstrap")
    print("=" * 60)

    # 1. Validate src_dir
    if not src_dir.exists():
        err(f"Source directory not found: {src_dir}")
        sys.exit(1)
    files = list(src_dir.iterdir())
    if not files:
        err(f"Source directory is empty: {src_dir}")
        sys.exit(1)

    # 2. Detect build system
    try:
        build_system = detect_build_system(src_dir)
    except UnsupportedBuildSystem as e:
        err(str(e))
        sys.exit(1)
    ok(f"Build system: {build_system}")

    # 3. Extract compile_commands.json
    build_dir = WORKSPACE / "build"
    try:
        compile_commands = extract_compile_commands(src_dir, build_system, build_dir)
    except BootstrapError as e:
        err(str(e))
        sys.exit(1)
    ok(f"compile_commands.json generated → {compile_commands}")

    # 4. Run c2rust → workspace/src/
    out_dir = WORKSPACE / "src"
    # Skip c2rust only if Cargo.toml already exists (real translation, not just gitkeep)
    has_cargo = (out_dir / "Cargo.toml").exists() or bool(list(out_dir.rglob("Cargo.toml")))
    if has_cargo:
        print(f"  workspace/src/ already has Cargo.toml — skipping c2rust (delete to re-run)")
    else:
        # Remove target/ dir if present so we start fresh
        try:
            success = run_c2rust(compile_commands, out_dir)
            if not success:
                sys.exit(1)
        except BootstrapError as e:
            err(str(e))
            sys.exit(1)

    # 5. Verify baseline
    try:
        pass_rate, unsafe_count = verify_baseline(WORKSPACE)
    except BootstrapError as e:
        err(str(e))
        sys.exit(1)
    ok(f"Baseline pass_rate: {pass_rate:.3f}")

    # 6. Generate full corpus → workspace/corpus/
    c_bin = WORKSPACE / "build" / "c_target"
    corpus_dir = WORKSPACE / "corpus"
    if corpus_dir.exists() and any(corpus_dir.glob("input_*.bin")):
        existing = len(list(corpus_dir.glob("input_*.bin")))
        print(f"  workspace/corpus/ already exists with {existing} inputs — skipping")
        saved = existing
    else:
        print(f"  Generating {n_corpus} corpus inputs...")
        try:
            saved = generate_corpus(c_bin, corpus_dir, n=n_corpus)
        except BootstrapError as e:
            err(str(e))
            sys.exit(1)
    ok(f"Corpus generated: {saved} inputs → {corpus_dir}")

    # 7. Extract call graph → workspace/callgraph.json
    cg_path = WORKSPACE / "callgraph.json"
    cg = extract_callgraph(WORKSPACE / "src", cg_path)
    n_fns = len(cg["functions"])
    n_leaves = len(cg["leaves"])
    ok(f"Call graph extracted: {n_fns} functions, {n_leaves} leaves → {cg_path}")

    # 8. Summary
    print()
    print("=" * 60)
    ok(f"Bootstrap complete. Starting unsafe_count: {unsafe_count}")
    print()
    print("Next steps:")
    print("  git add workspace/src/ workspace/corpus/ workspace/callgraph.json")
    print('  git commit -m "bootstrap: c2rust baseline + oracle corpus"')
    print("  git tag baseline")
    print("  uv run oracle.py   # verify oracle output")
    print("=" * 60)


if __name__ == "__main__":
    main()
