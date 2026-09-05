"""Генерация именного сертификата о прохождении квеста.

Берёт шаблон assets/certificate_template.png и вписывает в него имя/фамилию
игрока (шрифт assets/certificate_font.ttf — кириллический, едет вместе с
репозиторием, чтобы не зависеть от шрифтов, установленных на сервере).
"""

import io

from PIL import Image, ImageDraw, ImageFont

CERTIFICATE_TEMPLATE_PATH = "assets/certificate_template.png"
CERTIFICATE_FONT_PATH = "assets/certificate_font.ttf"

# Координаты подобраны под новый шаблон (1536x1024) — область между
# "Настоящим подтверждается, что" и линией-разделителем перед "прошёл(ла)
# квест-прогулку по...".
NAME_LINE_Y = 514  # y линии-разделителя, имя ставим прямо над ней
NAME_BOTTOM_MARGIN = 12  # отступ снизу от текста имени до линии
NAME_AREA_X_START = 705
NAME_AREA_X_END = 1445
NAME_MAX_WIDTH_RATIO = 0.92  # не шире 92% выделенной под имя области
NAME_FONT_MAX_SIZE = 55
NAME_FONT_MIN_SIZE = 24
NAME_COLOR = (31, 21, 15)


def generate_certificate_png(player_name: str) -> bytes:
    """Возвращает готовый PNG (в виде байтов) с вписанным именем игрока."""
    img = Image.open(CERTIFICATE_TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)
    area_width = NAME_AREA_X_END - NAME_AREA_X_START
    max_text_width = area_width * NAME_MAX_WIDTH_RATIO

    size = NAME_FONT_MAX_SIZE
    font = ImageFont.truetype(CERTIFICATE_FONT_PATH, size)
    bbox = draw.textbbox((0, 0), player_name, font=font)
    text_width = bbox[2] - bbox[0]
    while text_width > max_text_width and size > NAME_FONT_MIN_SIZE:
        size -= 4
        font = ImageFont.truetype(CERTIFICATE_FONT_PATH, size)
        bbox = draw.textbbox((0, 0), player_name, font=font)
        text_width = bbox[2] - bbox[0]

    text_height = bbox[3] - bbox[1]
    x = NAME_AREA_X_START + (area_width - text_width) / 2 - bbox[0]
    y = NAME_LINE_Y - NAME_BOTTOM_MARGIN - text_height - bbox[1]
    draw.text((x, y), player_name, font=font, fill=NAME_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
