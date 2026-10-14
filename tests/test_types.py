from conftest import wait_for

from ciplink_tagserver import cli

AOI = {"tag_type": "struct", "data_type_name": "MyAOI",
       "data_type": {"name": "MyAOI", "aoi": {"whole_read": False, "readable": ["Open", "Cmd"],
                                             "refused": ["Local1"]}}}
REAL = {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"}


def serve_tags(client, monkeypatch):
    """Routes the CLI's HTTP fetch to the in-process test client."""
    def fetch(url, token, plc):
        response = client.get("/tags", params={"plc": plc} if plc else None,
                              headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()["tags"]
    monkeypatch.setattr(cli, "fetch_tags", fetch)


def connected(client, *plcs):
    wait_for(lambda: all(client.get("/health").json()["plcs"][p]["state"] == "connected" for p in plcs))


def test_types_one_plc(make, tmp_path, monkeypatch):
    def setup(fakes):
        fakes["PLC1"].tags = {"Line1.Speed": REAL, "Valve1": AOI}
    client, _, _ = make(setup=setup)
    connected(client, "PLC1")
    serve_tags(client, monkeypatch)
    out = tmp_path / "tags.d.ts"
    cli.main(["types", "--token", "view", "--plc", "PLC1", "--out", str(out)])
    text = out.read_text()
    assert 'export type TagName =\n  | "Line1.Speed"\n  | "Valve1.Open"\n  | "Valve1.Cmd";\n' in text
    assert "Local1" not in text


def test_types_qualified_with_several_plcs(make, tmp_path, monkeypatch):
    def setup(fakes):
        fakes["PLC1"].tags = {"Line1.Speed": REAL}
        fakes["PLC2"].tags = {"Program:Station1.DoorOpen": REAL, "Valve1": AOI}
    client, _, _ = make("PLC1", "PLC2", setup=setup)
    connected(client, "PLC1", "PLC2")
    serve_tags(client, monkeypatch)
    out = tmp_path / "tags.d.ts"
    cli.main(["types", "--token", "view", "--plc", "PLC1", "--plc", "PLC2", "--out", str(out)])
    assert out.read_text().endswith(
        '  | "PLC1/Line1.Speed"\n  | "PLC2/Program:Station1.DoorOpen"\n'
        '  | "PLC2/Valve1.Open"\n  | "PLC2/Valve1.Cmd";\n')
