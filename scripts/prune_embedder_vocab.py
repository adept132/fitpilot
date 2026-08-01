"""Обрезка вокабуляра эмбеддера под наш домен.

Зачем: в model_quantized.onnx матрица embeddings.word_embeddings.weight занимает
91.6 МБ из 112.2 МБ (82% модели) — это словарь на 250k токенов мультиязычной
MiniLM. Названия упражнений задействуют около тысячи из них. Обрезка режет и
модель, и память токенайзера (он сам по себе стоил 245 МБ RSS).

Почему резать строки безопасно: квантование матрицы ПО-ТЕНЗОРНОЕ (scale и
zero_point — скаляры), поэтому строки независимы и пересчитывать ничего не надо.
Проверяется явно, скрипт откажется работать, если это перестанет быть так.

Id спецтокенов (<s>=0, <pad>=1, </s>=2, <unk>=3) сохраняются автоматически:
они первые в словаре, а мы сохраняем исходный относительный порядок.

Запуск:
    python scripts/prune_embedder_vocab.py --policy ru --out api/data/embedder_pruned
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import onnx
from onnx import numpy_helper
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parent.parent
# Исходная (необрезанная) модель. В репозитории её нет — там уже лежит результат
# обрезки. Для повторного прогона нужен полный эмбеддер: либо каталог
# api/data/embedder_full, либо заново скачанный paraphrase-multilingual-MiniLM.
SRC_DIR = ROOT / "api" / "data" / "embedder_full"
INDEX_LABELS = ROOT / "api" / "data" / "exercise_index.json"

EMB_NAME = "embeddings.word_embeddings.weight_quantized"
SCALE_NAME = "embeddings.word_embeddings.weight_scale"
ZP_NAME = "embeddings.word_embeddings.weight_zero_point"

SPECIAL_IDS = {0, 1, 2, 3}


def _is_ru_en(text: str) -> bool:
    """Токен состоит только из кириллицы/латиницы/цифр/пунктуации."""
    body = text.replace("▁", "")
    if not body:
        return True
    for ch in body:
        if not ch.isalpha():
            continue
        low = ch.lower()
        if "а" <= low <= "я" or low == "ё" or "a" <= low <= "z":
            continue
        return False
    return True


def _is_cyrillic(text: str) -> bool:
    body = text.replace("▁", "")
    return bool(body) and any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in body) \
        and _is_ru_en(text)


def build_keep_ids(policy: str, vocab: list[str], corpus: list[str],
                   tokenizer: Tokenizer) -> list[int]:
    keep = set(SPECIAL_IDS)

    # Токены, реально нужные корпусу, на котором построен индекс.
    for text in corpus:
        keep.update(tokenizer.encode(text).ids)

    # Страховка от <unk>: byte_fallback у этого токенайзера выключен, поэтому
    # символ, которого нет в словаре, превращается в <unk> и вектор портится.
    # Держим короткие куски, чтобы незнакомое слово раскладывалось, а не терялось.
    short = {i for i, v in enumerate(vocab)
             if len(v.replace("▁", "")) <= 2 and _is_ru_en(v)}
    keep |= short

    if policy in ("ru", "ru_en"):
        keep |= {i for i, v in enumerate(vocab) if _is_cyrillic(v)}
    if policy == "ru_en":
        keep |= {i for i, v in enumerate(vocab) if _is_ru_en(v)}

    # Порядок сохраняем исходный — от него зависят id спецтокенов.
    return sorted(keep)


def prune(policy: str, out_dir: Path) -> None:
    tok_path = SRC_DIR / "tokenizer.json"
    raw = json.loads(tok_path.read_text(encoding="utf-8"))
    vocab_entries = raw["model"]["vocab"]
    vocab = [e[0] for e in vocab_entries]

    corpus = [l["name"] for l in json.loads(INDEX_LABELS.read_text(encoding="utf-8"))]
    tokenizer = Tokenizer.from_file(str(tok_path))

    keep_ids = build_keep_ids(policy, vocab, corpus, tokenizer)
    old_to_new = {old: new for new, old in enumerate(keep_ids)}
    print(f"политика={policy}: оставляем {len(keep_ids)} из {len(vocab)} токенов")

    assert old_to_new[0] == 0 and old_to_new[1] == 1, "спецтокены съехали"
    assert old_to_new[2] == 2 and old_to_new[3] == 3, "спецтокены съехали"

    # --- модель ---
    model = onnx.load(str(SRC_DIR / "model_quantized.onnx"))
    inits = {i.name: i for i in model.graph.initializer}

    scale = numpy_helper.to_array(inits[SCALE_NAME])
    zero_point = numpy_helper.to_array(inits[ZP_NAME])
    if scale.ndim != 0 or zero_point.ndim != 0:
        raise SystemExit(
            "Квантование матрицы эмбеддингов больше не по-тензорное "
            f"(scale.shape={scale.shape}) — резать строки без пересчёта нельзя."
        )

    weights = numpy_helper.to_array(inits[EMB_NAME])
    pruned = np.ascontiguousarray(weights[keep_ids])
    print(f"матрица: {weights.shape} ({weights.nbytes/1048576:.1f} МБ)"
          f" -> {pruned.shape} ({pruned.nbytes/1048576:.1f} МБ)")

    new_init = numpy_helper.from_array(pruned, EMB_NAME)
    inits[EMB_NAME].CopyFrom(new_init)

    out_dir.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(out_dir / "model_quantized.onnx"))

    # --- токенайзер ---
    raw["model"]["vocab"] = [vocab_entries[i] for i in keep_ids]
    for entry in raw.get("added_tokens", []):
        if entry["id"] in old_to_new:
            entry["id"] = old_to_new[entry["id"]]
        else:
            entry["id"] = None
    raw["added_tokens"] = [e for e in raw.get("added_tokens", []) if e["id"] is not None]
    (out_dir / "tokenizer.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    for extra in ("config.json", "tokenizer_config.json", "special_tokens_map.json",
                  "ort_config.json"):
        src = SRC_DIR / extra
        if src.exists():
            shutil.copy2(src, out_dir / extra)

    # vocab_size в config должен совпадать с новой матрицей, иначе загрузчики
    # transformers будут вводить в заблуждение.
    cfg_path = out_dir / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["vocab_size"] = len(keep_ids)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(p.stat().st_size for p in out_dir.iterdir()) / 1048576
    was = sum(p.stat().st_size for p in SRC_DIR.iterdir()
              if p.name != "model.onnx") / 1048576
    print(f"каталог: {was:.1f} МБ -> {total:.1f} МБ  ({out_dir})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=("corpus", "ru", "ru_en"), default="ru",
                    help="corpus — только нужное индексу + короткие куски; "
                         "ru — плюс вся кириллица; ru_en — плюс латиница")
    ap.add_argument("--out", type=Path, default=ROOT / "api" / "data" / "embedder_pruned")
    args = ap.parse_args()
    prune(args.policy, args.out)
