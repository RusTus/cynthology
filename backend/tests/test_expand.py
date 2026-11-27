import os

os.environ["CYN_DEV"] = "1"

from cynthology.storage import ALIGN, MIN_GROW, plan_expand  # noqa: E402

G = 1 << 21          # 1 GiB in sectors


def disk(total_g, parts):
    """parts: list of sizes in GiB, laid out from 1 MiB."""
    start, ps = ALIGN, {}
    for n, sz in enumerate(parts, start=1):
        ps[n] = (start, sz * G)
        start += sz * G
    return {"free_start": start, "end": total_g * G, "parts": ps}


def test_shr_two_drives_replaced_with_bigger():
    # 2+2+4+4+4+4 GB SHR: tier 1 (2 GB) on all six, tier 2 (2 GB) on the four 4 GB drives.
    tiers = [{"part": 1, "level": "5", "size": 2 * G, "members": ["a", "b", "c", "d", "e", "f"]},
             {"part": 2, "level": "5", "size": 2 * G, "members": ["c", "d", "e", "f"]}]
    layout = {x: disk(4, [2, 2]) for x in "cdef"}
    layout["a"] = disk(8, [2])        # replaced by 8 GB drives
    layout["b"] = disk(8, [2])
    plan = plan_expand("shr", tiers, layout)
    joins = [s for s in plan["steps"] if s["do"] == "join"]
    new = [s for s in plan["steps"] if s["do"] == "new_tier"]
    assert sorted(j["disk"] for j in joins) == ["a", "b"] and all(j["part"] == 2 for j in joins)
    assert len(new) == 1 and new[0]["disks"] == ["a", "b"] and new[0]["level"] == "1" and new[0]["part"] == 3
    assert abs(new[0]["size"] - 4 * G) <= ALIGN and abs(plan["gain"] - 8 * G * 512) < 4 * ALIGN * 512


def test_shr_single_bigger_drive_waits_for_a_second():
    tiers = [{"part": 1, "level": "1", "size": 4 * G, "members": ["a", "b"]}]
    plan = plan_expand("shr", tiers, {"a": disk(8, [4]), "b": disk(4, [4])})
    assert plan["steps"] == [] and plan["gain"] == 0      # (8 GB next to 4 GB: no redundancy for the extra 4)


def test_raid5_grows_partitions_when_all_are_bigger():
    tiers = [{"part": 1, "level": "5", "size": 2 * G, "members": ["a", "b", "c"]}]
    lay = {x: disk(4, [2]) for x in "abc"}
    plan = plan_expand("raid5", tiers, lay)
    assert plan["steps"][0]["do"] == "grow_part" and plan["gain"] >= 2 * (2 * G - ALIGN) * 512
    lay["c"] = disk(2, [2])                                   # one drive still small: nothing to do
    assert plan_expand("raid5", tiers, lay)["steps"] == []


def test_tiny_leftovers_ignored():
    tiers = [{"part": 1, "level": "1", "size": 4 * G, "members": ["a", "b"]}]
    lay = {"a": disk(4, [4]), "b": disk(4, [4])}
    lay["a"]["end"] += MIN_GROW // 2
    lay["b"]["end"] += MIN_GROW // 2
    assert plan_expand("raid1", tiers, lay)["steps"] == []


def test_interrupted_expansion_is_finished():
    # tier 2 already has b's partition as an idle spare (an earlier expansion stopped half-way)
    tiers = [{"part": 1, "level": "5", "size": 2 * G, "members": ["a", "b", "c"]},
             {"part": 2, "level": "5", "size": 2 * G, "members": ["a", "c", "d"]}]
    lay = {"a": disk(8, [2, 2]), "b": disk(8, [2, 2]), "c": disk(4, [2, 2]), "d": disk(4, [2, 2])}
    plan = plan_expand("shr", tiers, lay, idle={2: ["b"]})
    assert {"do": "activate", "disk": "b", "part": 2, "size": 2 * G} in plan["steps"]
    assert not [s for s in plan["steps"] if s["do"] == "join"]
    assert [s["disks"] for s in plan["steps"] if s["do"] == "new_tier"] == [["a", "b"]]


def test_mdadm_conf_lines_clean_and_stable():
    from cynthology.storage import mdadm_conf_lines
    scan = ('mdadm: Value "cyntest:cyn1t3" cannot be set as devname. Reason: Not POSIX compatible. Value ignored.\n'
            "ARRAY /dev/md/cyn1t1 metadata=1.2 UUID=aa\n"
            "ARRAY /dev/md/cyntest:cyn1t3 metadata=1.2 UUID=bb\n"
            "ARRAY /dev/md/oldname:cyn2c metadata=1.2 UUID=cc\n"
            "ARRAY /dev/md/other:data metadata=1.2 UUID=dd\n")
    assert mdadm_conf_lines(scan) == ["ARRAY /dev/md/cyn1t1 metadata=1.2 UUID=aa", "ARRAY /dev/md/cyn1t3 metadata=1.2 UUID=bb",
                                      "ARRAY /dev/md/cyn2c metadata=1.2 UUID=cc", "ARRAY /dev/md/other:data metadata=1.2 UUID=dd"]
