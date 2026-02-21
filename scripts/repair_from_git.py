from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

DEFAULT_FILES = [
    "app/ui.py",
    "app/main.py",
    "app/routes/picks.py",
    "data/providers/football_data.py",
    "daily/refresh.py",
    "scripts/verify_no_patch_markers.py",
]


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def ensure_git_repo() -> None:
    probe = run(["git", "rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise RuntimeError("No Git repository found in current directory.")


def ref_exists(ref: str) -> bool:
    return run(["git", "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def normalize_source_ref(ref: str) -> str:
    # Common typo/translation-safe aliases used in support chats.
    fixed = ref.strip().replace("origen/", "origin/")
    if fixed.endswith("/principal"):
        fixed = fixed[: -len("/principal")] + "/main"
    if fixed == "principal":
        return "main"
    return fixed


def detect_remote_default_ref() -> str | None:
    head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head.returncode == 0:
        candidate = head.stdout.strip()
        if candidate and ref_exists(candidate):
            return candidate
    return None


def maybe_fetch_remote() -> None:
    run(["git", "fetch", "origin"])  # best effort only


def resolve_source(source_arg: str | None) -> str:
    if source_arg:
        source = normalize_source_ref(source_arg)
        if ref_exists(source):
            return source
        raise RuntimeError(f"Source ref not found: {source}")

    maybe_fetch_remote()
    preferred = [detect_remote_default_ref(), "origin/main", "origin/master", "main", "master", "HEAD"]
    for candidate in preferred:
        if candidate and ref_exists(candidate):
            return candidate
    return "HEAD"


def restore(paths: list[str], source: str) -> None:
    cmd = ["git", "restore", f"--source={source}", "--"] + paths
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or "git restore failed")


def compile_check(paths: list[str]) -> None:
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        p = Path(rel)
        if not p.exists():
            continue
        code = p.read_text(encoding="utf-8", errors="strict")
        compile(code, str(p), "exec")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore known files from git and syntax-check Python files to recover from copy/paste corruption."
    )
    parser.add_argument("files", nargs="*", help="Optional file list. Defaults to known fragile files.")
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "Git source ref for restore. Supports common aliases like 'origen/principal'. "
            "Default: origin/<default-branch> if available, then origin/main, origin/master, main, master, HEAD."
        ),
    )
    args = parser.parse_args()

    files = args.files or DEFAULT_FILES

    try:
        ensure_git_repo()
        source = resolve_source(args.source)
        restore(files, source)
        compile_check(files)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1

    print(f"OK: restored files from {source} and syntax-checked Python files:")
    for f in files:
        print(f" - {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())