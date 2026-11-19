"""Count or clear email payloads from terminal conversion jobs."""
import argparse
import asyncio

import asyncpg

from app.config import get_settings

_COUNT_SQL = """
    SELECT COUNT(*)
    FROM conversions
    WHERE status IN ('completed', 'failed', 'rejected')
      AND email_payload <> '{}'::jsonb
"""

_UPDATE_SQL = """
    UPDATE conversions
    SET email_payload = '{}'::jsonb
    WHERE status IN ('completed', 'failed', 'rejected')
      AND email_payload <> '{}'::jsonb
"""


async def run_cleanup(execute: bool = False) -> int:
    connection = await asyncpg.connect(get_settings().database_url)
    try:
        count = await connection.fetchval(_COUNT_SQL)
        print(f"Terminal jobs with retained email_payload: {count}")
        if execute:
            await connection.execute(_UPDATE_SQL)
            print(f"Cleared email_payload for {count} terminal jobs")
        else:
            print("Dry run only; no rows changed")
        return count
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Count matching rows without changes (default)")
    mode.add_argument("--execute", action="store_true", help="Clear payloads for matching terminal jobs")
    args = parser.parse_args()
    asyncio.run(run_cleanup(execute=args.execute))


if __name__ == "__main__":
    main()
