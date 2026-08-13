import os
import subprocess
import types
import sys

import pytest


@pytest.fixture
def rf():
    from server.read_file import read_file_text
    return read_file_text


def _write(path, data=b"x"):
    with open(path, "wb") as f:
        f.write(data)
    return path


class TestUnknownExt:
    def test_unknown_ext_reads_as_text(self, rf, tmp_path):
        p = _write(str(tmp_path / "a.xyz"), data=b"hello text")
        assert rf(p) == "hello text"

    def test_unknown_ext_falls_back_to_latin1(self, rf, tmp_path):
        p = _write(str(tmp_path / "a.xyz"), data=b"\xff\xfe bytes")
        assert rf(p) == "\xff\xfe bytes"

    def test_missing_file_raises(self, rf, tmp_path):
        with pytest.raises(FileNotFoundError):
            rf(str(tmp_path / "nope.pdf"))


class TestPdf:
    def test_fitz_path(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "a.pdf"))

        class FakePage:
            def get_text(self):
                return "<p>Hello <b>world</b></p>"

        class FakeDoc:
            def __init__(self, *a, **k):
                pass

            def __iter__(self):
                yield FakePage()

            def close(self):
                pass

        fake_fitz = types.SimpleNamespace(open=lambda **k: FakeDoc())
        monkeypatch.setitem(sys.modules, "fitz", fake_fitz)
        out = rf(p)
        assert out == "Hello world"

    def test_pdftotext_fallback(self, rf, monkeypatch, tmp_path):
        import builtins
        p = _write(str(tmp_path / "b.pdf"))

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "fitz":
                raise ImportError("no fitz")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        class FakeProc:
            returncode = 0
            stdout = "pdftotext content"

        def fake_run(cmd, **k):
            assert "pdftotext" in cmd
            return FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert rf(p) == "pdftotext content"


class TestDocx:
    def test_extracts_paragraphs(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "c.docx"))

        class FakePara:
            def __init__(self, text):
                self.text = text

        class FakeDocument:
            def __init__(self, stream):
                self.stream = stream
                self.paragraphs = [FakePara("line one"), FakePara("line two")]

        fake_docx = types.SimpleNamespace(Document=FakeDocument)
        monkeypatch.setitem(sys.modules, "docx", fake_docx)
        assert rf(p) == "line one\nline two"


class TestDoc:
    def test_catdoc_success(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "d.doc"))

        def fake_run(cmd, **k):
            return types.SimpleNamespace(returncode=0, stdout="catdoc out")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert rf(p) == "catdoc out"

    def test_antiword_fallback(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "e.doc"))
        calls = []

        def fake_run(cmd, **k):
            calls.append(cmd[0])
            if cmd[0] == "catdoc":
                return types.SimpleNamespace(returncode=1, stdout="")
            return types.SimpleNamespace(returncode=0, stdout="antiword out")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert rf(p) == "antiword out"
        assert calls == ["catdoc", "antiword"]


class TestXlsx:
    def test_reads_rows(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "f.xlsx"))

        class FakeSheet:
            def iter_rows(self, values_only=True):
                yield [1, None, "three"]
                yield [4, 5, 6]

        class FakeWB:
            def __init__(self, *a, **k):
                self.worksheets = [FakeSheet()]

            def close(self):
                pass

        fake_openpyxl = types.SimpleNamespace(load_workbook=lambda *a, **k: FakeWB())
        monkeypatch.setitem(sys.modules, "openpyxl", fake_openpyxl)
        assert rf(p) == "1\t\tthree\n4\t5\t6"

    def test_xls_extension_also_supported(self, rf, monkeypatch, tmp_path):
        p = _write(str(tmp_path / "g.xls"))

        class FakeSheet:
            def iter_rows(self, values_only=True):
                yield ["a", "b"]

        class FakeWB:
            def __init__(self, *a, **k):
                self.worksheets = [FakeSheet()]

            def close(self):
                pass

        monkeypatch.setitem(
            sys.modules, "openpyxl", types.SimpleNamespace(load_workbook=lambda *a, **k: FakeWB())
        )
        assert rf(p) == "a\tb"
