"""Entry point for ``python -I -m governance.github_ci``."""

from __future__ import annotations

from governance.github_ci.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
