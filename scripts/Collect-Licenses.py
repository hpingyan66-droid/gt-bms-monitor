"""Collect installed distribution metadata and license files for redistribution."""
from importlib.metadata import distributions
from pathlib import Path
import json

root = Path("artifacts/licenses")
root.mkdir(parents=True, exist_ok=True)
index = []
for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = dist.metadata["Name"]
    record = {"name": name, "version": dist.version, "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"), "files": []}
    for file in dist.files or []:
        if any(word in file.name.lower() for word in ("license", "copying", "notice")) and ".dist-info" in str(file):
            source = Path(dist.locate_file(file))
            if source.is_file():
                target = root/name/file.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                record["files"].append(str(target))
    index.append(record)
(root/"index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Collected {len(index)} distributions into {root}")
