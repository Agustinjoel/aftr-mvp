from __future__ import annotations

from pathlib import Path

TEMPLATE = Path("scripts/assets/ui_clean.py")
TARGET = Path("app/ui.py")


def main() -> int:
    if not TEMPLATE.exists():
        print(f"ERROR: template not found: {TEMPLATE}")
        return 1

    TARGET.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    # quick syntax guard
    code = TARGET.read_text(encoding="utf-8")
    compile(code, str(TARGET), "exec")

    print(f"OK: rewrote {TARGET} from {TEMPLATE} and syntax is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())