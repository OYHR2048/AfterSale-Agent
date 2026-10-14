"""Inspect every public file, not just Git's ignored list."""

import argparse
import hashlib
import json
import re
from pathlib import Path

FORBIDDEN_DIRS = {
    ".venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
    "models",
}
FORBIDDEN_SUFFIX = {".db", ".sqlite", ".pyc", ".pkl", ".safetensors", ".bin", ".whl"}


def audit(root):
    root = root.resolve()
    issues = []
    files = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        resolved = path.resolve()
        if path.is_symlink() or root not in resolved.parents:
            issues.append(rel + ": link/outside")
            continue
        if any(
            p in FORBIDDEN_DIRS or p.endswith(".egg-info") for p in path.relative_to(root).parts
        ):
            issues.append(rel + ": working directory")
        if path.is_dir():
            continue
        if (
            path.name == ".env"
            or path.suffix in FORBIDDEN_SUFFIX
            or path.name.startswith(("auth.", "business.db", "checkpoints.db"))
        ):
            issues.append(rel + ": runtime/sensitive artifact")
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            issues.append(rel + ": unexpected binary")
            continue
        if re.search(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\|/(?!/))", content):
            issues.append(rel + ": machine absolute path")
        if re.search(r"sk-[A-Za-z0-9_-]{20,}", content) or re.search(
            r"(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----$", content
        ):
            issues.append(rel + ": credential pattern")
        files.append({"path": rel, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return {"passed": not issues, "files_checked": len(files), "issues": issues, "files": files}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    report = audit(Path(__file__).resolve().parent.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
