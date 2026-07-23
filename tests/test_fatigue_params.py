"""Тесты реестра параметров усталостной модели."""

import dataclasses

import pytest

from api.services.fatigue.params import (
    CONFIG,
    DEFAULT_PARAMS,
    LIT,
    MODEL_VERSION,
    PARAM_REGISTRY,
    PRIOR,
    T1,
    T2,
    T3,
    TIER_NONE,
)


def test_model_version_is_set():
    assert MODEL_VERSION


def test_every_param_field_is_in_registry():
    # Реестр — единственный источник провенанса. Поле без записи в реестре
    # означает магическую цифру, а это запрещено глобальным ограничением.
    fields = {f.name for f in dataclasses.fields(DEFAULT_PARAMS)}
    assert fields == set(PARAM_REGISTRY.keys())


def test_registry_values_match_defaults():
    for name, param in PARAM_REGISTRY.items():
        assert getattr(DEFAULT_PARAMS, name) == param.value, name


@pytest.mark.parametrize("name,param", list(PARAM_REGISTRY.items()))
def test_provenance_and_tier_are_valid(name, param):
    assert param.provenance in {LIT, PRIOR, CONFIG}, name
    assert param.tier in {T1, T2, T3, TIER_NONE}, name


@pytest.mark.parametrize("name,param", list(PARAM_REGISTRY.items()))
def test_config_and_lit_params_are_never_personalized(name, param):
    # Предохранитель, подогнанный под пользователя, перестаёт быть
    # предохранителем; литературные окна теряют сопоставимость.
    if param.provenance in {CONFIG, LIT}:
        assert param.tier == TIER_NONE, name


@pytest.mark.parametrize("name,param", list(PARAM_REGISTRY.items()))
def test_personalizable_params_are_priors(name, param):
    if param.tier in {T1, T2, T3}:
        assert param.provenance == PRIOR, name


@pytest.mark.parametrize("name,param", list(PARAM_REGISTRY.items()))
def test_every_param_explains_its_tier(name, param):
    assert param.note, name


def test_tau_ordering_matches_physiology():
    # Механика восстанавливается дольше мышцы, мышца — дольше системы.
    p = DEFAULT_PARAMS
    assert p.tau_systemic_h < p.tau_muscular_h < p.tau_mechanical_h


def test_only_tau_is_tier_one():
    t1 = {n for n, p in PARAM_REGISTRY.items() if p.tier == T1}
    assert t1 == {"tau_systemic_h", "tau_muscular_h", "tau_mechanical_h"}
