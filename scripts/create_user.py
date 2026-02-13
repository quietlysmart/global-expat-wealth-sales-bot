from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a login user.")
    parser.add_argument("--email", required=True, help="User email")
    parser.add_argument("--password", required=True, help="User password")
    args = parser.parse_args()

    settings = Settings()
    db = Database(Path(settings.database_path))
    db.init()
    db.ensure_user(args.email, args.password)
    print(f"User ready: {args.email.strip().lower()}")


if __name__ == "__main__":
    main()
