"""Import EVERY module of the registry package, from the image's own layout.

Staging 2026-10-10/11: society/work_packet.py computed ``parents[4]`` at import;
inside the image (/app/app/society/) that raised ``IndexError: 4`` and 37 society
runs went DEAD for ~20h while CI was green -- the entry-module smoke never
imported that module. ``scripts/ci/check_images.sh`` runs this with ``python -c``
from the image's /app (tests/test_image_imports.py from a copy in that layout).
Prints one line per failing module and exits 1; no database or Redis is contacted.
"""

import importlib
import pkgutil
import sys

import app

failed = []
names = sorted(m.name for m in pkgutil.walk_packages(app.__path__, "app.", onerror=lambda n: failed.append(f"{n}: package walk failed")))
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 -- every failure is reported, none hides another
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
for line in failed:
    print("IMPORT FAILED", line)
print(f"registry modules imported: {len(names) - len(failed)}/{len(names)}")
sys.exit(1 if failed or not names else 0)
