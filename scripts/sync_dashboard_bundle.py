from __future__ import annotations

import subprocess
from pathlib import Path

FILES = [
    "app/ui.py",
    "app/main.py",
    "app/routes/picks.py",
    "static/style.css",
    "data/providers/football_data.py",
    "daily/refresh.py",
]


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def ref_exists(ref: str) -> bool:
    return run(["git", "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def detect_source() -> str:
    # Keep latest local work by default; fallback to remote refs if needed.
    if ref_exists("HEAD"):
        return "HEAD"

    run(["git", "fetch", "origin"])  # best effort

    head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head.returncode == 0:
        candidate = head.stdout.strip()
        if candidate and ref_exists(candidate):
            return candidate

    for candidate in ["origin/main", "origin/master", "main", "master"]:
        if ref_exists(candidate):
            return candidate

    return "HEAD"


def restore_files(source: str) -> None:
    cmd = ["git", "restore", f"--source={source}", "--", *FILES]
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or "git restore failed")


def compile_check() -> None:
    for rel in FILES:
        if not rel.endswith(".py"):
            continue
        p = Path(rel)
        code = p.read_text(encoding="utf-8", errors="strict")
        compile(code, rel, "exec")


def main() -> int:
    try:
        source = detect_source()
        restore_files(source)
        compile_check()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1

    print("OK: restored dashboard bundle and syntax-checked Python files:")
    print(f"Source: {source}")
    for rel in FILES:
        print(f" - {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())