"""Fix the confirmed equal-millisecond initialization race in pinned Superset 6.1.0.

Upstream src/pages/SqlLab/index.tsx must consider an equal timestamp initialized:
resetState's queriesLastUpdate can equal the fulfilledTimeStamp it consumed.
Fail the image build if the pinned asset changes; review this on every upgrade.
"""

from hashlib import sha256
from pathlib import Path

asset = Path("/app/superset/static/assets/bd752f465ad7ba9999cc.chunk.js")
original = asset.read_bytes()
if sha256(original).hexdigest() != "bd6b8f4becb09e4ec6e90a89860114c9ce2926c43674694ff993e7304dbd8726":
    raise RuntimeError("Review the SQL Lab initialization patch against the pinned asset.")
before = b"fulfilledTimeStamp:a}=P(),o=e<=(a||0)"
if original.count(before) != 1:
    raise RuntimeError("Expected exactly one SQL Lab initialization guard.")
asset.write_bytes(original.replace(before, before.replace(b"<=", b"<")))
