"""
Генерация памятного коллажа из фото, которые игрок присылал по ходу
квеста (см. photo_slot у соответствующих beat'ов в content.json).

Шаблон assets/collage_template.png — картинка с тремя "рамками"
(имитация полароидных карточек), в каждую из которых нужно вписать
одно из присланных фото с учётом перспективы и поворота рамки.

Если какое-то фото не было прислано (игрок нажал "Пропустить") — та
рамка просто остаётся с исходной картинкой-заглушкой из шаблона,
коллаж всё равно собирается и отправляется.

Специально реализовано только на Pillow, без OpenCV/NumPy — на многих
недорогих/бесплатных хостингах (например, FreeBSD-хостинги вроде
ct8.pl/serv00.com) для OpenCV просто нет готовых бинарных пакетов, а
собрать его из исходников без root-доступа практически нереально.
Pillow гораздо легче собирается из исходников (или уже стоит), плюс
он и так уже используется в проекте для сертификата (certificate.py).
"""

import io

from PIL import Image, ImageDraw

COLLAGE_TEMPLATE_PATH = "assets/collage_template.png"

# Координаты подобраны вручную/полуавтоматически под конкретный шаблон
# 1536x1024 (см. эксперименты в разработке). Каждая рамка задана 4
# точками в порядке [top-left, top-right, bottom-right, bottom-left].
# "boatman" — первое фото по ходу квеста (бронзовый дуэт), "tower" —
# второе (селфи у башни), "final" — третье (у лавочек).
FRAME_QUADS = {
    "boatman": [(819, 85), (1341, 23), (1373, 290), (852, 352)],
    "tower": [(1147, 300), (1554, 327), (1534, 636), (1127, 609)],
    "final": [(952, 581), (1474, 682), (1411, 1001), (891, 899)],
}


def _order_points(pts):
    """Раскладывает 4 произвольно упорядоченные точки в
    [top-left, top-right, bottom-right, bottom-left]."""
    pts = list(pts)
    s = [x + y for x, y in pts]
    d = [x - y for x, y in pts]
    tl = pts[s.index(min(s))]
    br = pts[s.index(max(s))]
    tr = pts[d.index(max(d))]
    bl = pts[d.index(min(d))]
    return [tl, tr, br, bl]


def _solve_linear_system(a, b):
    """Простое решение системы Ax=b методом Гаусса с выбором ведущего
    элемента. a — список списков (NxN), b — список (N). Без numpy,
    чтобы не тянуть лишнюю бинарную зависимость."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot_row][col]) < 1e-12:
            raise ValueError("Вырожденная система координат рамки коллажа")
        m[col], m[pivot_row] = m[pivot_row], m[col]
        pivot = m[col][col]
        m[col] = [v / pivot for v in m[col]]
        for r in range(n):
            if r != col:
                factor = m[r][col]
                m[r] = [v - factor * m[col][i] for i, v in enumerate(m[r])]
    return [row[-1] for row in m]


def _find_perspective_coeffs(dst_quad, src_rect):
    """Коэффициенты для PIL Image.transform(..., Image.PERSPECTIVE, ...):
    dst_quad — 4 точки на итоговом холсте (куда должны попасть углы фото),
    src_rect — 4 угла исходного фото (0,0),(w,0),(w,h),(0,h) в том же
    порядке. Возвращает 8 коэффициентов a..h."""
    a_matrix = []
    b_vector = []
    for (x, y), (sx, sy) in zip(dst_quad, src_rect):
        a_matrix.append([x, y, 1, 0, 0, 0, -sx * x, -sx * y])
        b_vector.append(sx)
        a_matrix.append([0, 0, 0, x, y, 1, -sy * x, -sy * y])
        b_vector.append(sy)
    return _solve_linear_system(a_matrix, b_vector)


def _cover_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Обрезает изображение по центру так, чтобы его пропорции совпали
    с target_w/target_h (аналог CSS object-fit: cover), затем
    масштабирует к точному размеру."""
    w, h = img.size
    target_ratio = target_w / target_h
    src_ratio = w / h
    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x0 = (w - new_w) // 2
        img = img.crop((x0, 0, x0 + new_w, h))
    else:
        new_h = int(w / target_ratio)
        y0 = (h - new_h) // 2
        img = img.crop((0, y0, w, y0 + new_h))
    return img.resize((target_w, target_h), Image.LANCZOS)


def _paste_photo_into_quad(template: Image.Image, photo: Image.Image, quad_pts) -> Image.Image:
    dst = _order_points(quad_pts)
    tl, tr, br, bl = dst

    def dist(p1, p2):
        return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5

    target_w = max(1, round((dist(tl, tr) + dist(bl, br)) / 2))
    target_h = max(1, round((dist(tl, bl) + dist(tr, br)) / 2))

    fitted = _cover_crop(photo.convert("RGB"), target_w, target_h)
    src_rect = [(0, 0), (target_w - 1, 0), (target_w - 1, target_h - 1), (0, target_h - 1)]

    coeffs = _find_perspective_coeffs(dst, src_rect)

    canvas_size = template.size
    warped = fitted.transform(canvas_size, Image.PERSPECTIVE, coeffs, Image.BICUBIC)

    mask = Image.new("L", canvas_size, 0)
    ImageDraw.Draw(mask).polygon([tuple(p) for p in dst], fill=255)

    result = template.copy()
    result.paste(warped, (0, 0), mask)
    return result


def generate_collage_png(photos: dict) -> bytes:
    """photos: {slot_name: photo_bytes}. Отсутствующие слоты просто
    пропускаются (рамка остаётся с картинкой-заглушкой шаблона)."""
    template = Image.open(COLLAGE_TEMPLATE_PATH).convert("RGB")

    result = template
    for slot, quad in FRAME_QUADS.items():
        photo_bytes = photos.get(slot)
        if not photo_bytes:
            continue
        try:
            photo = Image.open(io.BytesIO(photo_bytes))
            photo.load()
        except Exception:
            continue
        result = _paste_photo_into_quad(result, photo, quad)

    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()
