import pytest

from ciplink_tagserver.planner import execute, packets, plan, reply_size, request_size, tokenize

from conftest import FakeDriver, FakePLC, plant, tag_def, udt


@pytest.fixture
def plc():
    fake = FakePLC()
    plant(fake)
    return FakeDriver(fake)


def reads(plc, tags, whole_max_bytes=1500, no_group=frozenset(), budget=4000):
    """{read name: sorted requested tags it serves}"""
    return {name: sorted(t for t, _ in members)
            for name, members in plan(tags, plc.get_tag_info, whole_max_bytes, no_group, budget)}


def direct(plc, tags):
    """What reading each tag on its own gives: (value, error)."""
    return {tag: (r.value, r.error) for tag, r in zip(tags, plc.read(*tags))}


def run(plc, tags, **kw):
    out, failed, services = execute(plc, plan(tags, plc.get_tag_info, 1500, **kw))
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
    budget = 52 + sum(r for _, r in plain) + 10  # room for the plain replies, not for the whole one
    assert packets(plain, budget) == 1 and packets(group, budget) == 2
    assert reads(plc, tags, budget=budget) == {t: [t] for t in tags}
    assert reads(plc, tags, budget=4000) == {"Line1.Motor": tags[::-1]}


def test_packet_estimate_packs_in_order():
    assert packets([(30, 1000)] * 3, 4000) == 1
    assert packets([(30, 1000)] * 4, 4000) == 2  # 52 bytes of reply overhead per packet
    assert packets([(30, 5000)], 4000) == 2  # fragmented
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
