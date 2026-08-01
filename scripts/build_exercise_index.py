"""Офлайн-построитель индекса для авто-заполнения упражнений (запускать разово).

Собирает размеченный корпус (free-exercise-db + наш каталог), считает мультиязычные
эмбеддинги названий и складывает всё в статику, которую читает рантайм-классификатор:

    api/data/exercise_index.npy      — матрица векторов (N x D, float32, L2-normalized)
    api/data/exercise_index.json     — метки в том же порядке (main/secondary/equipment)
    api/data/embedder/model.onnx     — та же модель в ONNX (для вектора запроса в рантайме)
    api/data/embedder/*              — токенайзер

Требует (ставится только для этого офлайн-шага, в рантайм НЕ идёт):
    pip install "sentence-transformers" "optimum[onnxruntime]"

Запуск из корня репозитория:
    python -m scripts.build_exercise_index
    python -m scripts.build_exercise_index --no-quantize   # если int8 падает

Модель: paraphrase-multilingual-MiniLM-L12-v2 (RU+EN, 384-dim, mean pooling).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
from sqlalchemy import select

from app.database import SessionLocal
from api.services.models import Exercise
from api.services import exercise_taxonomy as tax

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_JSON = REPO_ROOT / "scripts" / ".cache" / "free-exercise-db" / "exercises.json"
DATA_DIR = REPO_ROOT / "api" / "data"
INDEX_VECS = DATA_DIR / "exercise_index.npy"
INDEX_LABELS = DATA_DIR / "exercise_index.json"
EMBEDDER_DIR = DATA_DIR / "embedder"


def _dataset_records() -> List[dict]:
    """free-exercise-db -> записи с русскими метками нашего словаря."""
    if not CACHE_JSON.exists():
        sys.exit(
            f"[build] Нет {CACHE_JSON}. Сначала скачай датасет:\n"
            f"  python -m scripts.ingest_exercise_images fetch"
        )
    dataset = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
    records: List[dict] = []
    for ex in dataset:
        primary = tax.dataset_muscles_to_ru(ex.get("primaryMuscles"))
        if not primary:
            continue  # без главной мышцы запись бесполезна как метка
        equip = tax.dataset_equipment_to_canon(ex.get("equipment"))
        records.append({
            "name": ex.get("name", ""),
            "main_muscle": primary[0],
            "secondary": tax.dataset_muscles_to_ru(ex.get("secondaryMuscles")),
            "equipment": [equip] if equip else [],
            "source": "dataset",
        })
    return records


async def _catalog_records() -> List[dict]:
    """Наш каталог (RU, уже в нашем словаре) -> записи корпуса."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Exercise).where(Exercise.app_user_id.is_(None))
        )).scalars().all()
    records: List[dict] = []
    for ex in rows:
        if not ex.name or not ex.main_muscle_group:
            continue
        records.append({
            "name": ex.name,
            "main_muscle": ex.main_muscle_group,
            "secondary": list(ex.secondary_muscle_groups or []),
            "equipment": list(ex.equipment_needed or []),
            "source": "catalog",
        })
    return records


def _embed(names: List[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)
    vecs = model.encode(
        names,
        normalize_embeddings=True,  # косинус == скалярное произведение
        convert_to_numpy=True,
        show_progress_bar=True,
        batch_size=64,
    )
    return vecs.astype(np.float32)


def _export_onnx(quantize: bool) -> None:
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer

    EMBEDDER_DIR.mkdir(parents=True, exist_ok=True)
    print("[build] Экспорт модели в ONNX…")
    ort = ORTModelForFeatureExtraction.from_pretrained(MODEL_NAME, export=True)
    ort.save_pretrained(EMBEDDER_DIR)
    AutoTokenizer.from_pretrained(MODEL_NAME).save_pretrained(EMBEDDER_DIR)

    if quantize:
        try:
            from optimum.onnxruntime import ORTQuantizer
            from optimum.onnxruntime.configuration import AutoQuantizationConfig

            print("[build] Динамическая int8-квантизация…")
            quantizer = ORTQuantizer.from_pretrained(EMBEDDER_DIR)
            qconfig = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
            quantizer.quantize(save_dir=EMBEDDER_DIR, quantization_config=qconfig)
            # optimum кладёт model_quantized.onnx — рантайм сам выберет его, если есть.
        except Exception as exc:  # noqa: BLE001
            print(f"[build] Квантизация пропущена ({exc}); остаётся fp32 model.onnx")


async def _amain(quantize: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    records = _dataset_records() + await _catalog_records()
    if not records:
        sys.exit("[build] Пустой корпус — нечего индексировать.")
    print(f"[build] Записей в корпусе: {len(records)} "
          f"(датасет+каталог)")

    vecs = _embed([r["name"] for r in records])
    np.save(INDEX_VECS, vecs)
    INDEX_LABELS.write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[build] Векторы: {vecs.shape} -> {INDEX_VECS}")
    print(f"[build] Метки -> {INDEX_LABELS}")

    _export_onnx(quantize)
    print("[build] Готово. Рантайм подхватит индекс и embedder из api/data/.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-quantize", action="store_true",
                        help="не квантовать ONNX (оставить fp32)")
    args = parser.parse_args()
    asyncio.run(_amain(quantize=not args.no_quantize))


if __name__ == "__main__":
    main()
