"""Авто-заполнение упражнения: по названию -> мышцы/вторичные/оборудование.

kNN по мультиязычным эмбеддингам над размеченным корпусом (free-exercise-db + наш
каталог), собранным офлайн в scripts/build_exercise_index.py. Вектор запроса считаем
в рантайме той же моделью через onnxruntime (без torch). Если артефакты/зависимости
недоступны — мягкий фолбэк на явный разбор названия (не хуже прежних словарей).

Артефакты (api/data/): exercise_index.npy, exercise_index.json, embedder/ (ONNX+токенайзер).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from api.services import equipment as equip
from api.services import exercise_taxonomy as tax

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INDEX_VECS = DATA_DIR / "exercise_index.npy"
INDEX_LABELS = DATA_DIR / "exercise_index.json"
EMBEDDER_DIR = DATA_DIR / "embedder"


def _onnx_enabled() -> bool:
    """Включён ли тяжёлый ONNX-путь (CLASSIFIER_MODE=onnx).

    Выключен по умолчанию, и это осознанный fail-safe. Модель с токенайзером на
    250k токенов даёт пик ~900 МБ: 155 МБ сессия, 245 МБ токенайзер, ~380 МБ
    арена аллокатора на первом инференсе. На инстансе с 512 МБ процесс убивает
    OOM-killer, а не исключение — поэтому мягкий фолбэк ниже (except Exception)
    в этом сценарии не срабатывает НИ РАЗУ, и каждое нажатие «Определить
    автоматически» заново роняет сервис.

    Забытая переменная окружения должна деградировать в качество, а не в
    падение, поэтому включать режим нужно явно.
    """
    return (os.getenv("CLASSIFIER_MODE") or "").strip().lower() == "onnx"

TOP_K = 10
MIN_SIM = 0.30  # ниже — считаем, что близких соседей нет (низкая уверенность)
SECONDARY_MIN_RATIO = 0.35  # порог веса вторичной мышцы относительно главной
MAX_SECONDARY = 3


# ---------------------------------------------------------------------------
# Ленивая загрузка модели и индекса
# ---------------------------------------------------------------------------

class _Embedder:
    """ONNX-эмбеддер запроса. Грузится лениво; при ошибке остаётся неактивным."""

    def __init__(self) -> None:
        self._ready: Optional[bool] = None
        self._session = None
        self._tokenizer = None
        self._input_names: set = set()

    def _load(self) -> None:
        # Проверяем режим ДО импорта onnxruntime: сам импорт стоит ~16 МБ,
        # а создание сессии — сотни.
        if not _onnx_enabled():
            self._ready = False
            return

        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = EMBEDDER_DIR / "model_quantized.onnx"
            if not model_path.exists():
                model_path = EMBEDDER_DIR / "model.onnx"
            tok_path = EMBEDDER_DIR / "tokenizer.json"
            if not model_path.exists() or not tok_path.exists():
                self._ready = False
                return

            # Арена аллокатора onnxruntime на первом инференсе занимала ~380 МБ
            # под сиквенс длиной до max_position_embeddings, хотя название
            # упражнения — это десяток токенов. С выключенной ареной инференс
            # стоит +2 МБ. Один поток: запрос короткий, параллелить нечего, а
            # каждый поток держит свой пул.
            options = ort.SessionOptions()
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1

            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._input_names = {i.name for i in self._session.get_inputs()}
            self._tokenizer = Tokenizer.from_file(str(tok_path))
            self._ready = True
        except Exception:  # noqa: BLE001 — нет onnxruntime/tokenizers/файлов -> фолбэк
            self._ready = False

    @property
    def ready(self) -> bool:
        if self._ready is None:
            self._load()
        return bool(self._ready)

    def embed(self, text: str) -> np.ndarray:
        enc = self._tokenizer.encode(text)
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        feed = {}
        if "input_ids" in self._input_names:
            feed["input_ids"] = ids
        if "attention_mask" in self._input_names:
            feed["attention_mask"] = mask
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self._session.run(None, feed)[0]  # (1, seq, dim)
        return _mean_pool_normalize(out[0], enc.attention_mask)


def _mean_pool_normalize(hidden: np.ndarray, attention_mask) -> np.ndarray:
    """Mean pooling с маской + L2-нормализация (как у SentenceTransformer)."""
    mask = np.array(attention_mask, dtype=np.float32)[:, None]  # (seq, 1)
    summed = (hidden * mask).sum(axis=0)
    counts = np.clip(mask.sum(axis=0), 1e-9, None)
    vec = summed / counts
    norm = np.linalg.norm(vec)
    return (vec / norm).astype(np.float32) if norm > 0 else vec.astype(np.float32)


class _Index:
    def __init__(self) -> None:
        self._loaded = False
        self.vectors: Optional[np.ndarray] = None
        self.labels: List[dict] = []

    def load(self) -> bool:
        if self._loaded:
            return self.vectors is not None
        self._loaded = True
        try:
            if INDEX_VECS.exists() and INDEX_LABELS.exists():
                self.vectors = np.load(INDEX_VECS)
                self.labels = json.loads(INDEX_LABELS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.vectors = None
        return self.vectors is not None


_embedder = _Embedder()
_index = _Index()


# ---------------------------------------------------------------------------
# Явный разбор оборудования из названия (высокая точность)
# ---------------------------------------------------------------------------

def _equipment_from_name(name: str) -> List[str]:
    """Оборудование из названия. Разбор по основам слов — см. equipment.py."""
    return equip.equipment_from_text(name)


# ---------------------------------------------------------------------------
# Агрегация соседей (чистая логика — тестируется без модели)
# ---------------------------------------------------------------------------

def _aggregate(
    neighbors: List[Tuple[dict, float]], name: str
) -> Dict:
    """neighbors: [(label, sim)], sim по убыванию. -> предложение полей."""
    main_w: Dict[str, float] = defaultdict(float)
    sec_w: Dict[str, float] = defaultdict(float)
    eq_w: Dict[str, float] = defaultdict(float)

    for label, sim in neighbors:
        w = max(float(sim), 0.0)
        if label.get("main_muscle"):
            main_w[label["main_muscle"]] += w
        for m in label.get("secondary") or []:
            sec_w[m] += w
        for e in label.get("equipment") or []:
            eq_w[e] += w

    main_sorted = sorted(main_w.items(), key=lambda kv: kv[1], reverse=True)
    main_muscle = main_sorted[0][0] if main_sorted else None
    top_w = main_sorted[0][1] if main_sorted else 0.0
    second_w = main_sorted[1][1] if len(main_sorted) > 1 else 0.0

    # Вторичные: заметный вес и не совпадают с главной.
    secondary = [
        m for m, w in sorted(sec_w.items(), key=lambda kv: kv[1], reverse=True)
        if m != main_muscle and top_w > 0 and w >= SECONDARY_MIN_RATIO * top_w
    ][:MAX_SECONDARY]

    # Оборудование: явное из названия (надёжно) + проголосованное соседями.
    explicit = _equipment_from_name(name)
    voted = [
        e for e, w in sorted(eq_w.items(), key=lambda kv: kv[1], reverse=True)
        if top_w > 0 and w >= SECONDARY_MIN_RATIO * top_w
    ]
    equipment: List[str] = list(explicit)
    for e in voted:
        if e not in equipment:
            equipment.append(e)

    # Уверенность: близость лучшего соседа * отрыв главной мышцы.
    best_sim = max((s for _, s in neighbors), default=0.0)
    margin = 1.0 if top_w == 0 else (top_w - second_w) / top_w
    confidence = round(float(max(0.0, min(1.0, best_sim)) * (0.5 + 0.5 * margin)), 3)

    return {
        "main_muscle_group": main_muscle,
        "secondary_muscle_groups": secondary,
        "equipment_needed": equipment,
        "confidence": confidence,
        "source": "knn",
    }


# ---------------------------------------------------------------------------
# Фолбэк без модели/индекса
# ---------------------------------------------------------------------------

# Правила «основы слов -> мышца». Совпадение по ОСНОВЕ, а не по точной форме:
# прежний вариант искал подстроку в целой строке, из-за чего "жим лежа" не
# находилось в "жим штангИ лежа" — слова там не соседние.
#
# Порядок значим, выигрывает первое подошедшее правило: специфичные комбинации
# идут раньше общих ("бицепс бедра" раньше "бицепс", "жим стоя" раньше
# "жим лёжа", "тяга штанги" раньше generic "тяга").
#
# Названия мышц берутся из словаря каталога — см. test_fallback_lexicon.py.
_FALLBACK_RULES: List[Tuple[Tuple[str, ...], str]] = [
    # Задняя поверхность бедра — раньше руки, иначе "бицепс бедра" уедет в Бицепс.
    (("бицепс", "бедр"), "Бицепсы ног"),
    (("сгибан", "ног"), "Бицепсы ног"),
    (("румынск",), "Бицепсы ног"),

    (("гиперэкстенз",), "Поясница"),
    # Становую каталог относит к задней поверхности бедра, а не к пояснице —
    # фолбэк должен отвечать так же, как размечен каталог.
    (("станов",), "Бицепсы ног"),

    # Трицепс — раньше груди, иначе "французский жим" уедет в жимы.
    (("французск",), "Трицепс"),
    (("разгибан", "рук"), "Трицепс"),
    (("отжиман", "брус"), "Трицепс"),
    (("трицепс",), "Трицепс"),

    (("сгибан", "рук"), "Бицепс"),
    (("молот",), "Бицепс"),
    (("бицепс",), "Бицепс"),

    # Разведения расходятся по трём разным мышцам — разбираем до общего правила.
    (("развед", "наклон"), "Задняя дельта"),
    (("развед", "ног"), "Абдукторы"),
    (("развед", "сторон"), "Средняя дельта"),
    (("развед", "стоя"), "Средняя дельта"),
    (("отведен", "сторон"), "Средняя дельта"),
    (("обратн", "бабочк"), "Задняя дельта"),
    (("задн", "дельт"), "Задняя дельта"),

    # Дельты — раньше груди: "жим стоя" это плечи, а не жим лёжа.
    (("жим", "стоя"), "Передняя дельта"),
    (("армейск",), "Передняя дельта"),
    (("передн", "дельт"), "Передняя дельта"),
    (("тяг", "подбород"), "Средняя дельта"),
    (("мах", "ног"), "Ягодицы"),
    (("мах", "назад"), "Ягодицы"),
    (("мах",), "Средняя дельта"),
    (("дельт",), "Средняя дельта"),

    (("шраг",), "Трапеция"),
    (("трапец",), "Трапеция"),

    # Наклон и положение лёжа перебивают "сидя": жим сидя на наклонной — грудь.
    (("жим", "леж"), "Грудь"),
    (("жим", "наклон"), "Грудь"),
    (("жим", "сид"), "Передняя дельта"),
    (("развед", "леж"), "Грудь"),
    (("сведен", "рук"), "Грудь"),
    (("сведен", "леж"), "Грудь"),
    (("разводк",), "Грудь"),
    (("развед",), "Грудь"),
    (("бабочк",), "Грудь"),
    (("отжиман",), "Грудь"),

    # Икры раньше квадрицепсов: "подъём на носки в тренажёре для жима ногами"
    # иначе уедет в ("жим","ног").
    (("икр",), "Икры"),
    (("носк",), "Икры"),
    (("голен",), "Икры"),

    (("жим", "ног"), "Квадрицепсы"),
    (("разгибан", "ног"), "Квадрицепсы"),
    (("выпрямлен", "ног"), "Квадрицепсы"),
    (("присед",), "Квадрицепсы"),
    (("выпад",), "Квадрицепсы"),
    (("квадрицепс",), "Квадрицепсы"),

    (("ягодичн",), "Ягодицы"),
    (("мостик",), "Ягодицы"),
    (("ягод",), "Ягодицы"),

    (("подтягив",), "Широчайшие"),
    (("пуловер",), "Широчайшие"),
    (("тяг", "верхн"), "Широчайшие"),
    (("тяг", "нижн"), "Средняя часть спины"),
    (("тяг", "блок"), "Широчайшие"),
    (("широчайш",), "Широчайшие"),
    (("тяг", "штанг"), "Средняя часть спины"),
    (("тяг", "гантел"), "Средняя часть спины"),
    (("тяг", "горизонт"), "Средняя часть спины"),
    (("тяг",), "Широчайшие"),

    (("подъем", "ног"), "Пресс"),
    (("подъем", "тулов"), "Пресс"),
    (("скручив",), "Пресс"),
    (("планк",), "Пресс"),
    (("пресс",), "Пресс"),

    (("запяст",), "Предплечья"),
    (("предплеч",), "Предплечья"),

    (("аддуктор",), "Аддукторы"),
    (("сведен", "ног"), "Аддукторы"),
    (("абдуктор",), "Абдукторы"),
    (("отведен", "ног"), "Абдукторы"),

    # --- Жадные одиночные основы. Только в самом конце: "к груди" встречается
    # в тягах и жимах на плечи, голый "жим" — почти всегда жим лёжа, голые
    # "сгибания" без уточнения — на бицепс.
    (("груд",), "Грудь"),
    (("жим",), "Грудь"),
    (("сгибан",), "Бицепс"),
    (("curl",), "Бицепс"),
]


def _muscle_from_name(name: str) -> Optional[str]:
    tokens = equip.tokenize(name)
    if not tokens:
        return None
    for stems, muscle in _FALLBACK_RULES:
        if all(any(t.startswith(stem) for t in tokens) for stem in stems):
            return muscle
    return None


def _fallback(name: str) -> Dict:
    main = _muscle_from_name(name)
    return {
        "main_muscle_group": main,
        "secondary_muscle_groups": [],
        "equipment_needed": _equipment_from_name(name),
        "confidence": 0.0,
        "source": "fallback",
    }


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def classify(name: str) -> Dict:
    name = (name or "").strip()
    if not name:
        return {"main_muscle_group": None, "secondary_muscle_groups": [],
                "equipment_needed": [], "confidence": 0.0, "source": "empty"}

    if not (_embedder.ready and _index.load()):
        return _fallback(name)

    query = _embedder.embed(name)
    sims = _index.vectors @ query  # оба L2-нормализованы -> косинус
    k = min(TOP_K, len(sims))
    top_idx = np.argpartition(-sims, k - 1)[:k]
    top_idx = top_idx[np.argsort(-sims[top_idx])]

    neighbors = [(_index.labels[i], float(sims[i])) for i in top_idx]
    if not neighbors or neighbors[0][1] < MIN_SIM:
        # Ничего близкого — отдаём фолбэк, но не хуже (оборудование из названия).
        fb = _fallback(name)
        if fb["main_muscle_group"] or fb["equipment_needed"]:
            return fb

    return _aggregate(neighbors, name)
