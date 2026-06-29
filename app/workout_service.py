from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from datetime import datetime
from api.services.models import User, WorkoutLog
from app.training import get_user


class WorkoutService:
    """Сервис для управления состоянием тренировки."""

    def __init__(self, session: AsyncSession, user_id: int):
        self.session = session
        self.user_id = user_id

    async def user(self) -> User:
        if not self._user:
            self._user = await get_user(self.session, self.user_id)
        return self._user

    async def save_workout_log(
            self,
            exercise_id: int,
            sets_data: list[dict],  # [{"weight": 60, "reps": 10}, ...]
            pre_assessment: int,
            post_assessment: int = None
    ) -> WorkoutLog:
        """Сохранить лог тренировки."""
        log = WorkoutLog(
            user_id=self.user_id,
            exercise_id=exercise_id,
            sets=sets_data,
            self_assessment_pre=pre_assessment,
            self_assessment_post=post_assessment,
            date=datetime.now()
        )

        self.session.add(log)
        await self.session.flush()

        # Обновляем стрик пользователя
        user = await self.get_user()
        if user:
            user.current_streak += 1
            user.last_activity_date = datetime.now()

        return log

    async def get_last_exercise_performance(self, exercise_id: int) -> dict:
        """Получить последний результат по упражнению."""
        stmt = select(WorkoutLog).where(
            WorkoutLog.user_id == self.user_id,
            WorkoutLog.exercise_id == exercise_id
        ).order_by(desc(WorkoutLog.date)).limit(1)

        result = await self.session.execute(stmt)
        last_log = result.scalar_one_or_none()

        if last_log and last_log.sets:
            return {
                "weight": last_log.sets[0].get("weight", 0),
                "reps": last_log.sets[0].get("reps", 0),
                "sets_count": len(last_log.sets)
            }
        return {"weight": 0, "reps": 0, "sets_count": 0}