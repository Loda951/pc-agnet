import argparse
import asyncio
import json
from pathlib import Path

from app.core.database import AsyncSessionLocal
from app.services.dataset_mapper import normalize_part_record
from scripts.seed_demo import _upsert_product


async def import_file(path: Path, part_type: str, limit: int | None) -> int:
    records = json.loads(path.read_text(encoding="utf-8"))
    imported = 0
    async with AsyncSessionLocal() as session:
        for record in records:
            product = normalize_part_record(part_type, record)
            if product is None:
                continue
            await _upsert_product(session, product)
            imported += 1
            if limit and imported >= limit:
                break
        await session.commit()
    return imported


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="Path to a pc-part-dataset JSON file")
    parser.add_argument(
        "--part-type", required=True, help="Dataset part type, e.g. mouse or keyboard"
    )
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    imported = await import_file(args.path, args.part_type, args.limit)
    print(f"Imported {imported} products from {args.path}")


if __name__ == "__main__":
    asyncio.run(main())
