"""Авто-заполнение упражнения: по названию -> мышцы/вторичные/оборудование.

kNN по мультиязычным эмбеддингам над размеченным корпусом (free-exercise-db + наш
каталог), собранным офлайн в scripts/build_exercise_index.py. Вектор запроса считаем
в рантайме той же моделью через onnxruntime (без torch). Если артефакты/зависимости
недоступны — мягкий фолбэк на явный разбор названия (не хуже прежних словарей).

Артефакты (api/data/): exercise_index.npy, exercise_index.json, embedder/ (ONNX+токенайзер).
"""

from __future__ import annotations

import json
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

            self._session = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
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
    """Ищет оборудование в названии через словарь синонимов equipment.py.
    Пробегает окна из 1–3 слов ('машина смита', 'вес тела' и т.п.)."""
    words = [w for w in (name or "").lower().replace("ё", "е").split() if w]
    found: List[str] = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            canon = equip.normalize_equipment(" ".join(words[i:i + size]))
            if canon and canon not in found:
                found.append(canon)
    return found


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

# Минимальные ключевые слова RU -> наша мышца (только для фолбэка, не основной путь).
_FALLBACK_MUSCLE = [
    ("бицепс бедра", "Бицепсы ног"), ("бицепс", "Бицепс"), ("трицепс", "Трицепс"),
    ("груд", "Грудь"), ("жим лежа", "Грудь"), ("присед", "Квадрицепсы"),
    ("выпад", "Квадрицепсы"), ("икр", "Икры"), ("носки", "Икры"),
    ("ягод", "Ягодицы"), ("пресс", "Пресс"), ("скручив", "Пресс"),
    ("предплеч", "Предплечья"), ("широчайш", "Широчайшие"), ("тяг", "Широчайшие"),
    ("подтягив", "Широчайшие"), ("дельт", "Средняя дельта"), ("мах", "Средняя дельта"),
    ("трапец", "Трапеция"), ("шраг", "Трапеция"),
]


def _fallback(name: str) -> Dict:
    low = (name or "").lower().replace("ё", "е")
    main = None
    for kw, muscle in _FALLBACK_MUSCLE:
        if kw in low:
            main = muscle
            break
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
