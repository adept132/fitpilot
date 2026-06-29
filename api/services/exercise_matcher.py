# app/exercise_matcher.py
import re
from typing import Dict, List, Tuple
from difflib import SequenceMatcher
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from api.services.exercise_utils import get_base_exercise_query
from api.services.models import Exercise, UserExercise


class ExerciseMatcher:
    """Умный сопоставитель упражнений на основе существующей базы"""

    @staticmethod
    async def find_or_create_exercise(
            session: AsyncSession,
            user_id: int,
            exercise_name: str,
            min_similarity: float = 0.4  # СНИЖЕН по умолчанию для лучшего fuzzy
    ) -> Tuple[Dict, List[Dict]]:
        """
        Находит существующие упражнения или возвращает варианты.
        """
        # Нормализуем входное название
        normalized_input = ExerciseMatcher._normalize_string(exercise_name)

        # 1. Ищем точные совпадения с нормализацией
        exact_matches = await ExerciseMatcher._find_exact_matches(
            session, user_id, normalized_input
        )

        # 2. Ищем похожие упражнения
        similar_exercises = await ExerciseMatcher._find_similar_exercises(
            session, user_id, normalized_input, min_similarity
        )

        # 3. Если есть варианты, возвращаем их
        all_matches = exact_matches + similar_exercises

        if all_matches:
            all_matches = list({m['id']: m for m in all_matches}.values())
            # Сортируем по похожести
            all_matches.sort(key=lambda x: x.get('similarity', 0), reverse=True)

            # УЛУЧШЕННАЯ ЛОГИКА: выбираем лучший, даже если чуть ниже порога
            if all_matches:
                best_match = all_matches[0]
                # Если лучший > порога/2, выбираем его (адаптивно понижаем)
                if best_match['similarity'] >= min_similarity * 0.5:  # Более мягкий threshold
                    return best_match, all_matches[:5]
                else:
                    # Если есть варианты выше 0.5, возвращаем топ-1
                    filtered = [m for m in all_matches if m['similarity'] >= 0.5]
                    if filtered:
                        return filtered[0], all_matches[:5]

            return None, all_matches[:5]  # Возвращаем варианты для выбора пользователем

        # 4. Если ничего не нашли, НЕ создаем новое — пусть обрабатывает вызывающий код
        return None, []

    @staticmethod
    async def _find_exact_matches(
            session: AsyncSession,
            user_id: int,
            normalized_exercise_name: str
    ) -> List[Dict]:
        """Ищет точные совпадения с нормализацией (база + кастом)"""
        matches = []

        # Один запрос вместо двух
        stmt = get_base_exercise_query(user_id).where(
            Exercise.name.ilike(f"%{normalized_exercise_name}%")
        )
        result = await session.execute(stmt)

        for exercise in result.scalars().all():
            similarity = ExerciseMatcher._calculate_similarity(
                normalized_exercise_name, ExerciseMatcher._normalize_string(exercise.name)
            )
            matches.append({
                'id': exercise.id,
                'name': exercise.name,
                'source': 'user' if exercise.source == 'custom' else 'preset',  # Динамическое определение
                'similarity': similarity,
                'category': exercise.category,
                'main_muscle_group': exercise.main_muscle_group,
                'secondary_muscle_groups': exercise.secondary_muscle_groups,
                'equipment_needed': exercise.equipment_needed,
                'difficulty': exercise.difficulty
            })

        return matches

    @staticmethod
    async def _find_similar_exercises(
            session: AsyncSession,
            user_id: int,
            normalized_exercise_name: str,
            min_similarity: float
    ) -> List[Dict]:
        """Ищет похожие упражнения по нечеткому соответствию с нормализацией"""
        matches = []

        # Одним запросом забираем и базу, и кастом, без ручного склеивания списков
        stmt = get_base_exercise_query(user_id).limit(1500)
        result = await session.execute(stmt)
        all_exercises = list(result.scalars().all())

        for exercise in all_exercises:
            normalized_name = ExerciseMatcher._normalize_string(exercise.name)

            if normalized_exercise_name in normalized_name:
                similarity = 0.9 + (0.1 * len(normalized_exercise_name) / len(normalized_name))
            else:
                similarity = ExerciseMatcher._calculate_similarity(normalized_exercise_name, normalized_name)

            if similarity >= min_similarity * 0.6:
                matches.append({
                    'id': exercise.id,
                    'name': exercise.name,
                    'source': 'user' if exercise.source == 'custom' else 'preset',
                    'similarity': similarity,
                    'category': exercise.category,
                    'main_muscle_group': exercise.main_muscle_group,
                    'secondary_muscle_groups': exercise.secondary_muscle_groups,
                    'equipment_needed': exercise.equipment_needed,
                    'difficulty': exercise.difficulty
                })

        return matches

    @staticmethod
    def _calculate_similarity(str1: str, str2: str) -> float:
        """Вычисляет схожесть двух строк от 0 до 1 с нормализацией"""
        # Нормализация внутри уже выполнена в вызывающих методах, но доп. очистка
        return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

    @staticmethod
    def _normalize_string(s: str) -> str:
        """Нормализует строку с улучшениями для fuzzy-поиска: убирает лишние пробелы, регистр, заменяет 'x'/'х', 'ё'/'е', убирает точки/запятые, стоп-слова и унифицирует термины"""
        s = s.lower().strip()
        s = re.sub(r'\s+', ' ', s)  # Один пробел
        s = re.sub(r'[.,\-]', '', s)  # Убираем разделители
        s = s.replace('х', 'x')  # Унифицируем разделители (для подходов типа 60x5)
        s = s.replace('*', 'x')
        # Нормализуем кириллицу для поиска
        s = s.replace('ё', 'е')  # 'ё' -> 'е' для совпадения с 'е'
        # Унифицируем вариации терминов для лучшего совпадения
        s = s.replace('гантели', 'штанга')  # Унифицировать гантели/штанга как "штанга" для core matching
        s = s.replace('гантель', 'штанга')
        s = s.replace('машина', 'тренажер')  # Унифицировать вариации оборудования
        s = s.replace('машины', 'тренажер')

        # Удаляем стоп-слова для фокуса на ключевых словах
        stop_words = {'на', 'с', 'в', 'для', 'от', 'к', 'по', 'блоки', 'машины',
                      'тренажер'}  # Добавьте по необходимости
        words = [w for w in s.split() if w not in stop_words]

        return ' '.join(words)