"""Композиция тела: запись веса/%жира/замеров и обзор с историей и ИМТ.

Вес, % жира и рост — в UserAnthropometry (append-only снимок с датой).
Обхваты — в BodyMeasurement (key-value журнал). ИМТ вычисляем из последнего
веса и роста (информативно, не как цель).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import BodyMeasurement, UserAnthropometry

# Допустимые ключи замеров-обхватов (валидация на уровне приложения —
# init_db не обновляет CHECK-констрейнты).
BODY_METRIC_KEYS = {
    "waist",     # талия
    "chest",     # грудь
    "hips",      # бёдра (таз)
    "arm",       # рука (бицепс)
    "forearm",   # предплечье
    "thigh",     # бедро (нога)
    "calf",      # голень
    "neck",      # шея
    "shoulders", # плечи
}


def compute_bmi(weight: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    if not weight or not height_cm or height_cm <= 0:
        return None
    m = height_cm / 100.0
    return round(weight / (m * m), 1)


_FEMALE = {"female", "f", "ж", "женский", "woman"}


def navy_body_fat(
    gender: Optional[str],
    height: Optional[float],
    waist: Optional[float],
    neck: Optional[float],
    hips: Optional[float] = None,
) -> Optional[float]:
    """% жира по методу US Navy (обхваты в см). None, если данных не хватает.

    Муж: нужны талия, шея, рост. Жен: + бёдра. Результат зажат в [3, 60]%.
    """
    if not height or not waist or not neck:
        return None
    is_female = (gender or "").strip().lower() in _FEMALE
    try:
        if is_female:
            if not hips:
                return None
            if (waist + hips - neck) <= 0:
                return None
            denom = (
                1.29579
                - 0.35004 * math.log10(waist + hips - neck)
                + 0.22100 * math.log10(height)
            )
        else:
            if (waist - neck) <= 0:
                return None
            denom = (
                1.0324
                - 0.19077 * math.log10(waist - neck)
                + 0.15456 * math.log10(height)
            )
        if denom <= 0:
            return None
        bf = 495.0 / denom - 450.0
    except ValueError:
        return None
    return round(max(3.0, min(60.0, bf)), 1)


async def _latest_anthro(session: AsyncSession, user_id: int) -> Optional[UserAnthropometry]:
    return (await session.execute(
        select(UserAnthropometry)
        .where(UserAnthropometry.app_user_id == user_id)
        .order_by(desc(UserAnthropometry.recorded_at))
        .limit(1)
    )).scalars().first()


async def record_body_entry(
    session: AsyncSession,
    user_id: int,
    weight: Optional[float] = None,
    height: Optional[float] = None,
    body_fat: Optional[float] = None,
    measurements: Optional[Dict[str, float]] = None,
    gender: Optional[str] = None,
    client_uuid: Optional[str] = None,
) -> None:
    """Пишет снимок композиции. Вес/рост/жир — новой записью UserAnthropometry
    (недостающие поля переносим из последней записи, чтобы не терять их).
    Обхваты — отдельными записями BodyMeasurement.

    Если % жира не задан вручную, но пришли обхваты (талия/шея/+бёдра) и есть
    рост+пол — считаем его автоматически по US Navy (ручной ввод приоритетнее).
    """
    # Идемпотентность offline-повтора: если запись с этим client_uuid уже есть
    # (в любой из таблиц), считаем операцию применённой и выходим.
    if client_uuid:
        exists_anthro = (await session.execute(
            select(UserAnthropometry.id).where(
                UserAnthropometry.app_user_id == user_id,
                UserAnthropometry.client_uuid == client_uuid,
            )
        )).first()
        exists_measure = (await session.execute(
            select(BodyMeasurement.id).where(
                BodyMeasurement.app_user_id == user_id,
                BodyMeasurement.client_uuid == client_uuid,
            )
        )).first()
        if exists_anthro or exists_measure:
            return

    last = await _latest_anthro(session, user_id)
    eff_height = height if height is not None else (last.height if last else None)

    # Авторасчёт % жира из замеров (US Navy), если не задан вручную.
    resolved_fat = body_fat
    if resolved_fat is None and measurements:
        resolved_fat = navy_body_fat(
            gender,
            eff_height,
            measurements.get("waist"),
            measurements.get("neck"),
            measurements.get("hips"),
        )

    if weight is not None or height is not None or resolved_fat is not None:
        session.add(UserAnthropometry(
            app_user_id=user_id,
            weight=weight if weight is not None else (last.weight if last else None),
            height=eff_height,
            body_fat=resolved_fat if resolved_fat is not None else (last.body_fat if last else None),
            birth_date=last.birth_date if last else None,
            activity_level=last.activity_level if last else None,
            client_uuid=client_uuid,
        ))

    for key, value in (measurements or {}).items():
        if key in BODY_METRIC_KEYS and value is not None:
            session.add(BodyMeasurement(
                app_user_id=user_id, metric_key=key, value=float(value),
                client_uuid=client_uuid,
            ))

    await session.commit()


async def get_metric_series(
    session: AsyncSession, user_id: int, metric_key: str
) -> List[tuple]:
    """Ряд (date, value) по метрике композиции — для трендов и целей.

    weight/body_fat берём из UserAnthropometry, обхваты — из BodyMeasurement.
    """
    from datetime import date as _date

    series: List[tuple] = []
    if metric_key in ("weight", "body_fat"):
        rows = list((await session.execute(
            select(UserAnthropometry)
            .where(UserAnthropometry.app_user_id == user_id)
            .order_by(UserAnthropometry.recorded_at)
        )).scalars().all())
        for r in rows:
            v = r.weight if metric_key == "weight" else r.body_fat
            if v is not None and r.recorded_at is not None:
                series.append((r.recorded_at.date(), float(v)))
    else:
        rows = list((await session.execute(
            select(BodyMeasurement)
            .where(
                BodyMeasurement.app_user_id == user_id,
                BodyMeasurement.metric_key == metric_key,
            )
            .order_by(BodyMeasurement.recorded_at)
        )).scalars().all())
        for r in rows:
            if r.recorded_at is not None:
                series.append((r.recorded_at.date(), float(r.value)))
    return series


async def get_body_entries(session: AsyncSession, user_id: int) -> List[dict]:
    """История записей — снимки, сгруппированные по дате (свежие сверху).

    Вес/жир берём из UserAnthropometry, обхваты из BodyMeasurement за ту же
    дату; в пределах дня берём последнее значение каждой метрики.
    """
    anthro = list((await session.execute(
        select(UserAnthropometry)
        .where(UserAnthropometry.app_user_id == user_id)
        .order_by(UserAnthropometry.recorded_at)
    )).scalars().all())
    meas = list((await session.execute(
        select(BodyMeasurement)
        .where(BodyMeasurement.app_user_id == user_id)
        .order_by(BodyMeasurement.recorded_at)
    )).scalars().all())

    by_date: Dict[str, dict] = {}

    def bucket(dt) -> Optional[dict]:
        if dt is None:
            return None
        key = dt.date().isoformat()
        return by_date.setdefault(
            key, {"date": key, "weight": None, "body_fat": None, "measurements": {}}
        )

    for a in anthro:
        b = bucket(a.recorded_at)
        if b is None:
            continue
        if a.weight is not None:
            b["weight"] = round(float(a.weight), 1)
        if a.body_fat is not None:
            b["body_fat"] = round(float(a.body_fat), 1)
    for m in meas:
        b = bucket(m.recorded_at)
        if b is None:
            continue
        b["measurements"][m.metric_key] = round(float(m.value), 1)

    return sorted(by_date.values(), key=lambda e: e["date"], reverse=True)


async def get_body_overview(session: AsyncSession, user_id: int) -> dict:
    """Последние значения + история по каждой метрике + ИМТ."""
    anthro_rows = list((await session.execute(
        select(UserAnthropometry)
        .where(UserAnthropometry.app_user_id == user_id)
        .order_by(UserAnthropometry.recorded_at)
    )).scalars().all())

    meas_rows = list((await session.execute(
        select(BodyMeasurement)
        .where(BodyMeasurement.app_user_id == user_id)
        .order_by(BodyMeasurement.recorded_at)
    )).scalars().all())

    history: Dict[str, List[dict]] = {}

    def push(key: str, dt, value):
        if value is None:
            return
        history.setdefault(key, []).append(
            {"date": dt.date().isoformat() if dt else None, "value": round(float(value), 1)}
        )

    for a in anthro_rows:
        push("weight", a.recorded_at, a.weight)
        push("body_fat", a.recorded_at, a.body_fat)
    for m in meas_rows:
        push(m.metric_key, m.recorded_at, m.value)

    latest = anthro_rows[-1] if anthro_rows else None
    latest_weight = latest.weight if latest else None
    latest_height = latest.height if latest else None
    latest_body_fat = latest.body_fat if latest else None

    # Последние значения замеров — по последней записи каждого ключа.
    latest_measurements: Dict[str, float] = {}
    for m in meas_rows:
        latest_measurements[m.metric_key] = round(float(m.value), 1)

    return {
        "latest_weight": latest_weight,
        "latest_height": latest_height,
        "latest_body_fat": latest_body_fat,
        "bmi": compute_bmi(latest_weight, latest_height),
        "measurements": latest_measurements,
        "history": history,
    }
