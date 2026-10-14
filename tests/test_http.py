from conftest import hello, until, wait_for

TAGS = {
    "Speed": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"},
    "Valve1": {"tag_type": "struct", "data_type_name": "MyAOI",
               "data_type": {"name": "MyAOI", "aoi": {"whole_read": False, "readable": ["Open"],
                                                     "refused": ["Local1"]}}},
}


def test_health(make):
    client, fakes, _ = make("A", "B", setup=lambda f: setattr(f["B"], "fail", True))
    wait_for(lambda: client.get("/health").json()["plcs"]["A"]["last_ok_ts"] is not None)
    plcs = client.get("/health").json()["plcs"]
    assert plcs["A"]["state"] == "connected"
    assert plcs["B"]["state"] in ("connecting", "disconnected")
    assert set(plcs["A"]) == {"state", "last_cycle_ms", "last_ok_ts", "reconnects", "reloads"}
    assert plcs["A"]["reloads"] == 0


def test_stats(make):
    client, fakes, _ = make()
    fakes["PLC1"].values.update({"A": 1, "B": 2})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["A", "B"], "tier": 3})
        until(ws, lambda m: m["op"] == "values")
        wait_for(lambda: client.get("/stats").json()["plcs"]["PLC1"]["reads_per_cycle"] == 2)
        stats = client.get("/stats").json()
        assert stats["sessions"] == 1
        plc = stats["plcs"]["PLC1"]
        assert plc["subscriptions"] == 2 and plc["cycle_ms"] == 20
        assert "overruns" in plc and plc["last_cycle_ms"] is not None
        session = next(iter(client.app.state.hub.sessions))
        assert session.subs[1]["tier"] == 3  # stored for later


def test_tags(make):
    client, fakes, _ = make(setup=lambda f: setattr(f["PLC1"], "tags", TAGS))
    assert client.get("/tags").status_code == 401
    assert client.get("/tags", headers={"Authorization": "Bearer nope"}).status_code == 401
    wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
    body = client.get("/tags?plc=PLC1", headers={"Authorization": "Bearer view"}).json()
    assert body["tags"] == [{"name": "Speed", "type": "REAL"},
                            {"name": "Valve1", "type": "MyAOI", "aoi_readable": ["Open"]}]
    assert client.get("/tags?plc=Nope", headers={"Authorization": "Bearer view"}).status_code == 404
