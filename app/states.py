from aiogram.fsm.state import State, StatesGroup

class Onboarding(StatesGroup):
    experience_start = State()
    training_location = State()
    training_goal = State()
    logging_experience = State()
    completed = State()

class LogWorkoutStates(StatesGroup):
    pre_assessment = State()
    exercise_search = State()
    muscle_group_search = State()
    custom_exercise_input = State()
    sets = State()
    reps = State()
    weight = State()
    add_next_exercise = State()
    post_assessment = State()
    planned_workout = State()
    planned_reps = State()
    planned_weight = State()
    planned_post_assessment = State()

class QuickLogStates(StatesGroup):
    date = State()
    pre_assessment = State()
    exercises_input = State()
    post_assessment = State()

class GenerateWorkoutStates(StatesGroup):
    pre_assessment = State()
    template_select = State()
    edit_plan = State()
    waiting_for_accent = State()

class CustomPlanStates(StatesGroup):
    waiting_for_plan_input = State()
    plan_review = State()

class CreateExerciseStates(StatesGroup):
    waiting_for_muscle_group = State()
    waiting_for_equipment = State()
    waiting_for_category = State()
    waiting_for_difficulty = State()
    review_exercise = State()


class GymTraining(StatesGroup):
    """Состояния для тренировки без плана"""
    choose_exercise = State()
    waiting_for_exercise_name = State()

    # Для пошагового ввода (ОДИН подход за раз):
    waiting_for_input_mode = State()  # Выбор режима ввода
    waiting_for_single_set = State()  # Ожидание ввода подхода "вес повторения"
    waiting_for_set_confirm = State()  # Подтверждение подхода
    waiting_for_next_action = State()  # Что делать дальше (следующий подход/таймер/завершить)

    # Для быстрого ввода (все подходы сразу):
    waiting_for_quick_sets = State()  # Быстрый ввод всех подходов

    # Общие:
    completing_exercise = State()  # Завершение упражнения
    workout_completed = State()  # Тренировка завершена

class ProfileStates(StatesGroup):
    """Состояния для профиля атлета"""
    waiting_for_goal_type = State()
    waiting_for_goal_exercise = State()
    waiting_for_goal_value = State()
    waiting_for_goal_deadline = State()
    waiting_for_schedule_day = State()
    waiting_for_schedule_time = State()
    waiting_for_custom_time = State()
    waiting_for_location = State()
    waiting_for_goal = State()
    waiting_for_logging = State()
    editing_profile = State()
    editing_experience = State()
    editing_level = State()
    waiting_for_gender = State()
    waiting_for_birth_date = State()
    waiting_for_height = State()
    waiting_for_weight = State()
    waiting_for_activity_level = State()

    waiting_for_target_weight = State()
    waiting_for_target_date = State()

    # Состояния для редактирования
    waiting_for_profile_edit_field = State()
    waiting_for_profile_edit_value = State()

    waiting_for_record_search = State()
    waiting_for_muscle_search = State()
    waiting_for_exercise_search = State()

    waiting_for_muscle_stats_search = State()


class GoalStates(StatesGroup):
    """Состояния для установки целей."""

    # Главное меню
    waiting_for_goal_type = State()

    # Весовая цель
    waiting_for_weight_goal_type = State()  # набор или похудение
    waiting_for_weight_target = State()
    waiting_for_weight_deadline = State()

    # Силовая цель
    waiting_for_strength_exercise = State()
    waiting_for_strength_target = State()
    waiting_for_strength_deadline = State()

    # Частота тренировок
    waiting_for_frequency_value = State()
    waiting_for_frequency_period = State()
    waiting_for_frequency_deadline = State()

    # Кастомный дедлайн
    waiting_for_custom_deadline = State()

    # Обновление цели
    waiting_for_goal_update = State()
    waiting_for_weight_update = State()
    waiting_for_strength_update = State()
    waiting_for_frequency_update = State()
    waiting_for_progress_update = State()


class QuickLogTrainingStates(StatesGroup):
    waiting_for_input = State()
    confirm_workout = State()
    waiting_for_muscle_group = State()
    waiting_for_exercise_selection = State()

class CreateExerciseStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_muscle = State()
    waiting_for_equipment = State()
    waiting_for_secondary_muscles = State()
    waiting_for_difficulty = State()
    confirm_exercise = State()

class CreatePlanStates(StatesGroup):
    choosing_method = State() # Sequential or Quick Log
    waiting_for_plan_name = State()
    adding_exercises = State()

class PreferenceStates(StatesGroup):
    waiting_for_search = State()
    waiting_for_search_preference = State()

class SetupScheduleStates(StatesGroup):
    choosing_days = State()  # Выбор дней недели
    setting_time = State()   # Установка времени
    confirming = State()     # Подтверждение

class FeedbackForm(StatesGroup):
    waiting_for_message = State()
    waiting_for_rating = State()
    waiting_for_contact = State()

class FreeTrainingStates(StatesGroup):
    pre_assessment = State()
    waiting_for_input_mode = State()
    exercise_search = State()
    muscle_group_search = State()
    custom_exercise_input = State()
    waiting_for_quick_sets = State()
    waiting_for_single_set = State()
    waiting_for_next_action = State()

class AutoProgressionStates(StatesGroup):
    waiting_for_exercise_selection = State()
    waiting_for_weight_input = State()
    waiting_for_reps_input = State()

class EditExerciseStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_muscle = State()
    waiting_for_secondary = State()
    waiting_for_equipment = State()
    waiting_for_difficulty = State()
    waiting_for_description = State()
    confirm_edit = State()

class HelpWizard(StatesGroup):
    """Состояния для wizard помощи"""

    main_menu = State()

    category = State()

    command_detail = State()

    search = State()
    search_results = State()

    onboarding_start = State()
    onboarding_profile = State()
    onboarding_goals = State()
    onboarding_schedule = State()
    onboarding_complete = State()