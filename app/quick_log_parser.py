import re
from typing import List, Dict, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from api.services.exercise_matcher import ExerciseMatcher
from app.sets_parser import SetsInputParser
from api.services.models import UserExercise
import logging

logger = logging.getLogger(__name__)

class QuickLogParser:

    # app/quick_log_parser.py
    @staticmethod
    async def parse_quick_log(
            session: AsyncSession,
            user_id: int,
            text: str,
            min_similarity: float = 0.5
    ) -> Tuple[List[Dict], Optional[str], List[Dict]]:
        """
        Парсит быстрый лог тренировки с использованием ExerciseMatcher
        """
        lines = text.strip().split('\n')
        parsed_exercises = []

        suggestions_needed = []  # Список упражнений, где нужно выбрать вариант

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue

            exercise_name, sets_part = QuickLogParser._split_exercise_line(line)
            if not exercise_name:
                continue

            sets, sets_error = SetsInputParser.parse_quick_input(sets_part)
            if sets_error:
                return [], f"Ошибка в строке {line_num}: {sets_error}", []

            if not sets:
                continue

            # Ищем упражнение через ExerciseMatcher
            exercise_data, all_matches = await QuickLogParser._find_exercise_with_matcher(
                session, user_id, exercise_name, min_similarity
            )

            # Если есть варианты, но ни один не выбран — добавляем в suggestions
            if not exercise_data and all_matches:
                suggestions_needed.append({
                    'line_num': line_num,
                    'exercise_name': exercise_name,
                    'sets': sets,
                    'variants': all_matches
                })
                continue

            if not exercise_data:
                return [], f"Не удалось найти или создать упражнение: '{exercise_name}'", []

            parsed_exercises.append({
                'exercise_data': exercise_data,
                'sets': sets
            })

        if suggestions_needed:
            return parsed_exercises, None, suggestions_needed

        return parsed_exercises, None, []

    @staticmethod
    def _split_exercise_line(line: str) -> Tuple[str, str]:
        """
        Разделяет строку на название упражнения и подходы

        Примеры:
        "Жим лежа 60x5 70x5" -> ("Жим лежа", "60x5 70x5")
        "Присед 100*5 110*3" -> ("Присед", "100*5 110*3")
        """
        # Убираем лишние пробелы
        line = re.sub(r'\s+', ' ', line.strip())

        # Паттерны для поиска подходов
        patterns = [
            r'(\d+(?:[.,]\d+)?)\s*[xх*]\s*\d+',  # 60x5, 60*5
            r'(\d+(?:[.,]\d+)?)\s+\d+',  # 60 5
            r'(\d+(?:[.,]\d+)?)кг\s*[xх*]?\s*\d+'  # 60кгx5
        ]

        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                # Нашли подходы - всё до них это название упражнения
                exercise_name = line[:match.start()].strip()
                sets_part = line[match.start():].strip()

                # Очищаем название от мусора в конце
                exercise_name = re.sub(r'[,-]\s*$', '', exercise_name).strip()

                return exercise_name, sets_part

        # Не нашли подходов - вся строка это название
        return line.strip(), ""

    @staticmethod
    async def _find_exercise_with_matcher(
            session: AsyncSession,
            user_id: int,
            exercise_name: str,
            min_similarity: float = 0.5
    ) -> Tuple[Optional[Dict], List[Dict]]:
        """Ищет упражнение через ExerciseMatcher с логированием и возвращает (выбранное, все варианты)"""
        logger.info(f"Searching exercise: '{exercise_name}' for user {user_id}")

        try:
            best_match, all_matches = await ExerciseMatcher.find_or_create_exercise(
                session, user_id, exercise_name, min_similarity
            )

            logger.info(f"Результаты поиска для '{exercise_name}': "
                        f"наилучшее совпадение={best_match is not None}, "
                        f"всего вариантов={len(all_matches)}")

            if all_matches:
                for i, match in enumerate(all_matches[:5]):
                    logger.info(f"  Вариант {i + 1}: {match['name']} "
                                f"(источник: {match['source']}, "
                                f"схожесть: {match.get('similarity', 0):.2f})")

            # УЛУЧШЕННАЯ ЛОГИКА: если есть варианты, но ни один не превышает порог
            # берем самый похожий вариант, если он выше порога/2
            if not best_match and all_matches:
                # Проверяем, есть ли вариант с хотя бы средней схожестью
                for match in all_matches:
                    if match.get('similarity', 0) >= min_similarity * 0.7:  # 70% от порога
                        best_match = match
                        break

            if best_match:
                logger.info(f"Выбрано: {best_match['name']} (источник: {best_match['source']})")

                match_type = 'exact'
                similarity = best_match.get('similarity', 1.0)

                if similarity < 1.0:
                    if similarity >= min_similarity:
                        match_type = 'fuzzy'
                    else:
                        match_type = 'low_confidence_fuzzy'

                return {
                    'id': best_match['id'],
                    'name': best_match['name'],
                    'source': best_match['source'],
                    'similarity': similarity,
                    'match_type': match_type,
                    'category': best_match.get('category'),
                    'main_muscle_group': best_match.get('main_muscle_group')
                }, all_matches

            # Если до сих пор не нашли, пытаемся найти по ключевым словам
            fallback_exercise = await QuickLogParser._find_exercise_by_keywords(
                session, user_id, exercise_name
            )

            if fallback_exercise:
                return fallback_exercise, all_matches

            # Последний шанс: создаем пользовательское упражнение
            logger.info(f"Создание пользовательского упражнения: '{exercise_name}'")
            user_exercise = await QuickLogParser._create_user_exercise(session, user_id, exercise_name)
            if user_exercise:
                return user_exercise, []
            else:
                return None, all_matches

        except Exception as e:
            logger.error(f"Ошибка в _find_exercise_with_matcher: {e}")
            return None, []

    @staticmethod
    async def _create_user_exercise(
            session: AsyncSession,
            user_id: int,
            exercise_name: str
    ) -> Optional[Dict]:
        """Создает новое пользовательское упражнение"""
        try:
            # Определяем категорию и мышечную группу
            category, muscle_group = QuickLogParser._classify_exercise(exercise_name)

            new_exercise = UserExercise(
                user_id=user_id,
                name=exercise_name,
                category=category,
                main_muscle_group=muscle_group,
                secondary_muscle_groups=[],
                equipment_needed=['Штанга', 'Гантели'],  # Базовое оборудование
                difficulty='intermediate'
            )

            session.add(new_exercise)
            await session.flush()  # Получаем ID

            return {
                'id': new_exercise.id,
                'name': new_exercise.name,
                'source': 'user',
                'similarity': 1.0,
                'match_type': 'new_user_created'
            }

        except Exception as e:
            print(f"Error creating user exercise: {e}")
            return None

    @staticmethod
    def _classify_exercise(exercise_name: str) -> Tuple[str, str]:
        """Классифицирует упражнение по названию"""
        name_lower = exercise_name.lower()

        # Определяем категорию
        if any(word in name_lower for word in ['бег', 'ходьба', 'вело', 'кардио', 'беговая']):
            category = 'cardio'
        elif any(word in name_lower for word in ['планка', 'пресс', 'скручивание', 'корабль']):
            category = 'core'
        elif any(word in name_lower for word in ['растяжка', 'стретчинг', 'йога', 'пилатес']):
            category = 'flexibility'
        else:
            category = 'strength'

        # Определяем мышечную группу
        if any(word in name_lower for word in
               ['жим лежа', 'жим штанги', 'жим гантелей', 'разведение', 'бабочка', 'кроссовер']):
            muscle_group = 'Грудь'
        elif any(word in name_lower for word in
                 ['присед', 'squat', 'выпады', 'leg press', 'разгибание ног', 'сгибание ног', 'икры']):
            muscle_group = 'Ноги'
        elif any(word in name_lower for word in
                 ['тяга', 'deadlift', 'становая', 'подтягивание', 'широчайшие', 'гиперэкстензия', 'горбун']):
            muscle_group = 'Спина'
        elif any(word in name_lower for word in ['плеч', 'shoulder', 'дельт', 'армейский жим', 'махи', 'шраги']):
            muscle_group = 'Плечи'
        elif any(word in name_lower for word in ['бицепс', 'biceps', 'сгибание рук', 'молот', 'концентрированный']):
            muscle_group = 'Бицепс'
        elif any(word in name_lower for word in ['трицепс', 'triceps', 'разгибание рук', 'французский', 'кикбэк']):
            muscle_group = 'Трицепс'
        elif any(word in name_lower for word in ['пресс', 'abs', 'скручивание', 'подъем ног', 'велосипед', 'вакуум']):
            muscle_group = 'Пресс'
        elif any(word in name_lower for word in ['трапеция', 'шея', 'икры', 'forearm', 'предплечье']):
            muscle_group = 'Другие мышцы'
        else:
            muscle_group = 'Другое'

        return category, muscle_group

    @staticmethod
    async def _find_exercise_by_keywords(
            session: AsyncSession,
            user_id: int,
            exercise_name: str
    ) -> Optional[Dict]:
        """Ищет упражнение по ключевым словам (запасной вариант)"""
        from sqlalchemy import select, or_
        from api.services.models import Exercise

        name_lower = exercise_name.lower()

        # Ключевые слова для популярных упражнений
        keyword_mapping = {
            'жим лежа': ['жим лежа', 'жим штанги лежа', 'bench press'],
            'присед': ['приседания', 'squat', 'присед'],
            'становая': ['становая тяга', 'deadlift'],
            'тяга': ['тяга', 'тяга штанги', 'тяга блока'],
            'подтягивания': ['подтягивания', 'pull-up'],
            'отжимания': ['отжимания', 'push-up'],
            'жим гантелей': ['жим гантелей', 'жим гантелей лежа'],
            'жим стоя': ['жим стоя', 'жим штанги стоя'],
            'разведения': ['разведения гантелей', 'разведения'],
            'сгибания': ['сгибания рук', 'сгибания на бицепс'],
            'французский': ['французский жим', 'разгибания на трицепс'],
        }

        for keyword, variations in keyword_mapping.items():
            if keyword in name_lower:
                # Ищем упражнение по вариациям
                search_terms = variations + [keyword]

                for term in search_terms:
                    stmt = select(Exercise).where(
                        or_(
                            Exercise.name.ilike(f"%{term}%"),
                            Exercise.name.ilike(f"%{exercise_name}%")
                        )
                    )
                    result = await session.execute(stmt)
                    exercise = result.scalar_one_or_none()

                    if exercise:
                        return {
                            'id': exercise.id,
                            'name': exercise.name,
                            'source': 'preset',
                            'similarity': 0.7,  # Принудительно задаем схожесть
                            'match_type': 'keyword_fallback'
                        }

        return None