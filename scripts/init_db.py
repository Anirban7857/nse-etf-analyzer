from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.db import get_database_url, init_database


def main() -> None:
    init_database()
    print(f"Database schema is ready for {get_database_url()}")


if __name__ == "__main__":
    main()
