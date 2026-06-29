from sqlalchemy import select, Select
from api.services.models import Exercise

def get_base_exercise_query(app_user_id: int) -> Select:
    """
    Возвращает безопасный базовый запрос, отсекающий чужие кастомные упражнения.
    Включает: системные упражнения (app_user_id IS NULL) + кастомные упражнения юзера.
    """
    return select(Exercise).where(
        (Exercise.app_user_id == None) | (Exercise.app_user_id == app_user_id)
    )