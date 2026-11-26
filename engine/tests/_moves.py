"""Test helper: run code around every file move the publisher makes (`os.replace` and its one-step
no-replace rename)."""

from pathlib import Path

from dieline_engine import publish


def hook_moves(monkeypatch, before=None, after=None):
    real_replace, real_noreplace = publish.os.replace, publish.rename_noreplace

    def wrap(real):
        def move(src, dst, *a, **kw):
            if before is not None:
                before(Path(src), Path(dst))
            out = real(src, dst, *a, **kw)
            if after is not None:
                after(Path(src), Path(dst))
            return out
        return move

    monkeypatch.setattr(publish.os, "replace", wrap(real_replace))
    monkeypatch.setattr(publish, "rename_noreplace", wrap(real_noreplace))
