"""
Проверка ответов пользователя: нормализация текста, учёт альтернативных
формулировок и числовых ответов с допустимой погрешностью.
"""
import re


def normalize(text: str) -> str:
    """Приводит текст к нижнему регистру, убирает ё->е, пробелы и пунктуацию.

    Это нужно, чтобы "Пика", "пика.", "ПИКА!", "пика " и т.п. считались
    одним и тем же ответом, а "Идзковский-1919" == "идзковский 1919".
    """
    if text is None:
        return ""
    t = text.strip().lower()
    t = t.replace("ё", "е")
    # оставляем только буквы (кириллица/латиница) и цифры
    t = re.sub(r"[^a-zа-я0-9]", "", t)
    return t


def extract_number(text: str):
    """Достаёт первое целое число из текста пользователя, если оно есть."""
    if text is None:
        return None
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


def check_answer(user_text: str, clue: dict) -> bool:
    """Проверяет ответ пользователя против описания вопроса (clue/step).

    clue может содержать "numeric" (словарь {"target": int, "tolerance": int})
    и/или "answers" (список текстовых альтернатив) — если заданы оба, ответ
    считается верным, если подходит хотя бы один из них (например, вопрос
    допускает ответ и точным годом, и словом "век").
    """
    numeric_spec = clue.get("numeric")
    if numeric_spec:
        value = extract_number(user_text)
        if value is not None:
            target = numeric_spec["target"]
            tolerance = numeric_spec.get("tolerance", 0)
            if abs(value - target) <= tolerance:
                return True

    accepted = clue.get("answers", [])
    normalized_user = normalize(user_text)
    if normalized_user:
        for ans in accepted:
            if normalize(ans) == normalized_user:
                return True
    return False
