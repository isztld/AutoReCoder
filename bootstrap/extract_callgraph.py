"""
bootstrap/extract_callgraph.py — Standalone call graph extractor

Can be run independently to refresh callgraph.json without re-running bootstrap.

Usage:
    uv run bootstrap/extract_callgraph.py [--src workspace/src] [--out workspace/callgraph.json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from bootstrap.bootstrap import extract_callgraph


def main():
    parser = argparse.ArgumentParser(description="Extract Rust call graph")
    parser.add_argument("--src", type=Path, default=ROOT / "workspace" / "src")
    parser.add_argument("--out", type=Path, default=ROOT / "workspace" / "callgraph.json")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"ERROR: {args.src} does not exist", file=sys.stderr)
        sys.exit(1)

    cg = extract_callgraph(args.src, args.out)
    n_fns = len(cg["functions"])
    n_leaves = len(cg["leaves"])
    print(f"[OK] Call graph: {n_fns} functions, {n_leaves} leaves → {args.out}")


if __name__ == "__main__":
    main()
