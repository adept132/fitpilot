import uuid
from datetime import datetime, UTC, date as date_type, time as time_type, date
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    Column, Integer, String, ForeignKey, DateTime, Float, Boolean, Text,
    func, Index, BigInteger, UniqueConstraint, Time, Date, CheckConstraint, Numeric, Enum, UUID
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import relationship, declarative_base, mapped_column, Mapped

from api.services.day_template import DayTemplateType
from api.services.exercise_pattern_tags import ExerciseAction, ExerciseVector, ExerciseLaterality
from api.services.mesocycle_phase import MesocyclePhaseEnum
from api.services.scheduling import SchedulingMode, WorkoutStatus, MesocyclePhase

Base = declarative_base()


# --- ЯДРО ПОЛЬЗОВАТЕЛЕЙ ---

class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    firebase_uid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Связи (One-to-One / One-to-Many)
    profile: Mapped[Optional["AppUserProfile"]] = relationship(
        "AppUserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    # ИЗМЕНЕНИЕ: Теперь это One-to-Many (Журнал веса), uselist=True (по умолчанию для List)
    # Сортируем по умолчанию от свежего к старому
    anthropometry_history: Mapped[List["UserAnthropometry"]] = relationship(
        "UserAnthropometry", back_populates="app_user", cascade="all, delete-orphan",
        order_by="desc(UserAnthropometry.recorded_at)"
    )

    sessions: Mapped[List["WorkoutSession"]] = relationship("WorkoutSession", back_populates="app_user")
    user_splits: Mapped[List["UserSplit"]] = relationship("UserSplit", back_populates="app_user")
    mesocycles: Mapped[List["AppUserMesocycle"]] = relationship(
        "AppUserMesocycle",
        back_populates="app_user"
    )

    goals: Mapped[List["UserGoal"]] = relationship("UserGoal", back_populates="app_user")
    records: Mapped[List["UserRecord"]] = relationship("UserRecord", back_populates="app_user")
    custom_exercises: Mapped[List["UserExercise"]] = relationship("UserExercise", back_populates="app_user")
    exercise_preferences: Mapped[List["UserExercisePreference"]] = relationship("UserExercisePreference",
                                                                                back_populates="app_user")
    generator_presets: Mapped[List["AdvancedGeneratorPreset"]] = relationship("AdvancedGeneratorPreset",
                                                                              back_populates="app_user")

    microcycles: Mapped[list["AppUserMicrocycle"]] = relationship("AppUserMicrocycle", back_populates="app_user",
                                                                  cascade="all, delete-orphan")


class AppUserProfile(Base):
    __tablename__ = 'app_user_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             unique=True, index=True)

    username: Mapped[Optional[str]] = mapped_column(String(30), unique=True, index=True)
    bio: Mapped[Optional[str]] = mapped_column(String(150))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(255))

    # Базовые данные (Виджет 1)
    gender: Mapped[Optional[str]] = mapped_column(String(20))  # Перенесли из антропометрии
    experience_level: Mapped[str] = mapped_column(String(20), default='beginner', server_default='beginner')
    training_frequency: Mapped[int] = mapped_column(Integer, default=3, server_default='3')  # Дней в неделю
    pro_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')

    # Тренировочный бюджет (Виджет 2)
    volume_budget: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')

    current_streak: Mapped[int] = mapped_column(default=0, server_default='0')
    longest_streak: Mapped[int] = mapped_column(default=0, server_default='0')
    total_workouts: Mapped[int] = mapped_column(default=0, server_default='0')
    microcycle_length: Mapped[int] = mapped_column(Integer, default=7, server_default='7')

    timezone: Mapped[str] = mapped_column(String(50), default='UTC', server_default='UTC')

    # Настройки и Ограничения (Виджет 3).
    # Внутри будет лежать: {"locations": ["gym"], "prehab_flags": ["lower_back"]}
    settings: Mapped[dict] = mapped_column(
        JSONB,
        default=lambda: {'units': 'kg', 'is_public': True, 'notifications': True, 'prehab_flags': [],
                         'locations': ['gym']},
        server_default='{}'
    )

    user: Mapped["AppUser"] = relationship("AppUser", back_populates="profile")


# --- СПРАВОЧНИК УПРАЖНЕНИЙ ---

class Exercise(Base):
    __tablename__ = 'exercises'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    fatigue_tier: Mapped[int] = mapped_column(Integer, default=2, server_default='2', nullable=False)
    main_muscle_group: Mapped[str] = mapped_column(String(100), nullable=False)
    secondary_muscle_groups: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    equipment_needed: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default='default')
    app_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("app_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )
    video_url: Mapped[Optional[str]] = mapped_column(String(500))

    # --- НОВЫЕ ПОЛЯ КЛАССИФИКАЦИИ ---
    action: Mapped[ExerciseAction] = mapped_column(
        Enum(ExerciseAction, name="exercise_action_enum"),
        default=ExerciseAction.unknown,
        server_default="unknown"
    )
    vector: Mapped[ExerciseVector] = mapped_column(
        Enum(ExerciseVector, name="exercise_vector_enum"),
        default=ExerciseVector.unknown,
        server_default="unknown"
    )
    laterality: Mapped[ExerciseLaterality] = mapped_column(
        Enum(ExerciseLaterality, name="exercise_laterality_enum"),
        default=ExerciseLaterality.unknown,
        server_default="unknown"
    )
    # ---------------------------------

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user_preferences: Mapped[List["UserExercisePreference"]] = relationship(
        "UserExercisePreference", back_populates="exercise", cascade="all, delete-orphan"
    )
    session_exercises: Mapped[List["WorkoutSessionExercise"]] = relationship(
        "WorkoutSessionExercise", back_populates="exercise"
    )

    __table_args__ = (Index('ix_exercises_name_lower', func.lower(name)),)


class UserSplit(Base):
    """
    Активный мезоцикл пользователя.
    Связывает конкретного юзера с абстрактным шаблоном (чертежом) сплита.
    """
    __tablename__ = 'user_splits'

    # Можно оставить Integer, если так удобнее для старых связей,
    # но лучше перевести на UUID для единообразия новых таблиц. Оставим пока Integer.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete="CASCADE"), nullable=False)

    # ССЫЛАЕМСЯ НА НОВЫЙ ЧЕРТЕЖ (Blueprint)
    blueprint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('split_blueprints.id', ondelete="CASCADE")
    )

    # Настройки и прогресс конкретного пользователя по этому чертежу
    selected_plans: Mapped[dict] = mapped_column(JSONB, default=dict)
    start_date: Mapped[datetime] = mapped_column(server_default=func.now())
    is_active: Mapped[bool] = mapped_column(default=True)

    # Бесконечная лента (Rolling Schedule). От 1 до blueprint.length_days
    current_day: Mapped[int] = mapped_column(default=1)
    last_trained_date: Mapped[Optional[datetime]] = mapped_column()

    # Связи
    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="user_splits")
    blueprint: Mapped["SplitBlueprint"] = relationship("SplitBlueprint")


# --- ПЕРИОДИЗАЦИЯ ---

class Mesocycle(Base):
    __tablename__ = "mesocycles"

    # 1. ТЕПЕРЬ ТУТ UUID
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # 2. ПРИВЯЗКА К АВТОРУ (NULL означает, что это системный/глобальный шаблон)
    author_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"),
                                                     nullable=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # 3. УБРАЛИ ГЛОБАЛЬНЫЙ UNIQUE ОТСЮДА
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    phases_in_cycle: Mapped[int] = mapped_column(nullable=False)

    phases: Mapped[List["MesocyclePhase"]] = relationship(
        "MesocyclePhase",
        back_populates="mesocycle",
        cascade="all, delete-orphan",
        order_by="MesocyclePhase.phase_number"
    )

    __table_args__ = (
        # 4. Уникальность: у одного юзера не может быть двух стратегий с одинаковым кодом
        UniqueConstraint("author_id", "code", name="uq_mesocycle_author_code"),
        CheckConstraint("phases_in_cycle > 0", name="ck_mesocycles_phases_positive"),
    )


class MesocyclePhase(Base):
    __tablename__ = "mesocycle_phases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ТУТ ТОЖЕ МЕНЯЕМ ТИП НА UUID, чтобы связь работала
    mesocycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mesocycles.id", ondelete="CASCADE"),
                                                    nullable=False, index=True)

    phase_number: Mapped[int] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    effort_tier: Mapped[str] = mapped_column(String(20), nullable=False)

    mesocycle: Mapped["Mesocycle"] = relationship("Mesocycle", back_populates="phases")


class AppUserMesocycle(Base):
    __tablename__ = "app_user_mesocycles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False,
                                             index=True)

    # ТУТ ТОЖЕ МЕНЯЕМ ТИП НА UUID
    mesocycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mesocycles.id", ondelete="CASCADE"),
                                                    nullable=False)

    is_active: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    microcycle_length: Mapped[int] = mapped_column(Integer, nullable=False)
    current_phase: Mapped[int] = mapped_column(Integer, nullable=True)

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="mesocycles")
    mesocycle: Mapped["Mesocycle"] = relationship("Mesocycle")

# --- МОБИЛЬНЫЕ СЕССИИ (ЛОГИ) ---

class WorkoutSession(Base):
    __tablename__ = "workout_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False,
                                             index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # free | split_day | plan
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    plan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("workout_plans.id", ondelete="SET NULL"), nullable=True)
    split_day_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('split_day_slots.id', ondelete="SET NULL"),
        nullable=True
    )
    app_user_mesocycle_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("app_user_mesocycles.id", ondelete="SET NULL")
    )
    mesocycle_phase: Mapped[Optional[int]] = mapped_column()
    app_user_microcycle_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("app_user_microcycles.id",
                                                                                      ondelete="SET NULL"),
                                                                  nullable=True)
    volume_targets = Column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="sessions")
    exercises: Mapped[List["WorkoutSessionExercise"]] = relationship("WorkoutSessionExercise",
                                                                     back_populates="workout_session",
                                                                     cascade="all, delete-orphan",
                                                                     order_by="WorkoutSessionExercise.order_index")

    __table_args__ = (
        CheckConstraint("source IN ('free', 'split_day', 'plan')", name="ck_workout_sessions_source"),
        CheckConstraint("status IN ('active', 'finished')", name="ck_workout_sessions_status"),
    )


class WorkoutSessionExercise(Base):
    __tablename__ = "workout_session_exercises"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workout_session_id: Mapped[int] = mapped_column(ForeignKey("workout_sessions.id", ondelete="CASCADE"),
                                                    nullable=False, index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id", ondelete="RESTRICT"), nullable=False,
                                             index=True)
    order_index: Mapped[int] = mapped_column(nullable=False)
    superset_group: Mapped[Optional[str]] = mapped_column(String(64))
    recommended_rir: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recommended_rep_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recommended_rep_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workout_session: Mapped["WorkoutSession"] = relationship("WorkoutSession", back_populates="exercises")
    sets: Mapped[List["WorkoutSessionSet"]] = relationship("WorkoutSessionSet",
                                                           back_populates="workout_session_exercise",
                                                           cascade="all, delete-orphan",
                                                           order_by="WorkoutSessionSet.set_number")
    exercise: Mapped["Exercise"] = relationship("Exercise")


class WorkoutSessionSet(Base):
    __tablename__ = "workout_session_sets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workout_session_exercise_id: Mapped[int] = mapped_column(
        ForeignKey("workout_session_exercises.id", ondelete="CASCADE"), nullable=False, index=True)
    set_number: Mapped[int] = mapped_column(nullable=False)
    set_type: Mapped[str] = mapped_column(String(32), default="normal",
                                          server_default="normal")  # normal | warmup | drop
    weight: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    reps: Mapped[Optional[int]] = mapped_column()
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    effort_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_completed: Mapped[bool] = mapped_column(default=True, server_default="true")
    parent_set_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workout_session_sets.id", ondelete="SET NULL"))
    superset_round: Mapped[Optional[int]] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


    workout_session_exercise: Mapped["WorkoutSessionExercise"] = relationship("WorkoutSessionExercise",
                                                                              back_populates="sets")
    parent_set: Mapped[Optional["WorkoutSessionSet"]] = relationship("WorkoutSessionSet",
                                                                     remote_side="WorkoutSessionSet.id")

    __table_args__ = (
        CheckConstraint("set_number > 0", name="ck_workout_session_sets_set_number_positive"),
        CheckConstraint("set_type IN ('normal', 'warmup', 'drop')", name="ck_workout_session_sets_set_type"),
    )


# --- ДАННЫЕ ПОЛЬЗОВАТЕЛЯ ---

class UserExercise(Base):
    __tablename__ = 'user_exercises'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), default='strength')
    main_muscle_group: Mapped[str] = mapped_column(String(100), default='unknown')
    secondary_muscle_groups: Mapped[list] = mapped_column(JSONB, default=list)
    equipment_needed: Mapped[list] = mapped_column(JSONB, default=list)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="custom_exercises")
    user_preferences: Mapped[List["UserExercisePreference"]] = relationship("UserExercisePreference",
                                                                            back_populates="user_exercise",
                                                                            cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_user_exercises_user_name', app_user_id, func.lower(name)),
        Index('ix_user_exercises_name_lower', func.lower(name)),
    )


class UserAnthropometry(Base):
    __tablename__ = 'user_anthropometry'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ИЗМЕНЕНИЕ: Убрали unique=True, так как записей у одного юзера будет много
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             index=True)

    # ИЗМЕНЕНИЕ: Журналирование вместо обновления (Append-Only)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    birth_date: Mapped[Optional[date_type]] = mapped_column(Date)
    height: Mapped[Optional[float]] = mapped_column()
    weight: Mapped[Optional[float]] = mapped_column()
    activity_level: Mapped[Optional[str]] = mapped_column()

    # updated_at больше не нужен, так как мы не обновляем эту строку, а пишем новую

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="anthropometry_history")


class UserGoal(Base):
    __tablename__ = 'user_goals'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_value: Mapped[float] = mapped_column(nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey('exercises.id', ondelete='SET NULL'))
    deadline: Mapped[Optional[date_type]] = mapped_column(Date)
    is_completed: Mapped[bool] = mapped_column(default=False)

    app_user: Mapped["AppUser"] = relationship('AppUser', back_populates='goals')
    exercise: Mapped[Optional["Exercise"]] = relationship('Exercise')
    progress_history: Mapped[List["GoalProgress"]] = relationship('GoalProgress', back_populates='goal',
                                                                  cascade='all, delete-orphan')


class GoalProgress(Base):
    __tablename__ = 'goal_progress'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey('user_goals.id'), nullable=False, index=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             index=True)
    value: Mapped[float] = mapped_column(nullable=False)
    progress_percentage: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    goal: Mapped["UserGoal"] = relationship('UserGoal', back_populates='progress_history')


class UserRecord(Base):
    __tablename__ = 'user_records'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False)
    exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey('exercises.id'))
    user_exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey('user_exercises.id'))
    exercise_name: Mapped[str] = mapped_column(String(200), nullable=False)
    record_type: Mapped[str] = mapped_column(nullable=False)  # max_weight, max_reps
    value: Mapped[float] = mapped_column(nullable=False)
    date_achieved: Mapped[date_type] = mapped_column(Date, default=lambda: datetime.now(UTC).date())

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="records")
    exercise: Mapped[Optional["Exercise"]] = relationship("Exercise")
    user_exercise: Mapped[Optional["UserExercise"]] = relationship("UserExercise")


class UserExercisePreference(Base):
    __tablename__ = 'user_exercise_preferences'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             index=True)
    exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey('exercises.id'))
    user_exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey('user_exercises.id'))
    exercise_name: Mapped[str] = mapped_column(String(200), nullable=False)
    preference: Mapped[str] = mapped_column(String(50), nullable=False)  # favorite | disliked

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="exercise_preferences")
    exercise: Mapped[Optional["Exercise"]] = relationship("Exercise", back_populates="user_preferences")
    user_exercise: Mapped[Optional["UserExercise"]] = relationship("UserExercise", back_populates="user_preferences")

    __table_args__ = (
        UniqueConstraint('app_user_id', 'exercise_id', name='unique_user_exercise_pref_ex'),
        UniqueConstraint('app_user_id', 'user_exercise_id', name='unique_user_exercise_pref_user_ex'),
    )


class AdvancedGeneratorPreset(Base):
    __tablename__ = 'advanced_generator_presets'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('app_users.id', ondelete='CASCADE'), nullable=False,
                                             index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="generator_presets")

    __table_args__ = (UniqueConstraint('app_user_id', 'name', name='unique_preset_name_per_user'),)


class SplitBlueprint(Base):
    __tablename__ = "split_blueprints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    author_id = Column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=True)
    length_days = Column(Integer, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)

    # Связь со слотами
    slots = relationship("SplitDaySlot", back_populates="blueprint", cascade="all, delete-orphan",
                         order_by="SplitDaySlot.day_order")


# 2. Независимый «Кубик» дня (Day Template)
class DayBlueprint(Base):
    """
    Независимая сущность. Хранится в 'карусели'.
    Если удаляется сплит, этот кубик остается жить и может использоваться в других сплитах.
    """
    __tablename__ = "day_blueprints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)  # Например: "Тяжелые ноги" или "Push"
    author_id = Column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=True)
    template_type = Column(
        Enum(DayTemplateType, name="day_template_type", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False
    )
    is_system = Column(Boolean, default=False, nullable=False)

    # Целевые мышцы привязаны именно к кубику, а не к слоту
    muscle_targets = relationship("DayMuscleTarget", back_populates="day", cascade="all, delete-orphan")

    @property
    def muscle_target_names(self) -> list[str]:
        """Возвращает плоский список названий мышц для Pydantic"""
        return [m.muscle_group_id for m in self.muscle_targets]


# 3. Связующая таблица: Слот в сплите (Many-to-Many)
class SplitDaySlot(Base):
    """
    Определяет, на каком месте (day_order) стоит конкретный кубик (day_id) в конкретном сплите (blueprint_id).
    Здесь каскадное удаление оправдано: если удаляем сплит, удаляются только его слоты, но сами кубики (DayBlueprint) остаются.
    """
    __tablename__ = "split_day_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    blueprint_id = Column(UUID(as_uuid=True), ForeignKey("split_blueprints.id", ondelete="CASCADE"), nullable=False)

    # Ссылка на независимый кубик
    day_id = Column(UUID(as_uuid=True), ForeignKey("day_blueprints.id"), nullable=False)

    day_order = Column(Integer, nullable=False)

    # Связи
    blueprint = relationship("SplitBlueprint", back_populates="slots")
    day = relationship("DayBlueprint")


# 4. Целевые мышцы
class DayMuscleTarget(Base):
    __tablename__ = "day_muscle_targets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day_id = Column(UUID(as_uuid=True), ForeignKey("day_blueprints.id", ondelete="CASCADE"), nullable=False)
    muscle_group_id = Column(String, nullable=False)

    day = relationship("DayBlueprint", back_populates="muscle_targets")


class ActiveMesocycle(Base):
    __tablename__ = "active_mesocycles"

    # ИСПРАВЛЕНО: primary_base -> default
    id = Column(UUID(as_uuid=True), default=uuid.uuid4, primary_key=True)

    # ИСПРАВЛЕНО: тип изменен на BigInteger и добавлен ForeignKey
    user_id = Column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)

    # ИСПРАВЛЕНО: ссылка на split_blueprints (чертеж всего сплита, а не одного дня)
    blueprint_id = Column(UUID(as_uuid=True), ForeignKey("split_blueprints.id"), nullable=False)

    start_date = Column(Date, nullable=False, default=date.today)
    scheduling_mode = Column(Enum(SchedulingMode), nullable=False)

    # Массив дней: 0 = Понедельник, 6 = Воскресенье (согласно .weekday() в Python)
    allowed_weekdays = Column(ARRAY(Integer), nullable=True)

    # Массив формата [2, 1] -> 2 дня работы, 1 день отдыха
    rolling_pattern = Column(ARRAY(Integer), nullable=True)

    # Дни, когда тренировки полностью заморожены (например, [6] — воскресенье)
    blackout_weekdays = Column(ARRAY(Integer), nullable=True, default=[])

    # Отношения
    scheduled_workouts = relationship("ScheduledWorkout", back_populates="mesocycle", cascade="all, delete-orphan")


class ScheduledWorkout(Base):
    __tablename__ = "scheduled_workouts"

    id = Column(UUID(as_uuid=True), default=uuid.uuid4, primary_key=True)
    mesocycle_id = Column(UUID(as_uuid=True), ForeignKey("active_mesocycles.id", ondelete="CASCADE"), nullable=False)
    blueprint_day_id = Column(UUID(as_uuid=True), nullable=False)  # Конкретный кубик дня из сплита

    scheduled_date = Column(Date, nullable=False)
    status = Column(Enum(WorkoutStatus), nullable=False, default=WorkoutStatus.pending)
    mesocycle_phase = Column(
        Enum(MesocyclePhaseEnum),
        nullable=False,
        default=MesocyclePhaseEnum.medium
    )

    # Строгий порядковый индекс тренировки в мезоцикле для безопасных сдвигов (Shift)
    workout_order = Column(Integer, nullable=False)

    mesocycle = relationship("ActiveMesocycle", back_populates="scheduled_workouts")

    __table_args__ = (
        CheckConstraint("workout_order > 0", name="check_positive_order"),
    )


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)

    # Название генерируется автоматически или задается юзером
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # НОВОЕ: Теги для каскадной фильтрации
    day_tag: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # Например: "push", "legs"
    micro_tag: Mapped[str] = mapped_column(String(20), nullable=False,
                                           index=True)  # 'easy', 'medium', 'hard', 'adaptive'
    meso_tag: Mapped[str] = mapped_column(String(20), nullable=False,
                                          index=True)  # 'deload', 'easy', 'medium', 'prefailure', 'failure', 'adaptive'

    # Связь с упражнениями в плане
    exercises: Mapped[List["WorkoutPlanExercise"]] = relationship(
        "WorkoutPlanExercise",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="WorkoutPlanExercise.order_index"
    )

    __table_args__ = (
        CheckConstraint(
            "micro_tag IN ('easy', 'medium', 'hard', 'adaptive')",
            name="ck_plan_micro_tag"
        ),
        CheckConstraint(
            "meso_tag IN ('deload', 'easy', 'medium', 'prefailure', 'failure', 'adaptive')",
            name="ck_plan_meso_tag"
        ),
    )


class WorkoutPlanExercise(Base):
    __tablename__ = "workout_plan_exercises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("workout_plans.id", ondelete="CASCADE"), nullable=False)
    exercise_id: Mapped[int] = mapped_column(Integer, ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False)

    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # НОВОЕ: Группировка суперсетов. Если у двух упражнений одинаковый UUID, они в суперсете.
    superset_group_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    # Целевое количество подходов
    target_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Переопределения автопилота (если NULL, используем матрицу на лету)
    # Если юзер ввел ренж руками, мы сохраняем его сюда и отключаем подсветку "Оптимально"
    override_reps: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # e.g. "6-8" or "10"
    override_rir: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    plan: Mapped["WorkoutPlan"] = relationship("WorkoutPlan", back_populates="exercises")

    exercise: Mapped["Exercise"] = relationship("Exercise")


class AppUserMicrocycle(Base):
    __tablename__ = "app_user_microcycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False,
                                             index=True)

    # Название сплита (например, "PPLx2 + Rest")
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Физическая длина микроцикла в днях (включая дни отдыха)
    length_days: Mapped[int] = mapped_column(Integer, nullable=False)

    # JSONB с маппингом тренировочных дней.
    # Формат: {"1": {"type": "hard", "tag": "push"}, "2": {"type": "rest", "tag": None}, ...}
    days_mapping: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')

    # Флаг текущего активного сплита
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')

    app_user: Mapped["AppUser"] = relationship("AppUser", back_populates="microcycles")

    __table_args__ = (
        CheckConstraint("length_days > 0", name="ck_microcycles_length_positive"),
    )


class UserCalendarDay(Base):
    __tablename__ = "user_calendar"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False,
                                             index=True)

    # Главный якорь для фронтенда и Home Screen
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Связи с твоими сущностями планирования
    user_mesocycle_id: Mapped[Optional[int]] = mapped_column(Integer,
                                                             ForeignKey("app_user_mesocycles.id", ondelete="SET NULL"),
                                                             nullable=True)
    mesocycle_phase_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Номер текущей фазы

    user_microcycle_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("app_user_microcycles.id",
                                                                                  ondelete="SET NULL"), nullable=True)
    microcycle_day_number: Mapped[Optional[int]] = mapped_column(Integer,
                                                                 nullable=True)  # Какой день внутри сплита (1, 2, 3...)

    # Координаты, по которым мы нашли план (нужно для дебага и аналитики)
    day_tag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    micro_tag: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    meso_tag: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Итоговый тренировочный план на этот день
    plan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("workout_plans.id", ondelete="SET NULL"),
                                                   nullable=True)

    is_rest_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_blackout: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Статус дня: 'planned', 'completed', 'missed', 'skipped'
    status: Mapped[str] = mapped_column(String(20), default="planned", server_default="planned")

    # Фактически залогированная сессия (появится после завершения тренировки)
    actual_workout_session_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    plan = relationship("WorkoutPlan", lazy="noload")