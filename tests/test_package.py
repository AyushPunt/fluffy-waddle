import io
import zipfile

import pytest

from docagent.errors import PackageError
from docagent.ooxml.package import Package


def test_rebuild_without_changes_is_byte_identical(template):
    assert Package.from_bytes(template).rebuild({}) == template


def test_replacing_one_part_copies_every_other_record_raw(template):
    pkg = Package.from_bytes(template)
    new = pkg.rebuild({"word/settings.xml": b"<x/>"})
    out = Package.from_bytes(new)
    assert out.names == pkg.names
    for name in pkg.names:
        if name != "word/settings.xml":
            assert out.entry(name).raw_record == pkg.entry(name).raw_record
    assert out.part("word/settings.xml") == b"<x/>"
    with zipfile.ZipFile(io.BytesIO(new)) as z:
        assert z.testzip() is None


def test_fingerprint_covers_every_part(template):
    fp = Package.from_bytes(template).fingerprint()
    assert set(fp) == set(Package.from_bytes(template).names)
    assert all(len(h) == 64 for h in fp.values())


def test_refuses_non_zip():
    with pytest.raises(PackageError):
        Package.from_bytes(b"not a zip at all")


def test_refuses_duplicate_entry_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<a/>")
        with pytest.warns(UserWarning):
            z.writestr("word/document.xml", "<b/>")
    with pytest.raises(PackageError, match="duplicate"):
        Package.from_bytes(buf.getvalue())


def test_detects_corrupted_data(template):
    pkg = Package.from_bytes(template)
    entry = pkg.entry("word/document.xml")
    start = template.index(entry.raw_record) + len(entry.raw_record) - 10
    broken = template[:start] + bytes([template[start] ^ 0xFF]) + template[start + 1 :]
    with pytest.raises(PackageError):
        Package.from_bytes(broken).part("word/document.xml")
