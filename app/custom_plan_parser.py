# app/custom_plan_parser.py
import re
import uuid
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


class CustomPlanParser:
    @staticmethod
    def parse_plan_text(text: str) -> Tuple[List[dict], List[str]]:
        """
        Парсит текст. Если строка начинается с '+', она приклеивается к ТЕКУЩЕМУ
        или ПРЕДЫДУЩЕМУ упражнению, создавая единую группу superset_id.
        """
        exercises = []
        errors = []
        lines = text.strip().split('\n')

        current_ss_id = None

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line: continue

            # Флаг: является ли эта строка продолжением (начинается с +)
            is_plus_start = line.startswith('+')
            clean_line = line.lstrip('+').strip()

            # Разбиваем строку, если в ней есть внутренние плюсы (упр1 + упр2)
            parts = clean_line.split('+')

            # Логика склейки суперсета
            if is_plus_start or len(parts) > 1:
                # Если это начало новой связки (первое упр еще без +, но второе с +)
                if not current_ss_id:
                    current_ss_id = str(uuid.uuid4())
                    # Если МЫ УЖЕ добавили предыдущее упражнение, а текущее с +,
                    # то нам надо "откатиться" и пометить предыдущее тем же ID
                    if is_plus_start and exercises:
                        exercises[-1]['superset_id'] = current_ss_id
            else:
                current_ss_id = None

            for part in parts:
                part = part.strip()
                if not part: continue

                parsed = CustomPlanParser._parse_single_unit_to_dict(part)
                if parsed:
                    if current_ss_id:
                        parsed['superset_id'] = current_ss_id
                    exercises.append(parsed)
                else:
                    errors.append(f"Строка {line_num}: Ошибка в '{part}'")

        return exercises, errors

    @staticmethod
    def _parse_single_unit_to_dict(text: str) -> Optional[dict]:
        """Базовый regex парсинг строки в словарь"""
        text = re.sub(r'\s+', ' ', text.strip())
        patterns = [
            r'^(.+?)\s+(\d+)\s*[\*xхх:]\s*(\d+)-(\d+)$',
            r'^(.+?)\s+(\d+)\s*[\*xхх:]\s*(\d+)$',
            r'^(.+?)\s+(\d+)\s*[\*xхх:]\s*(max|макс)$',
        ]
        for p in patterns:
            match = re.match(p, text, re.IGNORECASE)
            if match:
                g = match.groups()
                res = {
                    "name": g[0].strip(), "sets": int(g[1]),
                    "matched_exercise_id": None, "status": "not_found", "superset_id": None
                }
                if "-" in text and len(g) == 4:
                    res.update({"reps_min": int(g[2]), "reps_max": int(g[3])})
                elif g[2].lower() in ['max', 'макс']:
                    res.update({"reps_min": 1, "reps_max": 50})
                else:
                    res.update({"reps_min": int(g[2]), "reps_max": int(g[2])})
                return res
        return {"name": text, "sets": 3, "reps_min": 8, "reps_max": 12, "status": "not_found", "superset_id": None}

    @staticmethod
    def format_plan_for_display(exercises: List[dict]) -> str:
        """
        Группирует упражнения для вывода.
        Формат: 'Индекс. 🔥 СУПЕРСЕТ: упр1, упр2' или 'Индекс. Упражнение'
        """
        if not exercises: return "План пуст"

        res = []
        processed_idx = 0
        block_num = 1  # Порядковый номер группы или упражнения

        while processed_idx < len(exercises):
            curr = exercises[processed_idx]
            sid = curr.get('superset_id')

            if sid:
                # Находим все упражнения этой группы
                group = []
                while processed_idx < len(exercises) and exercises[processed_idx].get('superset_id') == sid:
                    group.append(exercises[processed_idx])
                    processed_idx += 1

                # Формируем блок СУПЕРСЕТА
                res.append(f"{block_num}. 🔥 <b>СУПЕРСЕТ:</b>")
                for ex in group:
                    icon = "✅" if ex.get('matched_exercise_id') or ex.get('exercise_id') else "🆕"
                    reps = ex.get('reps') or f"{ex.get('reps_min', 8)}-{ex.get('reps_max', 12)}"
                    res.append(f"    └ {icon} {ex['name']} ({ex['sets']}×{reps})")
                block_num += 1
            else:
                # Обычное упражнение
                icon = "✅" if curr.get('matched_exercise_id') or curr.get('exercise_id') else "🆕"
                reps = curr.get('reps') or f"{curr.get('reps_min', 8)}-{curr.get('reps_max', 12)}"
                res.append(f"{block_num}. {icon} {curr['name']} ({curr['sets']}×{reps})")
                processed_idx += 1
                block_num += 1

        return "\n".join(res)

    @staticmethod
    async def parse_and_match_exercises(text: str, session, user_id: int):
        """Интерфейс для вызова из хендлеров"""
        from api.services.models import Exercise
        from sqlalchemy import select, func
        entries, errors = CustomPlanParser.parse_plan_text(text)
        if errors: return None, errors
        for entry in entries:
            stmt = select(Exercise).where(func.lower(Exercise.name) == entry['name'].lower())
            res = await session.execute(stmt)
            db_ex = res.scalar_one_or_none()
            if db_ex:
                entry['matched_exercise_id'] = db_ex.id
                entry['name'] = db_ex.name
                entry['status'] = "found"
        return entries, []