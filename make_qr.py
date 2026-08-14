"""
Генерация QR-кодов для кодов доступа. Каждый QR ведёт на персональную
deep-link ссылку вида https://t.me/<bot_username>?start=<код> — при скане
Telegram сразу откроет чат с ботом и активирует именно этот код.

Использование:
    python make_qr.py sled_gomel_bot AB12-CD34
        -> создаст один файл qr_codes/AB12-CD34.png

    python make_qr.py sled_gomel_bot --file codes_output.txt
        -> создаст QR для каждого кода из файла (по одному коду в строке)

    python make_qr.py sled_gomel_bot --all
        -> создаст QR для ВСЕХ ещё не использованных кодов из базы данных
"""
import asyncio
import sys
import os

import qrcode

import storage

OUTPUT_DIR = "qr_codes"


def make_qr_file(bot_username: str, code: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    url = f"https://t.me/{bot_username}?start={code}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    path = os.path.join(OUTPUT_DIR, f"{code}.png")
    img.save(path)
    print(f"OK: {code} -> {path}  ({url})")


async def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    bot_username = sys.argv[1]
    arg2 = sys.argv[2]

    if arg2 == "--all":
        await storage.init_db()
        codes = await storage.list_codes(status="unused", limit=10000)
        if not codes:
            print("Нет неиспользованных кодов в базе. Сначала сгенерируйте их: python generate_codes.py N")
            return
        for row in codes:
            make_qr_file(bot_username, row["code"])
        print(f"\nГотово: {len(codes)} QR-кодов в папке {OUTPUT_DIR}/")

    elif arg2 == "--file":
        if len(sys.argv) < 4:
            print("Укажите путь к файлу со списком кодов")
            sys.exit(1)
        with open(sys.argv[3], encoding="utf-8") as f:
            codes = [line.strip() for line in f if line.strip()]
        for code in codes:
            make_qr_file(bot_username, code)
        print(f"\nГотово: {len(codes)} QR-кодов в папке {OUTPUT_DIR}/")

    else:
        # одиночный код передан прямо в аргументе
        make_qr_file(bot_username, arg2)


if __name__ == "__main__":
    asyncio.run(main())
