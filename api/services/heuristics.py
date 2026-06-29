import re

from api.services.exercise_pattern_tags import ExerciseAction, ExerciseVector, ExerciseLaterality


class HeuristicsEngine:
    @staticmethod
    def classify_exercise(name: str, main_muscle_group: str) -> dict:
        name_lower = name.lower()
        muscle_lower = main_muscle_group.lower()

        action = ExerciseAction.unknown
        vector = ExerciseVector.unknown
        laterality = ExerciseLaterality.unknown

        # ==========================================
        # ЭТАП 1: ЖЕСТКИЕ ТРИГГЕРЫ И ИСКЛЮЧЕНИЯ
        # ==========================================

        # 1. Carry (Переноски)
        if re.search(r'прогулк|переноск|carry', name_lower):
            action = ExerciseAction.carry

        # 2. Plantarflexion (Икры)
        elif re.search(r'на носки|голен|calf|calves', name_lower):
            action = ExerciseAction.plantarflexion
            vector = ExerciseVector.vertical

        # 3. Shoulder Extension (Пуловеры и прямые руки)
        elif re.search(r'пуловер|pullover|прямых рук|прямыми рук', name_lower):
            action = ExerciseAction.shoulder_extension
            if re.search(r'леж|скамь', name_lower):
                vector = ExerciseVector.horizontal
            else:
                vector = ExerciseVector.vertical

        # 4. Elevation (Шраги)
        elif re.search(r'шраг|shrug', name_lower):
            action = ExerciseAction.elevation
            vector = ExerciseVector.vertical

        # 5. Rotation & Lateral Flexion (Вращения, косые скручивания, боковые наклоны)
        elif re.search(r'вращен|ротац|woodchopper|face pull|кубинск', name_lower):
            action = ExerciseAction.rotation
        elif re.search(r'косы[ех]|наклоны в сторон|боков.*экстенз|боков.*скручив', name_lower):
            action = ExerciseAction.lateral_flexion

        # 5.5. Core (Защита пресса от попадания в flexion конечностей)
        elif re.search(r'туловищ|подъем ног|скручиван|планка|пресс|crunch|roll|книжка', name_lower):
            action = ExerciseAction.core

        # 6. Hip Hinge (Становые, Гудморнинг, Ягодичные мосты)
        elif re.search(r'станов|румынск|мертв|гудморнинг|ягодичн.*мост|hip thrust|подъем таза|подъем ягодиц',
                       name_lower):
            action = ExerciseAction.hinge

        # 7. Knee-Dominant (Приседы, Жимы ногами - изоляция от слова "жим")
        elif re.search(r'жим ногами|leg press|присед|выпад|squat|lunge|зашагиван', name_lower):
            action = ExerciseAction.squat

        # 8. Инверсии антагонистов и Сленг (Сюда же "Французский жим")
        elif re.search(r'французск.*жим|skullcrush', name_lower):
            action = ExerciseAction.extension
        elif re.search(r'обратн.*(бабочк|разведен|сведен|пек-дек)', name_lower):
            action = ExerciseAction.abduction
        elif re.search(r'мах.*назад', name_lower):
            action = ExerciseAction.extension

        # 9. Инверсии предплечий (Ладони вниз/вверх)
        elif re.search(r'запяст|кист', name_lower):
            if re.search(r'вниз|сверху|пронац', name_lower):
                action = ExerciseAction.extension
            else:
                action = ExerciseAction.flexion

        # ==========================================
        # ЭТАП 2: АНАТОМИЧЕСКАЯ ИЗОЛЯЦИЯ
        # ==========================================
        elif action == ExerciseAction.unknown:
            if re.search(r'сгибан|curl|бицепс', name_lower) and not re.search(r'разгибан', name_lower):
                action = ExerciseAction.flexion
            elif re.search(r'разгибан|extension|трицепс|выпрямлен.*ног', name_lower) or (
                    'трицепс' in muscle_lower and re.search(r'тяг', name_lower)):
                action = ExerciseAction.extension

        # ==========================================
        # ЭТАП 3: БАЗОВЫЕ ПАТТЕРНЫ
        # ==========================================
        if action == ExerciseAction.unknown:
            if re.search(r'отжиман', name_lower):
                action = ExerciseAction.push
            elif re.search(r'жим|press|толчок', name_lower):
                action = ExerciseAction.push
            elif re.search(r'тяг|подтягиван|row|pull', name_lower):
                action = ExerciseAction.pull
            elif re.search(r'отведен|мах|abduction|разведен', name_lower):
                action = ExerciseAction.abduction
            elif re.search(r'сведен|adduction|бабочка|пек-дек', name_lower):
                action = ExerciseAction.adduction

        # ФОЛЛБЭК ДЛЯ ACTION
        if action == ExerciseAction.unknown:
            if 'грудь' in muscle_lower:
                action = ExerciseAction.push
            elif 'спина' in muscle_lower:
                action = ExerciseAction.pull
            elif 'пресс' in muscle_lower:
                action = ExerciseAction.core
            elif 'бицепс' in muscle_lower and 'бедра' not in muscle_lower:
                action = ExerciseAction.flexion
            elif 'трицепс' in muscle_lower:
                action = ExerciseAction.extension

        # ==========================================
        # ЭТАП 4: ВЕКТОР (Строго контекстный)
        # ==========================================
        if vector == ExerciseVector.unknown:
            if action == ExerciseAction.pull:
                if re.search(r'верхне|подтягиван|вертикаль|сверху|pull-up|pulldown', name_lower):
                    vector = ExerciseVector.vertical
                elif re.search(r'нижне|в наклоне|горизонталь|к поясу|row', name_lower):
                    vector = ExerciseVector.horizontal

            elif action in (ExerciseAction.push, ExerciseAction.adduction, ExerciseAction.abduction,
                            ExerciseAction.flexion, ExerciseAction.extension):
                if re.search(r'брусья|отрицательн|вниз|decline', name_lower):
                    vector = ExerciseVector.decline
                elif re.search(r'наклонн|положительн|углом|верхн.*груд|incline', name_lower):
                    vector = ExerciseVector.incline
                elif re.search(r'стоя|армейский|над головой|вверх|overhead|на руках', name_lower):
                    vector = ExerciseVector.vertical
                elif re.search(r'лежа|горизонтальн|flat|отжиман', name_lower):
                    vector = ExerciseVector.horizontal

        # ФОЛЛБЭК ДЛЯ VECTOR
        if vector == ExerciseVector.unknown:
            if action == ExerciseAction.push and 'грудь' in muscle_lower:
                vector = ExerciseVector.horizontal
            elif action == ExerciseAction.push and 'плечи' in muscle_lower:
                vector = ExerciseVector.vertical

        # ==========================================
        # ЭТАП 5: ЛАТЕРАЛЬНОСТЬ (Неявная и Явная)
        # ==========================================
        # Добавлены маркеры косых и боковых движений
        if re.search(
                r'одной|поочередн|унилат|single|одноруч|одноног|пистолетик|выпад|lunge|концентрирован|косы[ех]|боков',
                name_lower):
            laterality = ExerciseLaterality.unilateral
        elif re.search(r'(отведени[ея]|сгибани[ея]|разгибани[ея]|тяга)\s+(руки|ноги)\b', name_lower):
            laterality = ExerciseLaterality.unilateral
        else:
            laterality = ExerciseLaterality.bilateral

        return {
            "action": action,
            "vector": vector,
            "laterality": laterality
        }