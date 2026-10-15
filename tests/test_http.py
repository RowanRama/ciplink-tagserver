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
        ws.send_json({"op": "sub", "id": 1, "tags": ["A", "B"]})
        until(ws, lambda m: m["op"] == "values")
        wait_for(lambda: client.get("/stats").json()["plcs"]["PLC1"]["reads_per_cycle"] == 2)
        stats = client.get("/stats").json()
        assert stats["sessions"] == 1
        plc = stats["plcs"]["PLC1"]
        assert plc["subscriptions"] == 2 and plc["cycle_ms"] == 20
        assert "overruns" in plc and plc["last_cycle_ms"] is not None
        assert (plc["tier1"], plc["tier2"], plc["tags_per_cycle"], plc["group_fallbacks"]) == (2, 0, 2, 0)
        assert plc["groups"] == {}


def test_tags(make):
    client, fakes, _ = make(setup=lambda f: setattr(f["PLC1"], "tags", TAGS))
    assert client.get("/tags").status_code == 401
    assert client.get("/tags", headers={"Authorization": "Bearer nope"}).status_code == 401
    wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
    body = client.get("/tags?plc=PLC1", headers={"Authorization": "Bearer view"}).json()
    assert body["tags"] == [{"name": "Speed", "type": "REAL"},
                            {"name": "Valve1", "type": "MyAOI", "aoi_readable": ["Open"]}]
    assert client.get("/tags?plc=Nope", headers={"Authorization": "Bearer view"}).status_code == 404


GROUPS = """
[[plc.PLC1.rule]]
match = "Tank[*].Level"
units = "%"

[[plc.PLC1.group]]
name = "fast"
rate_ms = 10
match = ["Fast*"]
"""


def test_tags_types_rules_groups_and_cors(make, monkeypatch):
    import conftest

    tank = conftest.udt("Tank", 12, {"Level": "REAL", "Hist": ("REAL", 2)})
    monkeypatch.setattr(conftest.FakeDriver, "data_types", {"Tank": tank, "STR": conftest.STR}, raising=False)
    tags = {"Tank": conftest.tag_def(tank, dim=1, dims=[4]), "Name": conftest.tag_def(conftest.STR)}
    client, fakes, _ = make(extra=GROUPS, setup=lambda f: setattr(f["PLC1"], "tags", tags))
    wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
    body = client.get("/tags", headers={"Authorization": "Bearer view", "Origin": "http://hmi"})
    assert body.headers["access-control-allow-origin"] == "*"
    body = body.json()
    assert body["plc"] == "PLC1" and body["state"] == "connected" and body["version"] == 1
    assert body["tags"] == [{"name": "Tank", "type": "Tank", "dims": [4]}, {"name": "Name", "type": "STRING"}]
    assert body["types"] == {"Tank": {"members": {"Level": {"type": "REAL"}, "Hist": {"type": "REAL", "dims": [2]}}}}
    assert body["rules"] == [{"match": "Tank[*].Level", "units": "%"}]
    assert body["groups"] == [{"name": "fast", "rate_ms": 10, "match": ["Fast*"]}]
    preflight = client.options("/tags", headers={"Origin": "http://hmi", "Access-Control-Request-Method": "GET",
                                                 "Access-Control-Request-Headers": "Authorization"})
    assert preflight.status_code == 200


def test_stats_groups_and_write_counters(make):
    client, fakes, _ = make(extra=GROUPS)
    fakes["PLC1"].values.update({"FastA": 1, "Slow": 2})
    stats = client.get("/stats").json()
    assert (stats["writes"], stats["write_rejects"], stats["momentary_timeouts"]) == (0, 0, 0)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["FastA", "Slow"]})
        until(ws, lambda m: m["op"] == "values")
        wait_for(lambda: client.get("/stats").json()["plcs"]["PLC1"]["groups"]["fast"]["tags"] == 1)
        group = client.get("/stats").json()["plcs"]["PLC1"]["groups"]["fast"]
        assert group["rate_ms"] == 10 and "last_period_ms" in group
