"""Генерация именного сертификата о прохождении квеста.

Берёт шаблон assets/certificate_template.png и вписывает в него имя/фамилию
игрока (шрифт assets/certificate_font.ttf — кириллический, едет вместе с
репозиторием, чтобы не зависеть от шрифтов, установленных на сервере).
"""

import io

from PIL import Image, ImageDraw, ImageFont

CERTIFICATE_TEMPLATE_PATH = "assets/certificate_template.png"
CERTIFICATE_FONT_PATH = "assets/certificate_font.ttf"

# Координаты подобраны под конкретный шаблон (1748x1240) — область между
# "НАСТОЯЩИЙ СЕРТИФИКАТ С ГОРДОСТЬЮ ВРУЧАЕТСЯ" и декоративным разделителем.
NAME_Y = 560
NAME_MAX_WIDTH_RATIO = 0.75  # не шире 75% ширины сертификата
NAME_FONT_MAX_SIZE = 70
NAME_FONT_MIN_SIZE = 30
NAME_COLOR = (20, 20, 20)


def generate_certificate_png(player_name: str) -> bytes:
    """Возвращает готовый PNG (в виде байтов) с вписанным именем игрока."""
    img = Image.open(CERTIFICATE_TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)
    width, _ = img.size
    max_text_width = width * NAME_MAX_WIDTH_RATIO

    size = NAME_FONT_MAX_SIZE
    font = ImageFont.truetype(CERTIFICATE_FONT_PATH, size)
    bbox = draw.textbbox((0, 0), player_name, font=font)
    text_width = bbox[2] - bbox[0]
    while text_width > max_text_width and size > NAME_FONT_MIN_SIZE:
        size -= 4
        font = ImageFont.truetype(CERTIFICATE_FONT_PATH, size)
        bbox = draw.textbbox((0, 0), player_name, font=font)
        text_width = bbox[2] - bbox[0]

    x = (width - text_width) / 2
    y = NAME_Y + (NAME_FONT_MAX_SIZE - size) / 2  # держим вертикальный центр стабильным
    draw.text((x, y), player_name, font=font, fill=NAME_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
