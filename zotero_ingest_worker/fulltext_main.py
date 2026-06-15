from __future__ import annotations

from .__main__ import main as _main
from .worker_roles import ROLE_FULLTEXT


def main(argv: list[str] | None = None) -> int:
    return _main(argv, default_role=ROLE_FULLTEXT)


if __name__ == "__main__":
    raise SystemExit(main())
