"""Pure RAID layout planning (no side effects - unit tested).

Supported layouts
-----------------
basic   single disk (or JBOD/linear of several)
raid0   striping, no redundancy
raid1   mirror
raid5   1-disk parity
raid6   2-disk parity
raid10  striped mirrors
shr     SHR  - Synology-style Hybrid RAID, 1-disk fault tolerance
shr2    SHR-2 - Hybrid RAID, 2-disk fault tolerance

SHR is reimplemented from scratch here (no Synology code), built only from stock Linux
tools (mdadm + LVM). Disks of mixed sizes are cut into tiers at every distinct
disk size. Each tier becomes its own md array spanning every disk that reaches it
(RAID5/6 when enough members, else RAID1 mirror), and all tier arrays are joined
into one LVM volume group so the user sees a single volume. This lets
4+4+8+8 TB give 12 TB usable instead of the 12 TB / 8 TB you'd get with plain RAID5.
"""
from dataclasses import dataclass, field

MiB = 1024 * 1024
HEAD_MIB = 1          # GPT + alignment at start of disk
TAIL_MIB = 16         # spare at end so a replacement disk a few MB smaller still fits

MIN_DISKS = {"basic": 1, "raid0": 2, "raid1": 2, "raid5": 3, "raid6": 4, "raid10": 4,
             "shr": 1, "shr2": 4}
LABELS = {"basic": "Basic", "raid0": "RAID 0", "raid1": "RAID 1", "raid5": "RAID 5",
          "raid6": "RAID 6", "raid10": "RAID 10", "shr": "SHR (Hybrid RAID, 1-disk protection)",
          "shr2": "SHR-2 (Hybrid RAID, 2-disk protection)"}
TOLERANCE = {"basic": 0, "raid0": 0, "raid1": 1, "raid5": 1, "raid6": 2, "raid10": 1, "shr": 1, "shr2": 2}


@dataclass
class Tier:
    level: str                 # mdadm level: "1", "5", "6", "0", "10", "linear"
    start_mib: int             # partition start on each member disk
    size_mib: int              # partition size on each member disk
    members: list[str]         # disk names
    usable_mib: int


@dataclass
class Plan:
    layout: str
    tiers: list[Tier] = field(default_factory=list)
    usable_bytes: int = 0
    raw_bytes: int = 0
    tolerance: int = 0
    warnings: list[str] = field(default_factory=list)


def _usable(level: str, n: int, size: int) -> int:
    return {"linear": n * size, "0": n * size, "1": size, "5": (n - 1) * size,
            "6": (n - 2) * size, "10": (n // 2) * size}[level]


def _hybrid_level(n: int, redundancy: int) -> str | None:
    if redundancy == 1:
        return "5" if n >= 3 else "1" if n == 2 else None
    return "6" if n >= 4 else "1" if n == 3 else None   # 3-way mirror still survives 2 failures


def plan(layout: str, disks: dict[str, int]) -> Plan:
    """disks: name -> size in bytes. Raises ValueError on an impossible layout."""
    if layout not in MIN_DISKS:
        raise ValueError(f"unknown layout {layout}")
    if len(disks) < MIN_DISKS[layout]:
        raise ValueError(f"{LABELS[layout]} needs at least {MIN_DISKS[layout]} disks")
    if layout == "raid10" and len(disks) % 2:
        raise ValueError("RAID 10 needs an even number of disks")

    avail = {d: s // MiB - HEAD_MIB - TAIL_MIB for d, s in disks.items()}
    p = Plan(layout=layout, raw_bytes=sum(disks.values()), tolerance=TOLERANCE[layout])
    names = sorted(avail, key=lambda d: (avail[d], d))

    if layout in ("shr", "shr2"):
        red = 1 if layout == "shr" else 2
        prev = 0
        for cut in sorted(set(avail.values())):
            members = [d for d in names if avail[d] >= cut]
            size = cut - prev
            level = _hybrid_level(len(members), red)
            if level is None:
                p.warnings.append(f"{size * MiB / 1e12:.2f} TB on {', '.join(members)} is unprotected "
                                  f"and left unused - add another disk this size to use it")
            else:
                p.tiers.append(Tier(level, HEAD_MIB + prev, size, members, _usable(level, len(members), size)))
            prev = cut
        if len(disks) == 1:
            # single disk SHR: no protection yet, but upgradeable by adding a disk later
            d = names[0]
            p.tiers = [Tier("1", HEAD_MIB, avail[d], [d], avail[d])]
            p.tolerance = 0
            p.warnings = ["One disk gives no protection. Add a second disk later to make it redundant."]
    else:
        smallest = min(avail.values())
        level = {"basic": "linear", "raid0": "0", "raid1": "1", "raid5": "5", "raid6": "6", "raid10": "10"}[layout]
        if level == "linear":
            # JBOD: each disk contributes its full size
            p.tiers.append(Tier(level, HEAD_MIB, 0, names, sum(avail.values())))
        else:
            p.tiers.append(Tier(level, HEAD_MIB, smallest, names, _usable(level, len(names), smallest)))
            unused = sum(a - smallest for a in avail.values())
            if unused:
                p.warnings.append(f"{unused * MiB / 1e12:.2f} TB unused because disks differ in size "
                                  f"(SHR would use it)")

    p.usable_bytes = sum(t.usable_mib for t in p.tiers) * MiB
    return p


def compare(disks: dict[str, int]) -> list[dict]:
    """Usable capacity of every layout for these disks (for the UI's 'which RAID?' helper)."""
    out = []
    for layout in LABELS:
        try:
            pl = plan(layout, disks)
            out.append({"layout": layout, "label": LABELS[layout], "usable": pl.usable_bytes,
                        "tolerance": pl.tolerance, "warnings": pl.warnings, "ok": True})
        except ValueError as e:
            out.append({"layout": layout, "label": LABELS[layout], "ok": False, "error": str(e)})
    return out
