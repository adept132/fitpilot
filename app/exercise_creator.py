# app/exercise_creator.py
from typing import Dict, List
import re


class ExerciseCreator:
    """Помогает создавать упражнения с ручным определением параметров"""

    MUSCLE_GROUPS = [
        "chest", "back", "legs", "shoulders",
        "biceps", "triceps", "forearms", "calves",
        "abs", "glutes", "traps", "lats", "quads",
        "hamstrings", "fullbody"
    ]

    EQUIPMENT_TYPES = [
        "barbell", "dumbbell", "machine", "cable",
        "bodyweight", "kettlebell", "resistance_band",
        "ez-bar", "medicine_ball", "smith_machine"
    ]

    CATEGORIES = ["strength", "hypertrophy", "power", "endurance", "warmup"]
    DIFFICULTIES = ["beginner", "intermediate", "advanced"]

    @staticmethod
    def suggest_parameters(exercise_name: str) -> Dict:
        """Предлагает параметры для нового упражнения на основе названия"""
        name_lower = exercise_name.lower()

        suggestions = {
            'name': exercise_name,
            'category': 'strength',
            'main_muscle_group': 'unknown',
            'secondary_muscle_groups': [],
            'equipment_needed': [],
            'difficulty': 'intermediate',
            'description': '',
            'suggestions': []
        }

        # Простые подсказки на основе ключевых слов
        keyword_hints = {
            'жим': {'muscles': ['chest', 'shoulders'], 'equipment': ['barbell', 'dumbbell']},
            'тяга': {'muscles': ['back'], 'equipment': ['barbell', 'dumbbell', 'cable']},
            'присед': {'muscles': ['legs'], 'equipment': ['barbell']},
            'выпады': {'muscles': ['legs'], 'equipment': ['dumbbell', 'bodyweight']},
            'подтягивания': {'muscles': ['back'], 'equipment': ['bodyweight']},
            'отжимания': {'muscles': ['chest', 'triceps'], 'equipment': ['bodyweight']},
            'планка': {'muscles': ['abs'], 'equipment': ['bodyweight'], 'category': 'endurance'},
            'скручивания': {'muscles': ['abs'], 'equipment': ['bodyweight']},
            'разводки': {'muscles': ['chest'], 'equipment': ['dumbbell']},
            'сгибания': {'muscles': ['biceps'], 'equipment': ['dumbbell', 'barbell']},
            'разгибания': {'muscles': ['triceps'], 'equipment': ['cable', 'dumbbell']},
            'махи': {'muscles': ['shoulders'], 'equipment': ['dumbbell']},
        }

        for keyword, hints in keyword_hints.items():
            if keyword in name_lower:
                if 'muscles' in hints and suggestions['main_muscle_group'] == 'unknown':
                    suggestions['main_muscle_group'] = hints['muscles'][0]
                    suggestions['secondary_muscle_groups'] = hints['muscles'][1:] if len(hints['muscles']) > 1 else []

                if 'equipment' in hints:
                    suggestions['equipment_needed'] = hints['equipment']

                if 'category' in hints:
                    suggestions['category'] = hints['category']

                suggestions['suggestions'].append(f"Похоже на упражнение с '{keyword}'")

        # Определяем оборудование по словам
        equipment_words = {
            'штанга': 'barbell',
            'гантел': 'dumbbell',
            'тренажер': 'machine',
            'блок': 'cable',
            'весом тела': 'bodyweight',
            'гиря': 'kettlebell',
            'эспандер': 'resistance_band',
        }

        for word, equipment in equipment_words.items():
            if word in name_lower and equipment not in suggestions['equipment_needed']:
                suggestions['equipment_needed'].append(equipment)

        # Если оборудование не определили, ставим по умолчанию
        if not suggestions['equipment_needed']:
            suggestions['equipment_needed'] = ['dumbbell']

        # Формируем описание
        suggestions['description'] = (
            f"Упражнение '{exercise_name}'. "
            f"Основная группа мышц: {suggestions['main_muscle_group']}. "
            f"Оборудование: {', '.join(suggestions['equipment_needed'])}."
        )

        return suggestions