from types import SimpleNamespace

import pytest

from app.document_destinations import DocumentDestinations


def test_save_as_respects_the_selected_destination_and_foreign_changes(tmp_path, monkeypatch):
    source = tmp_path / "source.beamweft"
    source.write_bytes(b"original")
    target = tmp_path / "other.beamweft"
    choices = iter(["", str(target), str(target), str(target), str(target)])
    monkeypatch.setattr("app.document_destinations.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(choices) + "\n"))
    locations = DocumentDestinations()
    assert locations.choose(source) is None

    selection = locations.choose(source)
    destination = locations.take(selection)
    with pytest.raises(ValueError, match="已失效"):
        locations.take(selection)
    assert destination.write(lambda stage: stage.write_bytes(b"copy"))["filename"] == "other.beamweft"
    assert target.read_bytes() == b"copy"

    selected = locations.take(locations.choose(source))
    def external_save_while_packaging(stage):
        stage.write_bytes(b"delayed")
        target.write_bytes(b"collaborator")

    with pytest.raises(ValueError, match="已被其他程序改动"):
        selected.write(external_save_while_packaging)
    assert target.read_bytes() == b"collaborator"
    assert source.read_bytes() == b"original"
    assert list(tmp_path.glob(".beamweft-*")) == []

    selected = locations.take(locations.choose(source))
    selected.write(lambda stage: stage.write_bytes(b"confirmed replacement"))
    assert target.read_bytes() == b"confirmed replacement"

    target.unlink()
    selected = locations.take(locations.choose(source))
    with pytest.raises(ValueError, match="已被其他程序改动"):
        selected.write(external_save_while_packaging)
    assert target.read_bytes() == b"collaborator"
