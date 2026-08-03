"""AntiSuicideValidator.validate_mesocycle_sequence: правила безопасности мезоцикла.

Отдельное покрытие для ослабления Правила 2 (P0-08): после разгрузки (deload)
разрешён скачок интенсивности вверх любой величины, а сама проверка резкости
скачка применяется только к восходящим переходам. Раньше прежнее правило
запрещало даже рядовой переход deload -> prefailure, хотя возврат к тяжёлой
работе сразу после разгрузочной недели -- это и есть смысл разгрузки
(суперкомпенсация). Ослабление понадобилось потому, что движок P0-08 умеет
вставлять досрочную разгрузку в середину блока, и последовательность вида
easy -> medium -> deload -> prefailure -> deload обязана оставаться допустимой.
"""
import pytest
from fastapi import HTTPException

from api.services.validator import AntiSuicideValidator


def test_deload_potom_prefailure_razreshen():
    """После разгрузки допускается прыжок вверх любой величины: суперкомпенсация
    подразумевает возврат к тяжёлой работе, а не постепенный разгон заново."""
    AntiSuicideValidator.validate_mesocycle_sequence(["deload", "prefailure"])


def test_deload_potom_failure_i_snova_deload_razreshen():
    """Прыжок с разгрузки прямо в отказ -- крайний случай того же правила:
    он не должен запрещаться просто из-за величины скачка, если старт из deload."""
    AntiSuicideValidator.validate_mesocycle_sequence(["deload", "failure", "deload"])


def test_ryadovoy_blok_bez_rezkih_perehodov_razreshen():
    """Обычная последовательность без резких скачков (шаг за шагом вверх,
    разгрузка в конце) должна проходить как проходила и раньше -- ослабление
    не должно задевать штатный сценарий."""
    AntiSuicideValidator.validate_mesocycle_sequence(["easy", "medium", "prefailure", "deload"])


def test_motiv_oslableniya_dosrochnaya_razgruzka_ot_dvizhka_p0_08():
    """Именно эта форма последовательности -- результат вставки движком P0-08
    досрочной разгрузки в середину блока (insert_deload). Если бы Правило 2
    не ослабили, движку было бы можно то, что запрещено пользователю
    в конструкторе шаблонов -- само по себе это несоответствие и было причиной
    правки."""
    AntiSuicideValidator.validate_mesocycle_sequence(
        ["easy", "medium", "deload", "prefailure", "deload"]
    )


def test_rezkiy_skachok_ne_posle_razgruzki_vse_esche_zapreshen():
    """Ослабление касается только перехода ИЗ deload. Скачок с easy сразу
    на failure (через 3 ступени) без предшествующей разгрузки остаётся
    той самой резкой перегрузкой, от которой и защищает валидатор."""
    with pytest.raises(HTTPException):
        AntiSuicideValidator.validate_mesocycle_sequence(["easy", "failure", "deload"])


def test_shest_faz_nagruzki_podryad_bez_razgruzki_zapreshen():
    """Правило лимита без отдыха не связано с Правилом 2 и не должно было
    измениться: шесть и более фаз нагрузки подряд без единой разгрузки --
    это критическая перегрузка независимо от размера отдельных скачков."""
    with pytest.raises(HTTPException):
        AntiSuicideValidator.validate_mesocycle_sequence(
            ["easy", "medium", "easy", "medium", "easy", "medium"]
        )


def test_failure_bez_posleduyuschey_razgruzki_zapreshen():
    """Правило выхода из отказа тоже не менялось: за фазой failure обязана
    идти deload, иначе не происходит суперкомпенсации, ради которой и было
    сделано ослабление Правила 2 -- отказ без разгрузки просто опасен."""
    with pytest.raises(HTTPException):
        AntiSuicideValidator.validate_mesocycle_sequence(["medium", "failure", "easy"])


def test_pustoy_mezocikl_zapreshen():
    """Пустая последовательность фаз не описывает никакого мезоцикла --
    валидатор обязан отклонять её сразу, до перебора правил."""
    with pytest.raises(HTTPException):
        AntiSuicideValidator.validate_mesocycle_sequence([])


def test_ponizhenie_intensivnosti_lyuboy_glubiny_vsegda_razresheno():
    """Правило 2 в принципе про скачки ВВЕРХ. Падение интенсивности -- хоть
    сразу с failure на deload -- не может быть перегрузкой по определению,
    и проверка не должна на него срабатывать вне зависимости от глубины."""
    AntiSuicideValidator.validate_mesocycle_sequence(["failure", "deload", "easy"])
