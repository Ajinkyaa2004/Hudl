"""How big is the box this runs on. A container's memory limit (cgroup v2 or v1) decides cache
sizes and how many searches run at once: Render Free has 512 MB, the NAS gives the container
1.2 GB, a laptop has no limit. BOX_MEM_MB overrides it for tests."""
from __future__ import annotations
import os


def mem_limit_mb() -> int:
    """The memory limit in MB, or 0 when there is none."""
    if os.environ.get("BOX_MEM_MB"):
        return int(os.environ["BOX_MEM_MB"])
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v.isdigit() and int(v) < 1 << 50:
                return int(v) // 1048576
        except Exception:
            pass
    return 0


def small() -> bool:
    """Under about 600 MB: Render Free."""
    lim = mem_limit_mb()
    return bool(lim) and lim <= 600


def cache_size(normal: int) -> int:
    """A cache's entry count for this box: a quarter of normal on a small box, half on the NAS."""
    lim = mem_limit_mb()
    if not lim:
        return normal
    return normal // 4 if lim <= 600 else normal // 2 if lim <= 1600 else normal
