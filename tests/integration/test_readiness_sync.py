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


async def test_changes_delta_carries_last_top_weight(
    client, auth_headers, finished_session_with_prescription
):
    # Чтобы удержать вес офлайн, устройству нужен якорь: без него
    # applyReadinessCap не сможет ничего ограничить (спека §9.2).
    response = await client.get("/sync/changes", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "last_top_weights" in body
    assert isinstance(body["last_top_weights"], dict)


async def test_last_top_weights_keys_match_prescriptions(
    client, auth_headers, finished_session_with_prescription
):
    body = (await client.get("/sync/changes", headers=auth_headers)).json()
    for exercise_id in body["last_top_weights"]:
        assert exercise_id.isdigit()
