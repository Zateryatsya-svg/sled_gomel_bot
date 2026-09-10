"""
Генерация памятного коллажа из фото, которые игрок присылал по ходу
квеста (см. photo_slot у соответствующих beat'ов в content.json).

Шаблон assets/collage_template.png — картинка с тремя "рамками"
(имитация полароидных карточек), в каждую из которых нужно вписать
одно из присланных фото с учётом перспективы и поворота рамки.

Если какое-то фото не было прислано (игрок нажал "Пропустить") — та
рамка просто остаётся с исходной картинкой-заглушкой из шаблона,
коллаж всё равно собирается и отправляется.
"""

import io

import cv2
import numpy as np
from PIL import Image

COLLAGE_TEMPLATE_PATH = "assets/collage_template.png"

# Координаты подобраны вручную/полуавтоматически под конкретный шаблон
# 1536x1024 (см. эксперименты в разработке). Каждая рамка задана 4
# точками в любом порядке — order_points() сама разберётся, где какой
# угол. "boatman" — первое фото по ходу квеста (бронзовый дуэт),
# "tower" — второе (селфи у башни), "final" — третье (у лавочек).
FRAME_QUADS = {
    "boatman": [(819, 85), (1341, 23), (1373, 290), (852, 352)],
    "tower": [(1147, 300), (1554, 327), (1534, 636), (1127, 609)],
    "final": [(952, 581), (1474, 682), (1411, 1001), (891, 899)],
}


def _order_points(pts: np.ndarray) -> np.ndarray:
    """Раскладывает 4 произвольно упорядоченные точки в
    [top-left, top-right, bottom-right, bottom-left]."""
    pts = np.array(pts, dtype="float32")
    s = pts.sum(axis=1)
    d = (pts[:, 0] - pts[:, 1])
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmax(d)]
    bl = pts[np.argmin(d)]
    return np.array([tl, tr, br, bl], dtype="float32")


def _cover_crop(img: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Обрезает изображение по центру так, чтобы его пропорции совпали
    с target_w/target_h (аналог CSS object-fit: cover), затем
    масштабирует к точному размеру."""
    h, w = img.shape[:2]
    target_ratio = target_w / target_h
    src_ratio = w / h
    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x0 = (w - new_w) // 2
        img = img[:, x0:x0 + new_w]
    else:
        new_h = int(w / target_ratio)
        y0 = (h - new_h) // 2
        img = img[y0:y0 + new_h, :]
    return cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_AREA)


def _paste_photo_into_quad(template: np.ndarray, photo: np.ndarray, quad_pts) -> np.ndarray:
    dst = _order_points(np.array(quad_pts))
    w_top = np.linalg.norm(dst[1] - dst[0])
    w_bottom = np.linalg.norm(dst[2] - dst[3])
    h_left = np.linalg.norm(dst[3] - dst[0])
    h_right = np.linalg.norm(dst[2] - dst[1])
    target_w = max(1, int(round((w_top + w_bottom) / 2)))
    target_h = max(1, int(round((h_left + h_right) / 2)))

    fitted = _cover_crop(photo, target_w, target_h)
    src = np.array(
        [[0, 0], [target_w - 1, 0], [target_w - 1, target_h - 1], [0, target_h - 1]],
        dtype="float32",
    )

    M = cv2.getPerspectiveTransform(src, dst)
    h_t, w_t = template.shape[:2]
    warped = cv2.warpPerspective(fitted, M, (w_t, h_t))

    mask = np.zeros((h_t, w_t), dtype=np.uint8)
    cv2.fillConvexPoly(mask, dst.astype(np.int32), 255)
    mask_3 = cv2.merge([mask, mask, mask])

    result = np.where(mask_3 > 0, warped, template)
    return result


def generate_collage_png(photos: dict) -> bytes:
    """photos: {slot_name: photo_bytes}. Отсутствующие слоты просто
    пропускаются (рамка остаётся с картинкой-заглушкой шаблона)."""
    template = cv2.imread(COLLAGE_TEMPLATE_PATH)
    if template is None:
        raise RuntimeError(f"Не удалось загрузить шаблон коллажа: {COLLAGE_TEMPLATE_PATH}")

    result = template.copy()
    for slot, quad in FRAME_QUADS.items():
        photo_bytes = photos.get(slot)
        if not photo_bytes:
            continue
        arr = np.frombuffer(photo_bytes, dtype=np.uint8)
        photo = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if photo is None:
            continue
        result = _paste_photo_into_quad(result, photo, quad)

    ok, buf = cv2.imencode(".png", result)
    if not ok:
        raise RuntimeError("Не удалось закодировать итоговый коллаж в PNG")
    return buf.tobytes()
