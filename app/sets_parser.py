# app/parsers/sets_parser.py
import re
from typing import List, Tuple, Dict, Optional


class SetsInputParser:
    """Парсер для быстрого ввода подходов в различных форматах"""

    @staticmethod
    def parse_quick_input(text: str) -> Tuple[List[Dict], Optional[str]]:
        """
        Парсит строку с подходами в различных форматах.

        Поддерживаемые форматы:
        1. "90 10 10 95 12" → 3 подхода: 90×10, 90×10, 95×12
        2. "90*10 10 95*12" → 3 подхода: 90×10, 90×10, 95×12
        3. "90x10 90x10 95x12" → 3 подхода
        4. "90*10, 90*10, 95*12" → 3 подхода
        5. "90 10 90 10 95 12" → 3 подхода

        Возвращает:
        - List[Dict]: список подходов с весом и повторениями
        - Optional[str]: сообщение об ошибке или None
        """
        if not text or not text.strip():
            return [], "Введите данные в формате: 90 10 10 95 12"

        text = text.strip()
        sets = []

        try:
            # Вариант 1: Формат с разделителями (x, *, ×)
            if any(sep in text for sep in ['x', '*', '×', 'X']):
                sets = SetsInputParser._parse_with_separators(text)

            # Вариант 2: Просто числа через пробел
            else:
                sets = SetsInputParser._parse_numbers_only(text)

            if not sets:
                return [], "Не удалось распознать подходы. Проверьте формат."

            # Валидация данных
            for s in sets:
                if not isinstance(s.get('weight'), (int, float)) or s['weight'] <= 0:
                    return [], f"Некорректный вес: {s.get('weight')}"
                if not isinstance(s.get('reps'), int) or s['reps'] <= 0:
                    return [], f"Некорректное количество повторений: {s.get('reps')}"

            return sets, None

        except Exception as e:
            return [], f"Ошибка парсинга: {str(e)}"

    @staticmethod
    def _parse_with_separators(text: str) -> List[Dict]:
        """Парсит форматы с разделителями: 90*10 10 95*12"""
        sets = []

        # Заменяем различные разделители на стандартный *
        text = re.sub(r'[x×X]', '*', text)
        text = re.sub(r'[,\-;]', ' ', text)

        # Разбиваем на токены
        tokens = text.split()

        i = 0
        while i < len(tokens):
            token = tokens[i]

            # Если токен содержит разделитель (например, 90*10)
            if '*' in token:
                try:
                    weight_str, reps_str = token.split('*')
                    weight = float(weight_str)
                    reps = int(float(reps_str))  # На случай дробных повторений
                    sets.append({'weight': weight, 'reps': reps})
                    i += 1
                except ValueError:
                    # Пробуем как два отдельных числа
                    if i + 1 < len(tokens):
                        try:
                            weight = float(token)
                            reps = int(float(tokens[i + 1]))
                            sets.append({'weight': weight, 'reps': reps})
                            i += 2
                        except ValueError:
                            i += 1
                    else:
                        i += 1

            # Иначе это может быть число
            else:
                if i + 1 < len(tokens):
                    try:
                        weight = float(token)
                        reps = int(float(tokens[i + 1]))
                        sets.append({'weight': weight, 'reps': reps})
                        i += 2
                    except ValueError:
                        i += 1
                else:
                    i += 1

        return sets

    @staticmethod
    def _parse_numbers_only(text: str) -> List[Dict]:
        """Парсит просто числа через пробел: 90 10 10 95 12"""
        sets = []

        # Разбиваем на числа
        tokens = []
        for token in text.split():
            try:
                # Пробуем преобразовать в число
                num = float(token) if '.' in token else int(token)
                tokens.append(num)
            except ValueError:
                continue

        # Обрабатываем пары чисел
        i = 0
        while i + 1 < len(tokens):
            weight = float(tokens[i])
            reps = int(tokens[i + 1])

            sets.append({'weight': weight, 'reps': reps})
            i += 2

        return sets

    @staticmethod
    def parse_single_set(text: str) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Парсит один подход в форматах:
        - "90 10"
        - "90*10"
        - "90x10"
        - "90"
        """
        if not text or not text.strip():
            return None, "Введите данные"

        text = text.strip().lower()

        # Удаляем лишние символы
        text = re.sub(r'[x×*]', ' ', text)

        try:
            parts = text.split()

            if len(parts) == 1:
                # Только вес
                weight = float(parts[0])
                return {'weight': weight, 'reps': None}, None

            elif len(parts) >= 2:
                # Вес и повторения
                weight = float(parts[0])
                reps = int(float(parts[1]))
                return {'weight': weight, 'reps': reps}, None

            else:
                return None, "Неверный формат. Используйте: 90 10 или 90*10"

        except ValueError as e:
            return None, f"Ошибка: {str(e)}. Введите числа."


class SetsFormatter:
    """Форматирование подходов для отображения"""

    @staticmethod
    def format_sets_list(sets: List[Dict]) -> str:
        """Форматирует список подходов для отображения"""
        if not sets:
            return "Нет подходов"

        lines = []
        for i, s in enumerate(sets, 1):
            weight = s.get('weight')
            reps = s.get('reps')

            if weight and reps:
                lines.append(f"{i}. {weight}кг × {reps}")
            elif weight:
                lines.append(f"{i}. {weight}кг (повторения не указаны)")

        return "\n".join(lines)

    @staticmethod
    def format_quick_input_examples() -> str:
        """Примеры быстрого ввода"""
        return (
            "📋 Примеры быстрого ввода:\n"
            "• 90 10 10 95 12 - 3 подхода\n"
            "• 100*8 8 105*6 - 3 подхода\n"
            "• 70x10 80x8 90x6 - 3 подхода\n"
            "• 60 12 70 10 80 8 - 3 подхода"
        )