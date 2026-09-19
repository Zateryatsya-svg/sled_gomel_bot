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


def edit_distance(a: str, b: str) -> int:
    """Расстояние Левенштейна: сколько букв нужно вставить/убрать/заменить,
    чтобы из одной строки получить другую."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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
    # "fuzzy": N у бита в content.json — допускаем до N ошибок в буквах
    # (опечатка, лишняя/пропущенная буква). Только для ответов от 6 букв,
    # чтобы короткие слова не путались между собой.
    fuzzy = int(clue.get("fuzzy", 0) or 0)
    if normalized_user:
        for ans in accepted:
            normalized_ans = normalize(ans)
            if normalized_ans == normalized_user:
                return True
            if fuzzy and len(normalized_ans) >= 6 and edit_distance(normalized_ans, normalized_user) <= fuzzy:
                return True
    return False


def check_keywords(user_text: str, clue: dict) -> bool:
    """Проверяет ответ пользователя на вхождение хотя бы одного из
    "keywords" (в отличие от check_answer, тут не нужно точное совпадение —
    достаточно, чтобы нормализованный ответ пользователя содержал одно из
    ключевых слов/словосочетаний, например "я у дворца Паскевичей" засчитает
    ключевое слово "дворец"."""
    keywords = clue.get("keywords", [])
    normalized_user = normalize(user_text)
    if not normalized_user:
        return False
    for kw in keywords:
        normalized_kw = normalize(kw)
        if normalized_kw and normalized_kw in normalized_user:
            return True
    return False
