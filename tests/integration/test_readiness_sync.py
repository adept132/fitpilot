"""Офлайн-очередь наблюдений и last_top_weight в дельте (спека P0-07 §9.2, §12.1)."""


async def test_observations_endpoint_accepts_a_batch(client, auth_headers):
    response = await client.post(
        "/sync/observations",
        headers=auth_headers,
        json={
            "observations": [
                {"client_uuid": "obs-1", "sleep": 3, "stress": 2},
                {"client_uuid": "obs-2", "pain": {"knee": 2}, "source": "post_session"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    assert response.json()["duplicates"] == 0


async def test_replay_is_deduplicated(client, auth_headers):
    payload = {"observations": [{"client_uuid": "obs-dup", "sleep": 4}]}
    first = await client.post("/sync/observations", headers=auth_headers, json=payload)
    second = await client.post("/sync/observations", headers=auth_headers, json=payload)
    assert first.json()["accepted"] == 1
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 1


async def test_batch_validates_each_item(client, auth_headers):
    response = await client.post(
        "/sync/observations",
        headers=auth_headers,
        json={"observations": [{"client_uuid": "obs-bad", "sleep": 42}]},
    )
    assert response.status_code == 422


async def test_empty_batch_is_fine(client, auth_headers):
    response = await client.post(
        "/sync/observations", headers=auth_headers, json={"observations": []}
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 0


async def test_mixed_batch_counts_new_and_replayed_separately(client, auth_headers):
    # Один новый item и один уже отправленный ранее — в одной пачке. save_signals()
    # вернёт [] на повтор, но не на первый — разница не должна размыться в общий счётчик.
    first_payload = {"observations": [{"client_uuid": "obs-seen", "sleep": 3}]}
    seed = await client.post(
        "/sync/observations", headers=auth_headers, json=first_payload
    )
    assert seed.json()["accepted"] == 1

    response = await client.post(
        "/sync/observations",
        headers=auth_headers,
        json={
            "observations": [
                {"client_uuid": "obs-new", "sleep": 2},
                {"client_uuid": "obs-seen", "sleep": 3},
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1


async def test_first_time_empty_item_counts_as_accepted(client, auth_headers):
    # Все поля пропущены честно (см. докстринг CheckinRequest) — это не ошибка
    # и не повтор, save_signals() вернёт [] просто потому, что писать нечего.
    # Такой item не должен попасть в duplicates.
    response = await client.post(
        "/sync/observations",
        headers=auth_headers,
        json={"observations": [{"client_uuid": "obs-empty-first"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 0


async def test_changes_delta_carries_last_top_weight(
    client, auth_headers, finished_session_with_prescription
):
    # Чтобы удержать вес офлайн, устройству нужен якорь: без него
    # applyReadinessCap не сможет ничего ограничить (спека §9.2). Пустой
    # словарь по умолчанию (Pydantic `= {}`) прошёл бы и без реальной записи —
    # поэтому проверяем не только форму ответа, но и что якорь засеянного
    # упражнения в нём реально есть.
    response = await client.get("/sync/changes", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "last_top_weights" in body
    assert isinstance(body["last_top_weights"], dict)

    key = str(finished_session_with_prescription.exercise_id)
    assert key in body["last_top_weights"]
    assert body["last_top_weights"][key] > 0


async def test_last_top_weights_keys_match_prescriptions(
    client, auth_headers, finished_session_with_prescription
):
    body = (await client.get("/sync/changes", headers=auth_headers)).json()
    # На пустом словаре цикл ниже не выполнился бы ни разу и тест прошёл бы
    # вхолостую — поэтому сперва убеждаемся, что засеянное упражнение реально
    # присутствует, и только потом проверяем форму ключей у всех записей.
    key = str(finished_session_with_prescription.exercise_id)
    assert key in body["last_top_weights"]
    for exercise_id in body["last_top_weights"]:
        assert exercise_id.isdigit()
