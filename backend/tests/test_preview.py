import io
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from cynthology import preview  # noqa: E402


def test_kinds():
    assert preview.kind_of("a/IMG_0001.HEIC") == "image" and preview.kind_of("x.mkv") == "video"
    assert preview.kind_of("doc.pdf") == "pdf" and preview.kind_of("song.flac") == "audio"
    assert preview.kind_of("notes.md") == "text" and preview.kind_of("archive.zip") is None


def test_image_thumbnail_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "CACHE", str(tmp_path / "cache"))
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (3000, 2000), (200, 30, 30)).save(src, "JPEG")
    data = preview.make_thumb(str(src), 256)
    im = Image.open(io.BytesIO(data))
    assert im.format == "WEBP" and max(im.size) == 256
    assert preview.make_thumb(str(src), 256) == data                         # from the cache
    assert sum(len(f) for _, _, f in os.walk(tmp_path / "cache")) == 1


def test_bad_file_gives_clean_error(tmp_path, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(preview, "CACHE", str(tmp_path / "cache"))
    src = tmp_path / "broken.jpg"
    src.write_bytes(b"not an image at all")
    with pytest.raises(HTTPException) as e:
        preview.make_thumb(str(src), 256)
    assert e.value.status_code == 422


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
def test_video_thumbnail(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(preview, "CACHE", str(tmp_path / "cache"))
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    im = Image.open(io.BytesIO(preview.make_thumb(str(src), 256)))
    assert im.size[0] == 256


@pytest.mark.skipif(not __import__("shutil").which("pdftoppm"), reason="poppler not installed")
def test_pdf_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "CACHE", str(tmp_path / "cache"))
    src = tmp_path / "doc.pdf"
    Image.new("RGB", (595, 842), "white").save(src, "PDF")
    im = Image.open(io.BytesIO(preview.make_thumb(str(src), 256)))
    assert max(im.size) == 256
