import json
from types import SimpleNamespace

from ciplink_tagserver.cli import main
from ciplink_tagserver.mock import MockDriver

from conftest import hello, until


def test_mock_driver(tmp_path):
    path = tmp_path / "mock.json"
    path.write_text(json.dumps({"Speed": "REAL", "Run": "BOOL", "Count": "DINT", "Recipe": "UDT_R"}))
    plc = MockDriver(SimpleNamespace(mock_tags=str(path)))
    speeds = {plc.read("Speed").value for _ in range(20)}
    assert len(speeds) > 1
    assert all(isinstance(plc.read("Count").value, int) for _ in range(5))
    assert {plc.read("Run").value for _ in range(200)} == {True, False}
    assert plc.read("Nope").error.startswith("Tag doesn't exist")
    assert plc.read("Recipe").error  # structs are not simulated
    assert plc.write("Count", 7).error is None
    assert plc.values["Count"] == 7
    assert plc.tags["Speed"]["data_type_name"] == "REAL"


def test_mock_mode_values_change(make, tmp_path):
    (tmp_path / "mock.json").write_text(json.dumps({"Speed": "REAL"}))
    client, _, _ = make(mock=True, extra='mock_tags = "mock.json"')
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        seen = set()
        while len(seen) < 3:
            seen.add(until(ws, lambda m: m["op"] == "values")["d"]["Speed"][0])
        ws.send_json({"op": "write", "id": 2, "tag": "Speed", "value": 99.0})
        assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 2)["ok"]
    tags = client.get("/tags", headers={"Authorization": "Bearer view"}).json()["tags"]
    assert tags == [{"name": "Speed", "type": "REAL"}]


def test_dump_tags(tmp_path, monkeypatch):
    class Driver:
        tags = {"Speed": {"data_type_name": "REAL"}, "Valve1": {"data_type": {"name": "MyAOI"}}}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr("ciplink_tagserver.server.driver_factory", lambda cfg: Driver())
    config = tmp_path / "t.toml"
    config.write_text('[plc.A]\npath = "192.0.2.1"\n')
    out = tmp_path / "tags.json"
    main(["dump-tags", "--config", str(config), "--plc", "A", "--out", str(out)])
    assert json.loads(out.read_text()) == {"Speed": "REAL", "Valve1": "MyAOI"}
