"""Build integration and optional card archives from source."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Package source files only, excluding caches and runtime diagnostics."""
    source = ROOT / "custom_components" / "onlycat"
    version = json.loads((source / "manifest.json").read_text())["version"]
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    with ZipFile(output / f"onlycat-{version}.zip", "w", ZIP_DEFLATED) as archive:
        archive.write(ROOT / "LICENSE", "onlycat/LICENSE")
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".yaml"}:
                archive.write(path, Path("onlycat") / path.relative_to(source))
    with ZipFile(
        output / f"onlycat-activity-card-{version}.zip", "w", ZIP_DEFLATED
    ) as archive:
        archive.write(ROOT / "LICENSE", "LICENSE")
        for name in ["onlycat-activity-card.js", "README.md"]:
            archive.write(ROOT / "timeline-card" / name, name)
    print(f"Built integration and card archives in {output}")


if __name__ == "__main__":
    main()
