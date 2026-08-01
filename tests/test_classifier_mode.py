"""Режим классификатора и его изоляция от event-loop.

Контекст: на 512 МБ Render ONNX-путь (модель + токенайзер на 250k токенов)
съедает ~900 МБ и процесс убивает OOM-killer. Штатный мягкий фолбэк при этом
НЕ спасает: он ловит Exception, а убитый процесс исключения не бросает.
Поэтому ONNX-путь должен включаться явно, а по умолчанию быть выключен.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import api.routers.exercises as exercises_router
from api.schemas.exercises import ExerciseClassifyRequest
from api.services import exercise_classifier as clf


# --- разбор CLASSIFIER_MODE -------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, False),          # переменная не задана -> безопасный дефолт
        ("", False),
        ("lexical", False),
        ("off", False),
        ("мусор", False),
        ("onnx", True),
        ("ONNX", True),
        ("  onnx  ", True),
    ],
)
def test_onnx_enabled_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CLASSIFIER_MODE", raising=False)
    else:
        monkeypatch.setenv("CLASSIFIER_MODE", value)
    assert clf._onnx_enabled() is expected


# --- эмбеддер не поднимается, пока режим не включён --------------------------

def test_embedder_not_ready_when_mode_off(monkeypatch):
    """Главное свойство: при выключенном режиме InferenceSession не создаётся.

    Если бы _load() дошёл до onnxruntime, тест бы съел ~500 МБ — именно этого
    мы и не хотим в проде.
    """
    monkeypatch.delenv("CLASSIFIER_MODE", raising=False)
    embedder = clf._Embedder()
    assert embedder.ready is False
    assert embedder._session is None
    assert embedder._tokenizer is None


def test_embedder_not_ready_when_artifacts_missing(monkeypatch, tmp_path):
    """Режим включён, но артефактов нет — по-прежнему мягкий отказ, не падение."""
    monkeypatch.setenv("CLASSIFIER_MODE", "onnx")
    monkeypatch.setattr(clf, "EMBEDDER_DIR", tmp_path)
    embedder = clf._Embedder()
    assert embedder.ready is False


# --- деградация остаётся осмысленной ----------------------------------------

def test_classify_falls_back_without_model(monkeypatch):
    """Без модели ответ хуже, но не пустой: что-то из названия всё же извлекается."""
    monkeypatch.delenv("CLASSIFIER_MODE", raising=False)
    monkeypatch.setattr(clf, "_embedder", clf._Embedder())

    result = clf.classify("приседания со штангой")

    assert result["source"] == "fallback"
    assert result["equipment_needed"] == ["barbell"]
    assert result["main_muscle_group"] == "Квадрицепсы"
    assert result["confidence"] == 0.0


@pytest.mark.parametrize(
    "name, muscle, equipment",
    [
        # Работает: ключевое слово попадает в _FALLBACK_MUSCLE как подстрока.
        ("жим лёжа", "Грудь", []),
        ("сгибания на бицепс с гантелями", "Бицепс", []),
        ("тяга верхнего блока", "Широчайшие", []),
        # Известные пробелы фолбэка — зафиксированы намеренно, чтобы регресс
        # был виден, а улучшение словаря сразу уронило тест и потребовало
        # осознанного обновления ожиданий.
        ("жим штанги лёжа", None, ["barbell"]),  # 'жим лежа' не подстрока
        ("французский жим", None, []),
        ("гиперэкстензия", None, []),
        ("подтягивания на турнике", "Широчайшие", []),  # 'турник' не опознан
    ],
)
def test_fallback_quality_is_documented(monkeypatch, name, muscle, equipment):
    monkeypatch.delenv("CLASSIFIER_MODE", raising=False)
    monkeypatch.setattr(clf, "_embedder", clf._Embedder())

    result = clf.classify(name)

    assert result["source"] == "fallback"
    assert result["main_muscle_group"] == muscle
    assert result["equipment_needed"] == equipment


def test_classify_empty_name_is_not_a_fallback(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_MODE", raising=False)
    monkeypatch.setattr(clf, "_embedder", clf._Embedder())
    assert clf.classify("   ")["source"] == "empty"


# --- эндпоинт не блокирует event-loop ---------------------------------------

def test_endpoint_runs_classify_off_the_event_loop():
    """classify() синхронный и может занять секунды (загрузка модели).

    В `async def`-эндпоинте прямой вызов заморозил бы весь воркер, а не только
    этот запрос. Проверяем, что работа уехала в threadpool: если бы вызов шёл
    напрямую, он исполнился бы в том же потоке, где крутится event-loop.
    """
    seen = {}

    def spy(name):
        seen["thread"] = threading.current_thread()
        return {"main_muscle_group": None, "secondary_muscle_groups": [],
                "equipment_needed": [], "confidence": 0.0, "source": "fallback"}

    async def call():
        loop_thread = threading.current_thread()
        resp = await exercises_router.classify_exercise(
            ExerciseClassifyRequest(name="жим лёжа"),
            app_user=SimpleNamespace(id=1),
        )
        return loop_thread, resp

    with patch.object(clf, "classify", spy):
        loop_thread, resp = asyncio.run(call())

    assert seen["thread"] is not loop_thread, "classify выполнился в потоке event-loop"
    assert resp.source == "fallback"
