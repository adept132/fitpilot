"""Пороги и предохранители решателя объёма.

ВСЕ ЗНАЧЕНИЯ СТАРТОВЫЕ, подлежат калибровке на накопленных данных.
Ни одного из этих чисел не должно появиться в decide.py напрямую.
"""

# --- Виды правок ---
KIND_BUDGET_TO_RANGE = "budget_to_range"
KIND_PRESCRIPTION_ADD = "prescription_add"
KIND_PRESCRIPTION_CUT = "prescription_cut"
KIND_BUDGET_TO_FREQUENCY = "budget_to_frequency"

# --- Основания ---
REASON_BELOW_MEV = "below_mev"
REASON_ABOVE_MRV = "above_mrv"
REASON_DIRECT_ABOVE_CAP = "direct_above_cap"
REASON_DIRECT_BELOW_FLOOR = "direct_below_floor"
REASON_PRESCRIPTION_GAP = "prescription_gap"
REASON_ADHERENCE_GAP = "adherence_gap"

# [КОНФИГ] Сколько окон подряд должен держаться недобор, прежде чем о нём
# заговорить. Потолок реагирует с ПЕРВОГО окна, пол требует подтверждения:
# приложение должно неохотно требовать большего и быстро замечать
# избыточное (спека §5.1).
WINDOWS_FOR_FLOOR_TRIGGER = 2
WINDOWS_FOR_CEILING_TRIGGER = 1

# [КОНФИГ] Доля выполненных рабочих дней, ниже которой расписание считается
# нереалистичным. Одно окно ниже порога — это отпуск, два подряд — это
# расписание, поэтому триггеру нужны оба.
ADHERENCE_MIN_RATIO = 0.7
WINDOWS_FOR_ADHERENCE_TRIGGER = 2

# [КОНФИГ] Насколько предписание должно недобирать до цели, чтобы это
# считалось дефектом планирования, а не округлением.
PRESCRIPTION_GAP_MIN_SETS = 2

# [КОНФИГ] Доля прямого объёма в превышении потолка, начиная с которой
# рычаг остаётся на самой мышце (прямая срезка предписания), а не на
# базовом упражнении выше по цепочке (правка на бюджет). Ниже порога
# избыток преимущественно косвенный — требовать «убери подходы с
# трицепса», когда трицепс забит жимами, вредный совет.
DIRECT_SHARE_MAJORITY_RATIO = 0.5

# Производный флаг: требует ли пол подтверждения предыдущим окном.
# Вычисляется из WINDOWS_FOR_FLOOR_TRIGGER здесь, а не литералом в
# decide.py, чтобы порог оставался единственным источником истины.
FLOOR_REQUIRES_PREVIOUS_WINDOW = WINDOWS_FOR_FLOOR_TRIGGER > 1

# [КОНФИГ] Приоритет оснований для заголовка карточки. Порядок значим:
# превышение потолка опаснее недобора, а прямое превышение — самый
# действенный довод, потому что рычаг у пользователя прямо в руках.
REASON_PRIORITY = (
    REASON_DIRECT_ABOVE_CAP,
    REASON_ABOVE_MRV,
    REASON_BELOW_MEV,
    REASON_DIRECT_BELOW_FLOOR,
    REASON_ADHERENCE_GAP,
    REASON_PRESCRIPTION_GAP,
)
