import pytest

from ciplink_tagserver.planner import _pack, chunks, execute, packets, plan, read_size, reply_size, request_size, tokenize

from conftest import FakeDriver, FakePLC, plant, tag_def, udt


@pytest.fixture
def plc():
    fake = FakePLC()
    plant(fake)
    fake.tags.update({"Trend": tag_def("REAL", 1, [16]), "Long": tag_def("REAL", 1, [1000]),
                      "BoolArr": tag_def("BOOL", 1, [32]), "Tank": tag_def(TANK, 1, [4]),
                      "Grid": tag_def("DINT", 2, [4, 4])})
    fake.values.update({"Trend": [i / 2 for i in range(16)], "Long": [float(i) for i in range(1000)],
                        "BoolArr": [i % 3 == 0 for i in range(32)],
                        "Tank": [{"Level": i * 10.0, "Temp": 20.0 + i, "Valve": i % 2 == 0} for i in range(4)]})
    return FakeDriver(fake)


TANK = udt("Tank", 12, {"Level": "REAL", "Temp": "REAL", "Valve": "BOOL"})


def reads(plc, tags, whole_max_bytes=1500, no_group=frozenset(), budget=4000, together=None):
    """{read name: sorted requested tags it serves}"""
    return {name: sorted(t for t, _ in members)
            for name, members in plan(tags, plc.get_tag_info, whole_max_bytes, no_group, budget, together)}


def direct(plc, tags):
    """What reading each tag on its own gives: (value, error)."""
    return {tag: (r.value, r.error) for tag, r in zip(tags, plc.read(*tags))}


def run(plc, tags, budget=4000, **kw):
    reads_ = plan(tags, plc.get_tag_info, 1500, budget=budget, **kw)
    out, failed, services = execute(plc, chunks(reads_, plc.get_tag_info, budget))
    return {tag: (r.value, r.error) for tag, r in out.items()}, failed, services


def test_tokenize():
    assert tokenize("Arr[3].Status.Ready") == [("Arr", "Arr"), ("[3]", 3), (".Status", "Status"), (".Ready", "Ready")]
    assert tokenize("Program:Station1.Motor.Speed") == [
        ("Program:Station1.Motor", "Program:Station1.Motor"), (".Speed", "Speed")]
    assert tokenize("M[1,2].X")[1] == ("[1,2]", (1, 2))
    for never in ("X.Count.5", "X{3}", "X..Y", "[1]", "Program:Main", ""):
        assert tokenize(never) is None


def test_groups_members_and_merges_child_groups_into_the_parent(plc):
    assert reads(plc, ["Line1.Motor.Speed", "Line1.Motor.Current", "Speed"]) == {
        "Line1.Motor": ["Line1.Motor.Current", "Line1.Motor.Speed"], "Speed": ["Speed"]}
    # Line1.Motor (40 B) first, then Line1 (200 B) takes it and two more: 4 reads -> 1
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current", "Line1.Ready", "Line1.Data[2]", "Speed"]
    assert reads(plc, tags) == {
        "Line1": ["Line1.Data[2]", "Line1.Motor.Current", "Line1.Motor.Speed", "Line1.Ready"],
        "Speed": ["Speed"],
    }
    assert reads(plc, ["Line1.Motor.Speed", "Line2.Motor.Speed"]) == {  # no common struct
        "Line1.Motor.Speed": ["Line1.Motor.Speed"], "Line2.Motor.Speed": ["Line2.Motor.Speed"]}


def test_array_elements_and_program_tags(plc):
    tags = ["Arr[3].Motor.Speed", "Arr[3].Ready", "Arr[1].Ready", "Program:Station1.Motor.Speed",
            "Program:Station1.Motor.Running"]
    assert reads(plc, tags) == {
        "Arr[3]": ["Arr[3].Motor.Speed", "Arr[3].Ready"],
        "Program:Station1.Motor": ["Program:Station1.Motor.Running", "Program:Station1.Motor.Speed"],
        "Arr[1].Ready": ["Arr[1].Ready"],  # a whole array is never read for its elements
    }


def test_whole_max_bytes(plc):
    plc.plc.tags["Blob1"] = tag_def(udt("Blob", 300, {"A": "DINT", "B": "DINT"}))
    assert reads(plc, ["Blob1.A", "Blob1.B"]) == {"Blob1": ["Blob1.A", "Blob1.B"]}
    assert reads(plc, ["Blob1.A", "Blob1.B"], whole_max_bytes=299) == {"Blob1.A": ["Blob1.A"], "Blob1.B": ["Blob1.B"]}
    assert reads(plc, ["Line1.Motor.Speed", "Line1.Ready"], whole_max_bytes=100) == {
        "Line1.Motor.Speed": ["Line1.Motor.Speed"], "Line1.Ready": ["Line1.Ready"]}


@pytest.mark.parametrize("size, grouped", [(464, True), (465, False)])
def test_extra_reply_bytes_per_read_saved_threshold(plc, size, grouped):
    # reply: size + 10 whole vs 2 x (4 + 8) plain, so 450 extra bytes for one saved read at 464
    plc.plc.tags["Blob1"] = tag_def(udt("Blob", size, {"A": "DINT", "B": "DINT"}))
    assert (reads(plc, ["Blob1.A", "Blob1.B"]) == {"Blob1": ["Blob1.A", "Blob1.B"]}) is grouped
    # a big struct is fine when it saves enough reads (Big1 is 4000 B, never with the default cap)
    assert len(reads(plc, ["Big1.A", "Big1.B"], whole_max_bytes=5000)) == 2


def test_a_group_never_adds_a_packet(plc):
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current"]
    get = plc.get_tag_info
    plain = [(request_size(t), reply_size(get(t))) for t in tags]
    group = [(request_size("Line1.Motor"), reply_size(get("Line1.Motor")))]
    assert sum(r for _, r in group) > sum(r for _, r in plain)  # the whole read is the bigger reply
    budget = max(52 + sum(r for _, r in plain), 30 + sum(q for q, _ in plain)) + 10  # room for the plain reads,
    assert 52 + group[0][1] > budget  # not for the whole reply
    assert packets(plain, budget) == 1 and packets(group, budget) == 2 + 2  # fragmented: version checks around
    assert reads(plc, tags, budget=budget) == {t: [t] for t in tags}
    assert reads(plc, tags, budget=4000) == {"Line1.Motor": tags[::-1]}


def test_packet_estimate_packs_in_order():
    assert packets([(30, 1000)] * 3, 4000) == 1
    assert packets([(30, 1000)] * 4, 4000) == 2  # 52 bytes of reply overhead per packet
    assert packets([(30, 5000)], 4000) == 2 + 2  # fragmented, with a version check before and after
    assert packets([], 4000) == 0


def test_aois(plc):
    assert len(reads(plc, ["Valve1.Open", "Valve1.EnableIn"])) == 2  # whole read refused
    assert reads(plc, ["Pump1.Run", "Pump1.Speed"]) == {"Pump1": ["Pump1.Run", "Pump1.Speed"]}
    assert len(reads(plc, ["Cell1.Mode", "Cell1.Step"])) == 2  # holds such an AOI
    assert reads(plc, ["Cell1.Valve.Open", "Cell1.Mode"]) == {
        "Cell1.Valve.Open": ["Cell1.Valve.Open"], "Cell1.Mode": ["Cell1.Mode"]}


def test_never_grouped(plc):
    tags = ["Line1.Motor.Count.0", "Line1.Motor.Count.2", "Line1.Data{2}", "Line1.Motor.Speed",
            "Line1.Bits[3]", "Line1.Data", "Name1.LEN", "Name1.DATA", "Nope.A", "Nope.B"]
    assert reads(plc, tags) == {t: [t] for t in tags}


def test_requested_struct_is_the_group_read(plc):
    tags = ["Line1.Motor", "Line1.Motor.Speed", "Line1.Motor.Running"]
    assert reads(plc, tags) == {"Line1.Motor": sorted(tags)}
    # also when it is too big to be chosen otherwise
    assert reads(plc, ["Big1", "Big1.A"]) == {"Big1": ["Big1", "Big1.A"]}
    # and it joins a bigger group when there is one anyway
    assert reads(plc, ["Line1.Motor", "Line1.Motor.Speed", "Line1.Ready"]) == {
        "Line1": ["Line1.Motor", "Line1.Motor.Speed", "Line1.Ready"]}


def test_sliced_values_are_what_member_reads_give(plc):
    tags = ["Line1.Motor.Speed", "line1.motor.current", "Line1.Ready", "Line1.Data[2]", "Arr[3].Motor.Speed",
            "Arr[3].Data[0]", "Program:Station1.Motor.Speed", "Program:Station1.Motor.Count",
            "Line2.Motor", "Line2.Motor.Running", "Pump1.Speed", "Pump1.Run", "Speed", "Count.1", "Nope"]
    got, failed, services = run(plc, tags)
    assert got == direct(plc, tags)
    assert failed == []
    # Line1, Arr[3], Program:Station1.Motor, Line2.Motor, Pump1, Speed, Count.1, Nope
    assert services == 8


def test_group_error_falls_back_to_member_reads(plc):
    plc.plc.errors["Line1.Motor"] = "General error"
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current", "Speed"]
    plc.plc.reads.clear()
    got, failed, services = run(plc, tags)
    assert got == direct(plc, tags)
    assert failed == ["Line1.Motor"] and services == 4
    assert plc.plc.reads[1] == ("Line1.Motor.Speed", "Line1.Motor.Current")
    # once known bad, it isn't read whole again; the next struct up still can be
    assert reads(plc, tags, no_group={"line1.motor"}) == {
        "Line1": ["Line1.Motor.Current", "Line1.Motor.Speed"], "Speed": ["Speed"]}
    assert reads(plc, tags, no_group={"line1.motor", "line1"}) == {t: [t] for t in tags}


def test_slice_failure_falls_back(plc):
    plc.plc.values["Line1"]["Motor"] = 5  # the whole read no longer holds the member
    plc.plc.values["Line1.Motor.Speed"] = 1.0
    plc.plc.values["Line1.Motor.Current"] = 2.0
    got, failed, _ = run(plc, ["Line1.Motor.Speed", "Line1.Ready", "Line1.Motor.Current"])
    assert got["Line1.Motor.Speed"] == (1.0, None) and got["Line1.Motor.Current"] == (2.0, None)
    assert failed == ["Line1"]


def test_array_runs(plc):
    assert reads(plc, ["Trend[3]", "Trend[4]", "Trend[7]", "Speed"]) == {
        "Trend[3]{5}": ["Trend[3]", "Trend[4]", "Trend[7]"], "Speed": ["Speed"]}
    # a member array: the run is cheaper than the whole struct
    assert reads(plc, ["Line1.Data[0]", "Line1.Data[3]", "Arr[2].Data[1]", "Arr[2].Data[2]"]) == {
        "Line1.Data[0]{4}": ["Line1.Data[0]", "Line1.Data[3]"],
        "Arr[2].Data[1]{2}": ["Arr[2].Data[1]", "Arr[2].Data[2]"]}
    # never runs: BOOL/DWORD bits, struct elements, members of elements, 2-D, whole arrays, a single element
    for tags in (["BoolArr[1]", "BoolArr[2]"], ["Line1.Bits[0]", "Line1.Bits[1]"], ["Tank[1].Level", "Tank[2].Level"],
                 ["Grid[1,1]", "Grid[1,2]"], ["Trend", "Trend[1]"], ["Trend[5]", "Speed"], ["Arr[1]", "Arr[2]"]):
        assert reads(plc, tags) == {t: [t] for t in tags}
    # a run that failed is not used again, nor is anything of a failed array
    assert len(reads(plc, ["Trend[3]", "Trend[4]"], no_group={"trend[3]{2}"})) == 2
    assert len(reads(plc, ["Trend[3]", "Trend[4]"], no_group={"trend"})) == 2


@pytest.mark.parametrize("last, grouped", [(113, True), (114, False)])
def test_array_run_cost_rule(plc, last, grouped):
    # REAL: (last + 1) * 4 + 8 reply bytes vs 2 x 12 plain: 440 extra bytes for one saved read at 113
    run_ = {f"Long[0]{{{last + 1}}}": ["Long[0]", f"Long[{last}]"]}
    assert (reads(plc, ["Long[0]", f"Long[{last}]"]) == run_) is grouped
    # far apart elements make separate runs
    assert reads(plc, ["Long[0]", "Long[1]", "Long[500]", "Long[502]"]) == {
        "Long[0]{2}": ["Long[0]", "Long[1]"], "Long[500]{3}": ["Long[500]", "Long[502]"]}


def test_array_run_never_adds_a_packet(plc):
    tags = ["Trend[0]", "Trend[15]"]
    plain = [read_size(t, plc.get_tag_info) for t in tags]
    whole = [read_size("Trend[0]{16}", plc.get_tag_info)]
    assert whole[0][1] == 16 * 4 + 8 and plain[0][1] == 12
    assert packets(plain, 100) == 1 and packets(whole, 100) == 2 + 2
    assert reads(plc, tags, budget=100) == {t: [t] for t in tags}
    assert reads(plc, tags) == {"Trend[0]{16}": tags}


def test_array_run_values_are_sliced(plc):
    tags = ["Trend[3]", "Trend[4]", "Trend[7]", "Line1.Data[0]", "Line1.Data[3]", "Line1.Ready", "Speed"]
    got, failed, services = run(plc, tags)
    assert got == direct(plc, tags) and failed == []
    assert got["Trend[7]"] == (3.5, None) and services == 3  # Trend run, Line1 (it took the Data run), Speed
    plc.plc.values["Trend"] = [1.0, 2.0]  # the run now reads past the end: its members are read one by one
    got, failed, _ = run(plc, ["Trend[0]", "Trend[1]", "Trend[3]"])
    assert failed == ["Trend[0]{4}"] and got["Trend[1]"] == (2.0, None) and got["Trend[3]"][1].kind == "not_found"


def test_together(plc):
    tags = ["Line1.Motor.Speed", "Line1.Ready"]
    assert reads(plc, tags, whole_max_bytes=100) == {t: [t] for t in tags}
    assert reads(plc, tags, whole_max_bytes=100, together=lambda p: p == "Line1") == {"Line1": tags}
    # regardless of cost; outermost first; never for one tag or a struct over one packet
    plc.plc.tags["Blob1"] = tag_def(udt("Blob", 3000, {"A": "DINT", "B": "DINT"}))
    assert reads(plc, ["Blob1.A", "Blob1.B"], together=lambda p: True) == {"Blob1": ["Blob1.A", "Blob1.B"]}
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current", "Line1.Ready"]
    assert reads(plc, tags, together=lambda p: True) == {"Line1": sorted(tags)}
    assert reads(plc, ["Line1.Motor.Speed", "Speed"], together=lambda p: True) == {
        "Line1.Motor.Speed": ["Line1.Motor.Speed"], "Speed": ["Speed"]}
    assert reads(plc, ["Big1.A", "Big1.B"], together=lambda p: True) == {"Big1.A": ["Big1.A"], "Big1.B": ["Big1.B"]}
    seen = []
    reads(plc, ["Arr[1].Motor.Speed", "Arr[1].Motor.Count"], together=lambda p: seen.append(p))
    assert seen == ["Arr[1]", "Arr[1].Motor"]
    # a failed together read falls back to its members like any group
    plc.plc.errors["Line1"] = "General error"
    got, failed, _ = run(plc, tags, together=lambda p: p == "Line1")
    assert failed == ["Line1"] and got == direct(plc, tags)


def test_chunks_are_one_packet_each(plc):
    tags = ["Line1.Motor.Speed", "Line2.Motor.Speed"] + [f"Trend[{i}]" for i in range(0, 16, 5)] + \
        [f"Arr[{i}].Ready" for i in range(5)] + ["Speed", "Count", "Name1", "Big1", "Flags"]
    budget = 200
    reads_ = plan(tags, plc.get_tag_info, 1500, budget=budget)
    parts = chunks(reads_, plc.get_tag_info, budget)
    assert sorted(r for part in parts for r in part) == sorted(reads_) and len(parts) == 3
    assert parts[-1] == [("Big1", [("Big1", None)])]  # as ciplink: the fragmented read after the others
    sizes = [[read_size(name, plc.get_tag_info) for name, _ in part] for part in parts]
    big = packets([read_size("Big1", plc.get_tag_info)], budget)  # fragmented: alone, several packets
    assert big > 1 and len(parts) == packets([s for part in sizes for s in part], budget) - big + 1
    for part, size in zip(parts, sizes):
        assert packets(size, budget) == 1 or [name for name, _ in part] == ["Big1"]
    assert chunks([], plc.get_tag_info) == []


def test_a_fragmented_read_does_not_split_the_reads_around_it():
    assert packets([(30, 100), (30, 5000), (30, 100)], 4000) == 5  # one packet for both small reads
    reads_ = [("A", [("A", None)]), ("F", [("F", None)]), ("B", [("B", None)])]
    sizes = {"A": (30, 100), "F": (30, 5000), "B": (30, 100)}
    groups, _ = _pack([sizes[name] for name, _ in reads_], 4000)
    assert groups == [[0, 2], [1]]


def test_read_size_of_ranges(plc):
    station = reply_size(plc.get_tag_info("Arr"))  # structure_size + 10
    assert read_size("Arr{4}", plc.get_tag_info)[1] == 4 * (station - 10) + 10
    plc.plc.tags["Bits"] = tag_def("DWORD", dim=1, dims=[4])  # ciplink's BOOL[128]
    assert read_size("Bits{64}", plc.get_tag_info)[1] == 2 * 4 + 8
    assert read_size("Trend[2]{3}", plc.get_tag_info)[1] == 3 * 4 + 8


def test_between_runs_after_each_chunk_but_the_last(plc):
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current", "Speed", "Count", "Trend[1]", "Trend[2]", "Flags", "Name1"]
    reads_ = plan(tags, plc.get_tag_info, 1500, budget=100)
    parts = chunks(reads_, plc.get_tag_info, 100)
    assert len(parts) >= 3
    events, given = [], {}
    read = plc.read
    plc.read = lambda *names: events.append(("read", names)) or read(*names)

    def between(partial):
        events.append(("between", sorted(partial)))
        given.update(partial)

    out, failed, services = execute(plc, parts, between)
    expect = []
    for i, part in enumerate(parts):
        expect.append(("read", tuple(name for name, _ in part)))
        if i < len(parts) - 1:
            expect.append(("between", sorted(t for _, members in part for t, _ in members)))
    assert events == expect
    assert not set(out) & set(given) and {**given, **out}.keys() == set(tags)
    assert {t: (r.value, r.error) for t, r in {**given, **out}.items()} == direct(plc, tags)
    assert services == len(reads_) and failed == []


def test_failed_group_is_read_after_the_last_chunk(plc):
    plc.plc.errors["Line1.Motor"] = "General error"
    tags = ["Line1.Motor.Speed", "Line1.Motor.Current", "Speed", "Count", "Flags", "Name1"]
    parts = chunks(plan(tags, plc.get_tag_info, 1500, budget=150), plc.get_tag_info, 150)
    assert len(parts) >= 2 and parts[0][0][0] == "Line1.Motor"
    plc.plc.reads.clear()
    given = {}
    out, failed, services = execute(plc, parts, given.update)
    assert len(plc.plc.reads) == len(parts) + 1 and plc.plc.reads[-1] == ("Line1.Motor.Speed", "Line1.Motor.Current")
    assert failed == ["Line1.Motor"] and "Line1.Motor.Speed" in out and "Line1.Motor.Speed" not in given
    assert {t: (r.value, r.error) for t, r in {**given, **out}.items()} == direct(plc, tags)


def test_execute_takes_a_plain_plan_or_nothing(plc):
    out, failed, services = execute(plc, plan(["Speed", "Count"], plc.get_tag_info, 1500))
    assert {t: r.value for t, r in out.items()} == {"Speed": 3.5, "Count": 9} and services == 2
    assert execute(plc, []) == ({}, [], 0)


def test_request_size_follows_the_instance_id_and_assumes_16_bits():
    infos = {"small": {"instance_id": 5}, "mid": {"instance_id": 300}, "big": {"instance_id": 70000}}
    get = lambda name: infos.get(name.lower())
    assert [request_size(f"{n}.Level", get) for n in ("small", "mid", "big")] == [18, 20, 22]
    assert request_size("Motor7.Level") == request_size("mid.Level", get) == 20  # unknown: 16 bits


def test_a_run_that_turns_fragmented_is_not_taken_for_free():
    from ciplink_tagserver.planner import VERSION_REQUEST
    infos = {"trend": {"tag_type": "atomic", "data_type_name": "REAL", "dim": 1, "dimensions": [2000, 0, 0]}}
    get = lambda name: infos.get(name.lower().split("[")[0])
    tags = [f"Trend[{i}]" for i in range(0, 1000, 2)]
    assert packets([read_size("Trend[0]{999}", get)], 4000) == 2 + (2 if VERSION_REQUEST else 0)
    if VERSION_REQUEST:  # ciplink sends it as check, 2 fragments, check: more than the 2 packets of plain reads
        assert len(plan(tags, get, 1500, budget=4000)) == len(tags)
