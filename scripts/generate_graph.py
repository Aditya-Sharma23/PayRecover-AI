"""Generate a Graphify-compatible codebase graph from tracked source files."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"node_modules", "dist", ".venv", "__pycache__", ".git", "data", "vendor", ".npm-cache"}
EXTENSIONS = {".py", ".jsx", ".js", ".css", ".mjs", ".yml", ".yaml", ".json", ".md", ".html"}
SKIPPED_FILES = {"codebase-graph.json", "package-lock.json"}
JS_IMPORT = re.compile(r"""(?:import|from)\s+["'](\.{1,2}/[^"']+)["']""")


def included(path: Path) -> bool:
    return path.name not in SKIPPED_FILES and path.suffix.lower() in EXTENSIONS and not any(
        part in EXCLUDED for part in path.relative_to(ROOT).parts
    )


def python_edges(path: Path, files: list[Path]) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    targets = []
    for other in files:
        if other == path or other.suffix != ".py":
            continue
        module = other.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
        for prefix in ("backend.tests.", "backend.", "scripts."):
            module = module.removeprefix(prefix)
        if re.search(rf"^\s*(from {re.escape(module)} import|import {re.escape(module)}\b)", text, re.MULTILINE):
            targets.append(other.relative_to(ROOT).as_posix())
    return targets


def js_edges(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    targets = []
    for specifier in JS_IMPORT.findall(text):
        target = (path.parent / specifier).resolve()
        if target.is_file() and ROOT in target.parents:
            targets.append(target.relative_to(ROOT).as_posix())
    return targets


def main() -> None:
    files = sorted(path for path in ROOT.rglob("*") if path.is_file() and included(path))
    nodes = []
    edges = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        nodes.append({"id": relative, "label": path.name, "type": path.suffix.lstrip(".")})
        if path.suffix == ".py":
            targets = python_edges(path, files)
        elif path.suffix in {".js", ".jsx", ".mjs"}:
            targets = js_edges(path)
        else:
            targets = []
        edges.extend({"source": relative, "target": target, "type": "imports"} for target in targets)
    output = ROOT / "docs" / "codebase-graph.json"
    output.write_text(
        json.dumps(
            {
                "schema": "testpilot-codebase-graph/v1",
                "generated_by": "scripts/generate_graph.py",
                "root": ROOT.name,
                "nodes": nodes,
                "edges": edges,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Generated {output} with {len(nodes)} nodes and {len(edges)} edges")


if __name__ == "__main__":
    main()
