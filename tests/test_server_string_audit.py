import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_user_validation_errors_use_localized_exception_catalog():
    for relative in (
        "api/routers/body.py",
        "api/routers/data_io.py",
        "api/routers/goals.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "raise HTTPException" not in source, relative


def test_success_message_fields_do_not_embed_russian_copy():
    for relative in (
        "api/routers/mesocycles.py",
        "api/routers/microcycles.py",
        "api/routers/plans.py",
        "api/routers/splits.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not re.search(r'"message"\s*:\s*f?"[^"\n]*[А-Яа-яЁё]', source), relative
