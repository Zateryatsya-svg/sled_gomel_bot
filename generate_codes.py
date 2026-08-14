"""
Массовая генерация кодов доступа без необходимости запускать бота и
писать ему команды. Удобно, если нужно заранее подготовить, скажем,
50 кодов перед стартом продаж.

Использование:
    python generate_codes.py 20                  # сгенерировать 20 кодов
    python generate_codes.py 20 "партия для VK"   # с пометкой

Коды пишутся прямо в ту же базу quest_progress.db, которую использует бот,
и одновременно сохраняются в codes_output.txt для удобства копирования.
"""
import asyncio
import sys

import storage
from bot import generate_code


async def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("Использование: python generate_codes.py КОЛИЧЕСТВО [пометка]")
        sys.exit(1)

    n = int(sys.argv[1])
    note = sys.argv[2] if len(sys.argv) > 2 else None

    await storage.init_db()

    codes = []
    for _ in range(n):
        code = generate_code()
        await storage.create_code(code, note)
        codes.append(code)

    with open("codes_output.txt", "a", encoding="utf-8") as f:
        for c in codes:
            f.write(c + "\n")

    print(f"Сгенерировано {n} кодов, дописаны в codes_output.txt:\n")
    for c in codes:
        print(c)


if __name__ == "__main__":
    asyncio.run(main())
