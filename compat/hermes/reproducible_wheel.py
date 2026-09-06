"""Normalize only source-root references in Maturin's generated CycloneDX SBOM.

Native code, package sources, licenses and dependency relationships are retained.
The caller audits the original wheel first and audits the rewritten RECORD again.
"""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile


def relocate_source(value, source):
    prefix = "path+" + source.resolve().as_uri()
    def rewrite(item):
        if isinstance(item, str):
            if item.startswith(prefix) and item[len(prefix):len(prefix) + 1] in ("", "/", "#"):
                return "path+file:///hermes-source" + item[len(prefix):]
            return item
        if isinstance(item, list):
            return [rewrite(child) for child in item]
        if isinstance(item, dict):
            return {key: rewrite(child) for key, child in item.items()}
        return item
    return rewrite(value)


def normalize(path: Path, source: Path):
    with zipfile.ZipFile(path) as archive:
        contents = {item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()}
    records = [name for name in contents if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ValueError("Expected exactly one audited wheel RECORD")
    for name, payload in list(contents.items()):
        if ".dist-info/sboms/" in name and name.endswith(".cyclonedx.json"):
            bom = json.loads(payload)
            if bom.get("bomFormat") != "CycloneDX":
                raise ValueError("Unexpected generated SBOM format")
            contents[name] = (json.dumps(relocate_source(bom, source), sort_keys=True, indent=2) + "\n").encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, payload in sorted(contents.items()):
        if name == records[0]:
            writer.writerow((name, "", ""))
        else:
            digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
            writer.writerow((name, "sha256=" + digest, len(payload)))
    contents[records[0]] = record.getvalue().encode()
    temporary = path.with_suffix(".normalized")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in sorted(contents.items()):
            entry = zipfile.ZipInfo(name, (1980, 2, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, payload)
    temporary.replace(path)
