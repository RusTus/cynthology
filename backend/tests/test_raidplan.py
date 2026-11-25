import pytest

from cairn.raidplan import MiB, plan

TB = 10**12


def tb(p):
    return round(p.usable_bytes / TB, 1)


def test_raid_levels_equal_disks():
    d = {f"sd{c}": 4 * TB for c in "abcd"}
    assert tb(plan("raid0", d)) == 16.0
    assert tb(plan("raid1", d)) == 4.0
    assert tb(plan("raid5", d)) == 12.0
    assert tb(plan("raid6", d)) == 8.0
    assert tb(plan("raid10", d)) == 8.0


def test_shr_equal_disks_matches_raid5():
    d = {f"sd{c}": 4 * TB for c in "abcd"}
    assert tb(plan("shr", d)) == tb(plan("raid5", d))
    assert tb(plan("shr2", d)) == tb(plan("raid6", d))


def test_shr_mixed_disks_beats_raid5():
    # 4+4+8+8 TB: RAID5 = 12 TB, SHR = 4*3 (raid5 tier) + 4*1 (raid1 tier) = 16 TB
    d = {"sda": 4 * TB, "sdb": 4 * TB, "sdc": 8 * TB, "sdd": 8 * TB}
    assert tb(plan("raid5", d)) == 12.0
    p = plan("shr", d)
    assert tb(p) == 16.0
    assert [t.level for t in p.tiers] == ["5", "1"]
    assert p.tiers[1].members == ["sdc", "sdd"]


def test_shr_tiers_are_contiguous_partitions():
    d = {"sda": 2 * TB, "sdb": 4 * TB, "sdc": 8 * TB}
    p = plan("shr", d)
    for a, b in zip(p.tiers, p.tiers[1:]):
        assert b.start_mib == a.start_mib + a.size_mib


def test_shr_single_largest_disk_space_unprotected():
    d = {"sda": 4 * TB, "sdb": 4 * TB, "sdc": 8 * TB}
    p = plan("shr", d)
    assert tb(p) == 8.0          # raid5 over 3 x 4 TB
    assert p.warnings            # the extra 4 TB on sdc has no partner


def test_shr_one_and_two_disks():
    assert plan("shr", {"sda": 4 * TB}).tolerance == 0
    assert tb(plan("shr", {"sda": 4 * TB, "sdb": 4 * TB})) == 4.0


def test_shr2_mixed():
    # 4,4,4,4,8,8,8: tier1 raid6 over 7 x 4 TB = 20, tier2 3-way mirror of 4 TB = 4 -> 24
    d = {f"sd{c}": 4 * TB for c in "abcd"} | {f"sd{c}": 8 * TB for c in "efg"}
    assert tb(plan("shr2", d)) == 24.0


@pytest.mark.parametrize("layout,n", [("raid1", 1), ("raid5", 2), ("raid6", 3), ("shr2", 3)])
def test_minimum_disks(layout, n):
    with pytest.raises(ValueError):
        plan(layout, {f"d{i}": 4 * TB for i in range(n)})
