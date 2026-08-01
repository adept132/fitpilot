"""Ингестор изображений техники из free-exercise-db в нашу базу упражнений.

Датасет: https://github.com/yuhonas/free-exercise-db (лицензия Unlicense / public
domain, атрибуция не требуется). ~800 упражнений, у каждого 2 картинки (start/end).

Проблема: наша база — на русском, датасет — на английском. Прямой матчинг по имени
между языками ненадёжен, а неверная картинка техники хуже её отсутствия. Поэтому
сопоставление — с ВИЗУАЛЬНЫМ ревью: скрипт готовит кандидатов и показывает их
картинки техники, человек подтверждает выбор глазами.

Три режима:

    python -m scripts.ingest_exercise_images fetch
        Один раз качает dist/exercises.json и все картинки в локальный кэш
        (scripts/.cache/free-exercise-db/). Дальше в GitHub не ходим.

    python -m scripts.ingest_exercise_images suggest
        Строит топ-кандидатов для каждого нашего упражнения (фильтр по мышце +
        якоря из strong_dictionary + оборудование + fuzzy) и генерирует
        самодостаточную страницу scripts/review.html с картинками кандидатов и
        выбором. Кнопка на странице выгружает mapping.json.

    python -m scripts.ingest_exercise_images apply
        Копирует картинки выбранных кандидатов в media/exercises/<id>/ и проставляет
        Exercise.image_urls. Идемпотентно. Без аргумента берёт scripts/mapping.json;
        можно указать свой путь: `apply путь/к/mapping.json` (без квадратных скобок!).

    python -m scripts.ingest_exercise_images propagate
        Для упражнений БЕЗ картинки ищет «родственника» (то же движение, другое
        оборудование) среди уже сматченных и генерирует scripts/review-propagate.html
        с картинкой родственника на подтверждение. Кнопка выгружает propagation.json.

    python -m scripts.ingest_exercise_images apply-propagation
        Переносит картинки подтверждённых родственников в media/ и проставляет
        image_urls. Запускать ПОСЛЕ apply (доноры должны уже иметь картинки).

Запускать из корня репозитория (…/FitPilotBot).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select

from app.database import SessionLocal
from api.services.models import Exercise
from api.services.equipment import normalize_equipment_list
from api.services.exercise_matcher import ExerciseMatcher
from api.services import strong_dictionary as sd

# --- Пути ---
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
CACHE_DIR = SCRIPTS_DIR / ".cache" / "free-exercise-db"
CACHE_JSON = CACHE_DIR / "exercises.json"
CACHE_IMAGES = CACHE_DIR / "exercises"
MEDIA_DIR = REPO_ROOT / "media"
REVIEW_HTML = SCRIPTS_DIR / "review.html"
DEFAULT_MAPPING = SCRIPTS_DIR / "mapping.json"
PROPAGATE_HTML = SCRIPTS_DIR / "review-propagate.html"
DEFAULT_PROPAGATION = SCRIPTS_DIR / "propagation.json"

# --- Источник ---
RAW_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main"
EXERCISES_JSON_URL = f"{RAW_BASE}/dist/exercises.json"
IMAGE_BASE = f"{RAW_BASE}/exercises"

TOP_N = 6  # сколько кандидатов показывать на упражнение

# --- Мышцы: наши RU (main_muscle_group) -> primaryMuscles датасета (EN) ---
MUSCLE_RU_TO_EN: Dict[str, Set[str]] = {
    "грудь": {"chest"},
    "широчайшие": {"lats"},
    "средняя часть спины": {"middle back"},
    "середина спины": {"middle back"},
    "трапеция": {"traps"},
    "трапеции": {"traps"},
    "поясница": {"lower back"},
    "передняя дельта": {"shoulders"},
    "средняя дельта": {"shoulders"},
    "задняя дельта": {"shoulders"},
    "дельты": {"shoulders"},
    "плечи": {"shoulders"},
    "бицепс": {"biceps"},
    "трицепс": {"triceps"},
    "предплечья": {"forearms"},
    "предплечье": {"forearms"},
    "квадрицепсы": {"quadriceps"},
    "квадрицепс": {"quadriceps"},
    "бицепсы ног": {"hamstrings"},
    "бицепс бедра": {"hamstrings"},
    "ягодицы": {"glutes"},
    "ягодичные": {"glutes"},
    "аддукторы": {"adductors"},
    "абдукторы": {"abductors"},
    "икры": {"calves"},
    "пресс": {"abdominals"},
    "шея": {"neck"},
}

# --- Оборудование: значение датасета -> наши канонические коды (набор) ---
DATASET_EQUIP_TO_CANON: Dict[str, Set[str]] = {
    "barbell": {"barbell"},
    "dumbbell": {"dumbbell"},
    "cable": {"block_machine"},
    "machine": {"free_machine", "block_machine"},
    "body only": {"bodyweight"},
    "kettlebells": {"kettlebell"},
    "bands": {"band"},
    "e-z curl bar": {"barbell"},
    # medicine ball / exercise ball / foam roll / other / none -> нет соответствия
}


def _norm(s: str) -> str:
    """Нормализация для fuzzy: нижний регистр, ё->е, схлопнутые пробелы."""
    return " ".join((s or "").lower().replace("ё", "е").split())


def _build_reverse_strong() -> Dict[str, str]:
    """RU (нормализованное имя) -> EN-подсказка из strong_dictionary.

    Собираем из обоих словарей модуля (_GENERIC и _BY_EQUIPMENT). Подсказка нужна,
    чтобы fuzzy-сравнивать наше упражнение с англоязычными именами датасета —
    иначе кросс-язычное сравнение бессмысленно.
    """
    reverse: Dict[str, str] = {}

    def add(en: str, ru: str) -> None:
        key = _norm(ru)
        # Оставляем более короткую (обычно более «каноничную») EN-подсказку.
        if key not in reverse or len(en) < len(reverse[key]):
            reverse[key] = _norm(en)

    for en_key, ru in getattr(sd, "_GENERIC", {}).items():
        add(en_key, ru)
    for (en_name, _eq), ru in getattr(sd, "_BY_EQUIPMENT", {}).items():
        add(en_name, ru)

    return reverse


REVERSE_STRONG = _build_reverse_strong()


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path) -> Tuple[Path, Optional[str]]:
    """Скачивает url в dest (если ещё нет). Возвращает (dest, error|None)."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest, None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eurith-ingest"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest, None
    except Exception as exc:  # noqa: BLE001 — логируем и продолжаем остальные
        return dest, str(exc)


def cmd_fetch() -> None:
    print(f"[fetch] Скачиваю каталог -> {CACHE_JSON}")
    _, err = _download(EXERCISES_JSON_URL, CACHE_JSON)
    if err:
        sys.exit(f"[fetch] Не удалось скачать exercises.json: {err}")

    dataset = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
    image_paths: List[str] = []
    for ex in dataset:
        for rel in ex.get("images", []) or []:
            image_paths.append(rel)

    print(f"[fetch] Упражнений: {len(dataset)}, картинок к загрузке: {len(image_paths)}")

    ok = 0
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(_download, f"{IMAGE_BASE}/{rel}", CACHE_IMAGES / rel): rel
            for rel in image_paths
        }
        for i, fut in enumerate(as_completed(futures), 1):
            rel = futures[fut]
            _, err = fut.result()
            if err:
                failed.append(f"{rel}: {err}")
            else:
                ok += 1
            if i % 200 == 0:
                print(f"[fetch] …{i}/{len(image_paths)}")

    print(f"[fetch] Готово. Успешно/уже было: {ok}, ошибок: {len(failed)}")
    for line in failed[:20]:
        print(f"  ! {line}")
    if len(failed) > 20:
        print(f"  … ещё {len(failed) - 20}")


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------

def _load_dataset() -> List[dict]:
    if not CACHE_JSON.exists():
        sys.exit("[suggest] Нет кэша каталога. Сначала: fetch")
    return json.loads(CACHE_JSON.read_text(encoding="utf-8"))


def _score_candidate(
    ex: Exercise, hint: Optional[str], cand: dict, our_muscles: Set[str]
) -> Tuple[float, bool, bool]:
    """Оценка кандидата + флаги совпадения по primary/secondary мышце (для фильтра)."""
    primary = {m.lower() for m in (cand.get("primaryMuscles") or [])}
    secondary = {m.lower() for m in (cand.get("secondaryMuscles") or [])}
    primary_hit = bool(our_muscles & primary)
    secondary_hit = bool(our_muscles & secondary)

    score = 0.0
    # Имя: только если есть EN-подсказка (иначе RU vs EN бессмысленно).
    if hint:
        ratio = SequenceMatcher(None, hint, _norm(cand.get("name", ""))).ratio()
        score += 50.0 * ratio
    # Оборудование.
    our_equip = set(normalize_equipment_list(ex.equipment_needed))
    cand_equip = DATASET_EQUIP_TO_CANON.get((cand.get("equipment") or "").lower(), set())
    if our_equip & cand_equip:
        score += 15.0
    # Совпадение по мышце приподнимает кандидата (primary весомее secondary).
    if primary_hit:
        score += 10.0
    elif secondary_hit:
        score += 4.0
    return score, primary_hit, secondary_hit


def _candidates_for(ex: Exercise, dataset: List[dict]) -> List[Tuple[dict, float]]:
    """Топ-N авто-кандидатов. Фильтр по мышце: primary ИЛИ secondary (мягче), чтобы
    ловить упражнения, которые датасет отнёс к другой главной мышце. Что не попало
    сюда, но точно есть — добирается ручным поиском по всей базе в review.html."""
    our_muscles = MUSCLE_RU_TO_EN.get(_norm(ex.main_muscle_group), set())
    hint = REVERSE_STRONG.get(_norm(ex.name))

    scored: List[Tuple[dict, float]] = []
    for cand in dataset:
        if not cand.get("images"):
            continue  # без картинок кандидат бесполезен
        score, primary_hit, secondary_hit = _score_candidate(ex, hint, cand, our_muscles)
        # Когда наша мышца известна — оставляем совпавших по primary или secondary.
        if our_muscles and not (primary_hit or secondary_hit):
            continue
        scored.append((cand, score))

    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:TOP_N]


def _img_rel_for_html(dataset_image_path: str) -> str:
    """Путь к картинке кэша относительно review.html (в scripts/)."""
    return f".cache/free-exercise-db/exercises/{dataset_image_path}".replace("\\", "/")


async def cmd_suggest() -> None:
    dataset = _load_dataset()
    print(f"[suggest] Каталог: {len(dataset)} упражнений")

    async with SessionLocal() as session:
        result = await session.execute(
            select(Exercise)
            .where(Exercise.app_user_id.is_(None))
            .order_by(Exercise.main_muscle_group, Exercise.name)
        )
        our_exercises = list(result.scalars().all())

    print(f"[suggest] Наших системных упражнений: {len(our_exercises)}")

    rows_html: List[str] = []
    matched_any = 0
    for ex in our_exercises:
        cands = _candidates_for(ex, dataset)
        if cands:
            matched_any += 1
        rows_html.append(_render_exercise_block(ex, cands))

    page = _render_page(rows_html, dataset)
    REVIEW_HTML.write_text(page, encoding="utf-8")
    print(
        f"[suggest] Кандидаты хотя бы для {matched_any}/{len(our_exercises)} упражнений.\n"
        f"[suggest] Открой в браузере: {REVIEW_HTML}\n"
        f"[suggest] Отметь верные картинки и нажми «Скачать mapping.json»."
    )


def _render_candidate_card(ex_id: int, cand: dict, score: float) -> str:
    imgs = "".join(
        f'<img loading="lazy" src="{html.escape(_img_rel_for_html(p))}" alt="">'
        for p in (cand.get("images") or [])
    )
    cand_id = html.escape(cand.get("id", ""))
    name = html.escape(cand.get("name", ""))
    equip = html.escape(cand.get("equipment") or "—")
    muscles = html.escape(", ".join(cand.get("primaryMuscles") or []))
    return f"""
      <label class="cand">
        <input type="radio" name="ex_{ex_id}" value="{cand_id}">
        <div class="imgs">{imgs}</div>
        <div class="meta">
          <div class="cand-name">{name}</div>
          <div class="cand-sub">{equip} · {muscles}</div>
          <div class="cand-score">score {score:.0f}</div>
        </div>
      </label>"""


def _render_exercise_block(ex: Exercise, cands: List[Tuple[dict, float]]) -> str:
    name = html.escape(ex.name)
    muscle = html.escape(ex.main_muscle_group or "")
    equip = html.escape(", ".join(ex.equipment_needed or []) or "—")
    cards = "".join(_render_candidate_card(ex.id, c, s) for c, s in cands)
    if not cards:
        cards = '<div class="empty">Кандидаты по мышце не найдены</div>'
    return f"""
    <section class="ex" data-ex="{ex.id}">
      <div class="ex-head">
        <div class="ex-title">
          <span class="ex-name">{name}</span>
          <span class="ex-sub">#{ex.id} · {muscle} · {equip}</span>
        </div>
        <button type="button" class="search-btn" onclick="openSearch({ex.id})">
          🔍 вся база
        </button>
      </div>
      <div class="cands">
        {cards}
        <label class="cand none">
          <input type="radio" name="ex_{ex.id}" value="" checked>
          <div class="none-box">нет подходящих</div>
        </label>
      </div>
    </section>"""


def _render_page(rows_html: List[str], dataset: List[dict]) -> str:
    body = "\n".join(rows_html)
    # Компактный датасет для ручного поиска по всей базе прямо в странице.
    embed = [
        {
            "id": e["id"],
            "name": e.get("name", ""),
            "eq": e.get("equipment") or "",
            "imgs": [_img_rel_for_html(p) for p in (e.get("images") or [])],
        }
        for e in dataset
        if e.get("images")
    ]
    # "<\\/" — чтобы возможный "</script>" в данных не закрыл тег раньше времени.
    dataset_js = json.dumps(embed, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eurith · ревью картинок техники</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px 16px 96px; }}
  h1 {{ font-size: 18px; }}
  .ex {{ border: 1px solid #8883; border-radius: 12px; padding: 12px; margin: 12px 0; }}
  .ex-head {{ display: flex; align-items: flex-start; justify-content: space-between;
              gap: 12px; margin-bottom: 10px; }}
  .ex-title {{ display: flex; flex-direction: column; gap: 2px; }}
  .ex-name {{ font-weight: 800; font-size: 16px; }}
  .ex-sub {{ opacity: .6; font-size: 12px; }}
  .search-btn {{ background: #8883; color: inherit; padding: 6px 10px; font-size: 12px;
                 font-weight: 700; white-space: nowrap; }}
  .cands {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .cand {{ display: block; border: 2px solid #8884; border-radius: 10px; padding: 6px;
           cursor: pointer; width: 200px; }}
  .cand input {{ margin-bottom: 6px; }}
  .cand:has(input:checked) {{ border-color: #f08a24; background: #f08a2422; }}
  .cand.manual {{ border-color: #f08a24; background: #f08a2422; }}
  .imgs {{ display: flex; gap: 4px; }}
  .imgs img {{ width: 90px; height: 90px; object-fit: cover; border-radius: 6px;
               background: #8882; }}
  .cand-name {{ font-weight: 700; font-size: 13px; margin-top: 4px; }}
  .cand-sub {{ opacity: .6; font-size: 11px; }}
  .cand-score {{ opacity: .5; font-size: 10px; }}
  .none {{ width: 140px; display: flex; align-items: center; }}
  .none-box {{ opacity: .6; font-size: 13px; }}
  .empty {{ opacity: .5; font-size: 13px; padding: 8px 0; }}
  .bar {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 12px 16px;
          background: Canvas; border-top: 1px solid #8884; display: flex;
          align-items: center; gap: 12px; }}
  button {{ background: #f08a24; color: #111; border: 0; border-radius: 10px;
            padding: 10px 16px; font-weight: 800; font-size: 14px; cursor: pointer; }}
  #count {{ opacity: .7; font-size: 13px; }}
  .modal {{ position: fixed; inset: 0; background: #0008; z-index: 50;
            align-items: center; justify-content: center; }}
  .modal-box {{ background: Canvas; width: min(920px, 94vw); max-height: 88vh;
                border-radius: 12px; padding: 12px; display: flex; flex-direction: column; }}
  .modal-head {{ display: flex; gap: 8px; margin-bottom: 10px; }}
  .modal-head input {{ flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #8884;
                       font-size: 14px; background: transparent; color: inherit; }}
  .results {{ overflow: auto; display: grid;
              grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }}
  .result {{ border: 1px solid #8884; border-radius: 10px; padding: 6px; }}
  .result button {{ margin-top: 6px; width: 100%; padding: 8px; font-size: 13px; }}
</style></head>
<body>
  <h1>Ревью картинок техники — отметь верного кандидата, «нет подходящих» или найди в 🔍 всей базе</h1>
  {body}
  <div class="bar">
    <button onclick="downloadMapping()">Скачать mapping.json</button>
    <span id="count"></span>
  </div>

  <div id="searchModal" class="modal">
    <div class="modal-box">
      <div class="modal-head">
        <input id="searchInput" placeholder="Поиск по английскому названию во всей базе…"
               oninput="filterSearch()">
        <button type="button" onclick="closeSearch()">Закрыть</button>
      </div>
      <div id="searchResults" class="results"></div>
    </div>
  </div>

<script>
  const DATASET = {dataset_js};
  let activeEx = null;

  function esc(s) {{
    return (s || '').replace(/[&<>]/g, function (c) {{
      return {{'&': '&amp;', '<': '&lt;', '>': '&gt;'}}[c];
    }});
  }}
  function imgsHtml(list) {{
    return (list || []).map(function (u) {{
      return '<img loading="lazy" src="' + u + '">';
    }}).join('');
  }}

  function collect() {{
    const map = {{}};
    document.querySelectorAll('section.ex').forEach(function (sec) {{
      const id = sec.getAttribute('data-ex');
      const chosen = sec.querySelector('input[name="ex_' + id + '"]:checked');
      if (chosen && chosen.value) map[id] = chosen.value;
    }});
    return map;
  }}
  function refresh() {{
    const n = Object.keys(collect()).length;
    document.getElementById('count').textContent = n + ' сопоставлено';
  }}
  document.addEventListener('change', refresh);

  function downloadMapping() {{
    const blob = new Blob([JSON.stringify(collect(), null, 2)], {{type: 'application/json'}});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'mapping.json';
    a.click();
  }}

  // --- Ручной поиск по всей базе (для «нет подходящих», хотя картинка точно есть) ---
  function openSearch(exId) {{
    activeEx = exId;
    document.getElementById('searchModal').style.display = 'flex';
    const input = document.getElementById('searchInput');
    input.value = '';
    filterSearch();
    input.focus();
  }}
  function closeSearch() {{
    document.getElementById('searchModal').style.display = 'none';
  }}
  function filterSearch() {{
    const q = document.getElementById('searchInput').value.trim().toLowerCase();
    const box = document.getElementById('searchResults');
    if (!q) {{
      box.innerHTML = '<div class="empty">Введите часть английского названия…</div>';
      return;
    }}
    const hits = DATASET.filter(function (e) {{
      return e.name.toLowerCase().includes(q);
    }}).slice(0, 40);
    box.innerHTML = hits.map(function (e) {{
      return '<div class="result">' + '<div class="imgs">' + imgsHtml(e.imgs) + '</div>'
        + '<div class="cand-name">' + esc(e.name) + '</div>'
        + '<div class="cand-sub">' + esc(e.eq) + '</div>'
        + '<button type="button" onclick="pickManual(\\'' + e.id + '\\')">Выбрать</button>'
        + '</div>';
    }}).join('') || '<div class="empty">Ничего не найдено</div>';
  }}
  function pickManual(id) {{
    const e = DATASET.find(function (x) {{ return x.id === id; }});
    if (!e || activeEx === null) return;
    const sec = document.querySelector('section.ex[data-ex="' + activeEx + '"]');
    sec.querySelectorAll('.cand.manual').forEach(function (n) {{ n.remove(); }});
    const label = document.createElement('label');
    label.className = 'cand manual';
    label.innerHTML =
      '<input type="radio" name="ex_' + activeEx + '" value="' + id + '" checked>'
      + '<div class="imgs">' + imgsHtml(e.imgs) + '</div>'
      + '<div class="cand-name">' + esc(e.name) + '</div>'
      + '<div class="cand-sub">' + esc(e.eq) + ' · вручную</div>';
    const cands = sec.querySelector('.cands');
    cands.insertBefore(label, cands.firstChild);
    closeSearch();
    refresh();
  }}

  refresh();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _resolve_json_path(raw: str, default: Path) -> Optional[Path]:
    """Находит JSON-файл, прощая типичные оплошности: квадратные скобки из справки
    (`[mapping.json]`) и запуск из корня, когда файл лежит в scripts/."""
    cleaned = raw.strip().strip("[]").strip().strip('"').strip("'")
    p = Path(cleaned)
    candidates = [p]
    if not p.is_absolute():
        candidates += [Path.cwd() / cleaned, SCRIPTS_DIR / cleaned, REPO_ROOT / cleaned]
    candidates.append(default)  # запасной вариант — scripts/<default>

    seen: set = set()
    for c in candidates:
        key = str(c.resolve())
        if key in seen:
            continue
        seen.add(key)
        if c.exists():
            return c
    return None


async def cmd_apply(mapping_arg: str) -> None:
    mapping_path = _resolve_json_path(mapping_arg, DEFAULT_MAPPING)
    if mapping_path is None:
        looked = "\n  ".join(
            str(x) for x in [mapping_arg, Path.cwd() / mapping_arg, DEFAULT_MAPPING]
        )
        sys.exit(
            f"[apply] Не нашёл mapping.json. Искал (в т.ч.):\n  {looked}\n"
            f"Подсказка: запусти `apply` без аргумента — возьмётся {DEFAULT_MAPPING}."
        )
    print(f"[apply] Использую маппинг: {mapping_path}")
    mapping: Dict[str, str] = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not mapping:
        sys.exit("[apply] Маппинг пуст — нечего применять.")

    dataset = {ex["id"]: ex for ex in _load_dataset()}
    print(f"[apply] Записей в маппинге: {len(mapping)}")

    applied = 0
    skipped: List[str] = []

    async with SessionLocal() as session:
        for our_id_str, dataset_id in mapping.items():
            if not dataset_id:
                continue
            our_id = int(our_id_str)
            cand = dataset.get(dataset_id)
            if not cand:
                skipped.append(f"{our_id}: нет кандидата {dataset_id}")
                continue

            ex = await session.get(Exercise, our_id)
            if not ex:
                skipped.append(f"{our_id}: нет упражнения в БД")
                continue

            dest_dir = MEDIA_DIR / "exercises" / str(our_id)
            dest_dir.mkdir(parents=True, exist_ok=True)

            rel_urls: List[str] = []
            for i, src_rel in enumerate(cand.get("images") or []):
                src = CACHE_IMAGES / src_rel
                if not src.exists():
                    skipped.append(f"{our_id}: нет картинки в кэше {src_rel}")
                    continue
                dest = dest_dir / f"{i}.jpg"
                shutil.copyfile(src, dest)
                rel_urls.append(f"exercises/{our_id}/{i}.jpg")

            if not rel_urls:
                skipped.append(f"{our_id}: не скопировано ни одной картинки")
                continue

            ex.image_urls = rel_urls
            ex.image_approx = False  # прямое сопоставление с датасетом — точная техника
            applied += 1

        await session.commit()

    print(f"[apply] Обновлено упражнений: {applied}, пропущено: {len(skipped)}")
    for line in skipped[:30]:
        print(f"  ! {line}")


# ---------------------------------------------------------------------------
# propagate — перенос картинки на родственника (то же движение, др. оборудование)
# ---------------------------------------------------------------------------

def _enum_val(v) -> str:
    """Строковое значение enum/строки классификатора (action/vector/laterality)."""
    return str(getattr(v, "value", None) or getattr(v, "name", None) or v or "")


def _movement_known(ex: Exercise) -> bool:
    return all(
        _enum_val(g) not in ("", "unknown")
        for g in (ex.action, ex.vector, ex.laterality)
    )


def _base_name(name: str) -> str:
    # Нормализация уже унифицирует гантели↔штанга и машина↔тренажер — ровно то,
    # что нужно, чтобы варианты одного движения на разном оборудовании сходились.
    return ExerciseMatcher._normalize_string(name or "")


def _propagate_candidates(
    target: Exercise, donors: List[Exercise]
) -> List[Tuple[Exercise, float]]:
    """Доноры (наши упражнения С картинкой) того же движения, что и target."""
    t_base = _base_name(target.name)
    t_move = (
        _enum_val(target.action),
        _enum_val(target.vector),
        _enum_val(target.laterality),
    )
    t_known = _movement_known(target)

    scored: List[Tuple[Exercise, float]] = []
    for d in donors:
        if d.id == target.id or d.main_muscle_group != target.main_muscle_group:
            continue
        base = SequenceMatcher(None, t_base, _base_name(d.name)).ratio()
        d_move = (_enum_val(d.action), _enum_val(d.vector), _enum_val(d.laterality))
        move_match = t_known and _movement_known(d) and t_move == d_move
        # Отсекаем совсем далёкие: либо похоже имя, либо тот же паттерн движения.
        if base < 0.45 and not move_match:
            continue
        score = 100.0 * base + (25.0 if move_match else 0.0)
        scored.append((d, score))

    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:TOP_N]


def _donor_img_rel(url: str) -> str:
    """Картинка донора из media/ относительно review-propagate.html (в scripts/)."""
    return f"../media/{url.lstrip('/')}".replace("\\", "/")


def _render_donor_card(target_id: int, donor: Exercise, score: float) -> str:
    imgs = "".join(
        f'<img loading="lazy" src="{html.escape(_donor_img_rel(u))}" alt="">'
        for u in (donor.image_urls or [])
    )
    name = html.escape(donor.name)
    equip = html.escape(", ".join(donor.equipment_needed or []) or "—")
    return f"""
      <label class="cand">
        <input type="radio" name="ex_{target_id}" value="{donor.id}">
        <div class="imgs">{imgs}</div>
        <div class="meta">
          <div class="cand-name">{name}</div>
          <div class="cand-sub">#{donor.id} · {equip}</div>
          <div class="cand-score">score {score:.0f}</div>
        </div>
      </label>"""


def _render_target_block(target: Exercise, donors: List[Tuple[Exercise, float]]) -> str:
    name = html.escape(target.name)
    muscle = html.escape(target.main_muscle_group or "")
    equip = html.escape(", ".join(target.equipment_needed or []) or "—")
    cards = "".join(_render_donor_card(target.id, d, s) for d, s in donors)
    if not cards:
        cards = '<div class="empty">Родственников с картинкой не найдено</div>'
    return f"""
    <section class="ex" data-ex="{target.id}">
      <div class="ex-head">
        <div class="ex-title">
          <span class="ex-name">{name}</span>
          <span class="ex-sub">#{target.id} · {muscle} · {equip}</span>
        </div>
        <button type="button" class="search-btn" onclick="openSearch({target.id})">
          🔍 вся база
        </button>
      </div>
      <div class="cands">
        {cards}
        <label class="cand none">
          <input type="radio" name="ex_{target.id}" value="" checked>
          <div class="none-box">не переносить</div>
        </label>
      </div>
    </section>"""


def _render_propagate_page(rows_html: List[str], donors: List[Exercise]) -> str:
    body = "\n".join(rows_html)
    # Все доноры (наши упражнения С картинкой) для ручного поиска по всей базе.
    embed = [
        {
            "id": d.id,
            "name": d.name,
            "eq": ", ".join(d.equipment_needed or []) or "",
            "imgs": [_donor_img_rel(u) for u in (d.image_urls or [])],
        }
        for d in donors
    ]
    donors_js = json.dumps(embed, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eurith · перенос картинок на родственников</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px 16px 96px; }}
  h1 {{ font-size: 18px; }}
  .ex {{ border: 1px solid #8883; border-radius: 12px; padding: 12px; margin: 12px 0; }}
  .ex-head {{ display: flex; align-items: flex-start; justify-content: space-between;
              gap: 12px; margin-bottom: 10px; }}
  .ex-title {{ display: flex; flex-direction: column; gap: 2px; }}
  .ex-name {{ font-weight: 800; font-size: 16px; }}
  .ex-sub {{ opacity: .6; font-size: 12px; }}
  .search-btn {{ background: #8883; color: inherit; padding: 6px 10px; font-size: 12px;
                 font-weight: 700; white-space: nowrap; }}
  .cands {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .cand {{ display: block; border: 2px solid #8884; border-radius: 10px; padding: 6px;
           cursor: pointer; width: 200px; }}
  .cand input {{ margin-bottom: 6px; }}
  .cand:has(input:checked) {{ border-color: #f08a24; background: #f08a2422; }}
  .cand.manual {{ border-color: #f08a24; background: #f08a2422; }}
  .imgs {{ display: flex; gap: 4px; }}
  .imgs img {{ width: 90px; height: 90px; object-fit: cover; border-radius: 6px;
               background: #8882; }}
  .cand-name {{ font-weight: 700; font-size: 13px; margin-top: 4px; }}
  .cand-sub {{ opacity: .6; font-size: 11px; }}
  .cand-score {{ opacity: .5; font-size: 10px; }}
  .none {{ width: 140px; display: flex; align-items: center; }}
  .none-box {{ opacity: .6; font-size: 13px; }}
  .empty {{ opacity: .5; font-size: 13px; padding: 8px 0; }}
  .bar {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 12px 16px;
          background: Canvas; border-top: 1px solid #8884; display: flex;
          align-items: center; gap: 12px; }}
  button {{ background: #f08a24; color: #111; border: 0; border-radius: 10px;
            padding: 10px 16px; font-weight: 800; font-size: 14px; cursor: pointer; }}
  #count {{ opacity: .7; font-size: 13px; }}
  .modal {{ position: fixed; inset: 0; background: #0008; z-index: 50;
            align-items: center; justify-content: center; }}
  .modal-box {{ background: Canvas; width: min(920px, 94vw); max-height: 88vh;
                border-radius: 12px; padding: 12px; display: flex; flex-direction: column; }}
  .modal-head {{ display: flex; gap: 8px; margin-bottom: 10px; }}
  .modal-head input {{ flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #8884;
                       font-size: 14px; background: transparent; color: inherit; }}
  .results {{ overflow: auto; display: grid;
              grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }}
  .result {{ border: 1px solid #8884; border-radius: 10px; padding: 6px; }}
  .result button {{ margin-top: 6px; width: 100%; padding: 8px; font-size: 13px; }}
</style></head>
<body>
  <h1>Перенос картинок на родственников — подтверди картинку близкого варианта, «не переносить» или найди в 🔍 всей базе</h1>
  {body}
  <div class="bar">
    <button onclick="downloadMapping()">Скачать propagation.json</button>
    <span id="count"></span>
  </div>

  <div id="searchModal" class="modal">
    <div class="modal-box">
      <div class="modal-head">
        <input id="searchInput" placeholder="Поиск по названию среди упражнений с картинкой…"
               oninput="filterSearch()">
        <button type="button" onclick="closeSearch()">Закрыть</button>
      </div>
      <div id="searchResults" class="results"></div>
    </div>
  </div>

<script>
  const DONORS = {donors_js};
  let activeEx = null;

  function esc(s) {{
    return (s || '').replace(/[&<>]/g, function (c) {{
      return {{'&': '&amp;', '<': '&lt;', '>': '&gt;'}}[c];
    }});
  }}
  function imgsHtml(list) {{
    return (list || []).map(function (u) {{
      return '<img loading="lazy" src="' + u + '">';
    }}).join('');
  }}

  function collect() {{
    const map = {{}};
    document.querySelectorAll('section.ex').forEach(function (sec) {{
      const id = sec.getAttribute('data-ex');
      const chosen = sec.querySelector('input[name="ex_' + id + '"]:checked');
      if (chosen && chosen.value) map[id] = chosen.value;
    }});
    return map;
  }}
  function refresh() {{
    document.getElementById('count').textContent =
      Object.keys(collect()).length + ' к переносу';
  }}
  document.addEventListener('change', refresh);
  function downloadMapping() {{
    const blob = new Blob([JSON.stringify(collect(), null, 2)], {{type: 'application/json'}});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'propagation.json';
    a.click();
  }}

  // --- Ручной поиск донора по всей базе (упражнения нашей базы с картинкой) ---
  function openSearch(exId) {{
    activeEx = exId;
    document.getElementById('searchModal').style.display = 'flex';
    const input = document.getElementById('searchInput');
    input.value = '';
    filterSearch();
    input.focus();
  }}
  function closeSearch() {{
    document.getElementById('searchModal').style.display = 'none';
  }}
  function filterSearch() {{
    const q = document.getElementById('searchInput').value.trim().toLowerCase();
    const box = document.getElementById('searchResults');
    if (!q) {{
      box.innerHTML = '<div class="empty">Введите часть названия…</div>';
      return;
    }}
    const hits = DONORS.filter(function (e) {{
      return e.name.toLowerCase().includes(q);
    }}).slice(0, 40);
    box.innerHTML = hits.map(function (e) {{
      return '<div class="result">' + '<div class="imgs">' + imgsHtml(e.imgs) + '</div>'
        + '<div class="cand-name">' + esc(e.name) + '</div>'
        + '<div class="cand-sub">' + esc(e.eq) + '</div>'
        + '<button type="button" onclick="pickManual(\\'' + e.id + '\\')">Выбрать</button>'
        + '</div>';
    }}).join('') || '<div class="empty">Ничего не найдено</div>';
  }}
  function pickManual(id) {{
    const e = DONORS.find(function (x) {{ return String(x.id) === String(id); }});
    if (!e || activeEx === null) return;
    const sec = document.querySelector('section.ex[data-ex="' + activeEx + '"]');
    sec.querySelectorAll('.cand.manual').forEach(function (n) {{ n.remove(); }});
    const label = document.createElement('label');
    label.className = 'cand manual';
    label.innerHTML =
      '<input type="radio" name="ex_' + activeEx + '" value="' + e.id + '" checked>'
      + '<div class="imgs">' + imgsHtml(e.imgs) + '</div>'
      + '<div class="cand-name">' + esc(e.name) + '</div>'
      + '<div class="cand-sub">' + esc(e.eq) + ' · вручную</div>';
    const cands = sec.querySelector('.cands');
    cands.insertBefore(label, cands.firstChild);
    closeSearch();
    refresh();
  }}

  refresh();
</script>
</body></html>"""


async def cmd_propagate() -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Exercise).where(Exercise.app_user_id.is_(None))
        )
        rows = list(result.scalars().all())

    donors = [e for e in rows if e.image_urls]
    targets = [e for e in rows if not e.image_urls]
    print(f"[propagate] С картинкой (доноры): {len(donors)}, без картинки: {len(targets)}")

    blocks: List[str] = []
    matched = 0
    for t in sorted(targets, key=lambda e: (e.main_muscle_group or "", e.name)):
        cands = _propagate_candidates(t, donors)
        if cands:
            matched += 1
        blocks.append(_render_target_block(t, cands))

    PROPAGATE_HTML.write_text(_render_propagate_page(blocks, donors), encoding="utf-8")
    print(
        f"[propagate] Родственники найдены для {matched}/{len(targets)} безкартиночных.\n"
        f"[propagate] Открой: {PROPAGATE_HTML}\n"
        f"[propagate] Подтверди переносы и нажми «Скачать propagation.json», "
        f"затем: apply-propagation"
    )


async def cmd_apply_propagation(arg: str) -> None:
    path = _resolve_json_path(arg, DEFAULT_PROPAGATION)
    if path is None:
        sys.exit(
            f"[apply-propagation] Не нашёл propagation.json.\n"
            f"Подсказка: запусти без аргумента — возьмётся {DEFAULT_PROPAGATION}."
        )
    print(f"[apply-propagation] Использую: {path}")
    mapping: Dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    if not mapping:
        sys.exit("[apply-propagation] Пусто — нечего переносить.")

    applied = 0
    skipped: List[str] = []

    async with SessionLocal() as session:
        for target_id_str, donor_id in mapping.items():
            if not donor_id:
                continue
            target_id = int(target_id_str)
            donor = await session.get(Exercise, int(donor_id))
            target = await session.get(Exercise, target_id)
            if not donor or not donor.image_urls:
                skipped.append(f"{target_id}: донор {donor_id} без картинок")
                continue
            if not target:
                skipped.append(f"{target_id}: нет упражнения в БД")
                continue

            dest_dir = MEDIA_DIR / "exercises" / str(target_id)
            dest_dir.mkdir(parents=True, exist_ok=True)

            rel_urls: List[str] = []
            for i, donor_rel in enumerate(donor.image_urls):
                src = MEDIA_DIR / donor_rel.lstrip("/")
                if not src.exists():
                    skipped.append(f"{target_id}: нет файла донора {donor_rel}")
                    continue
                dest = dest_dir / f"{i}.jpg"
                shutil.copyfile(src, dest)
                rel_urls.append(f"exercises/{target_id}/{i}.jpg")

            if not rel_urls:
                skipped.append(f"{target_id}: не скопировано ни одной картинки")
                continue

            target.image_urls = rel_urls
            target.image_approx = True  # картинка родственника — техника примерная
            applied += 1

        await session.commit()

    print(f"[apply-propagation] Перенесено: {applied}, пропущено: {len(skipped)}")
    for line in skipped[:30]:
        print(f"  ! {line}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="скачать каталог и картинки в кэш")
    sub.add_parser("suggest", help="сгенерировать review.html с кандидатами")
    p_apply = sub.add_parser("apply", help="применить mapping.json в media/ и БД")
    p_apply.add_argument("mapping", nargs="?", default=str(DEFAULT_MAPPING))
    sub.add_parser(
        "propagate",
        help="review-propagate.html: перенос картинок на родственников без картинки",
    )
    p_prop = sub.add_parser(
        "apply-propagation", help="применить propagation.json (перенос на родственников)"
    )
    p_prop.add_argument("mapping", nargs="?", default=str(DEFAULT_PROPAGATION))

    args = parser.parse_args()

    if args.cmd == "fetch":
        cmd_fetch()
    elif args.cmd == "suggest":
        asyncio.run(cmd_suggest())
    elif args.cmd == "apply":
        asyncio.run(cmd_apply(args.mapping))
    elif args.cmd == "propagate":
        asyncio.run(cmd_propagate())
    elif args.cmd == "apply-propagation":
        asyncio.run(cmd_apply_propagation(args.mapping))


if __name__ == "__main__":
    main()
