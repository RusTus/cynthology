"""Thumbnail worker: turns ONE already-open file (descriptor given as argument) into a WebP thumbnail on stdout.

Runs as the unprivileged account cyn-thumb with no capabilities, no new privileges and memory
and CPU limits (see preview.py): image, video and PDF decoders are big C libraries that read
files users uploaded, so they never run as root. It can't open any file by itself - it only
gets the descriptor the web service opened after checking the user's access.

    python3 -m cynthology.thumbworker <kind> <size> <fd>      kind: image | video | pdf
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile


def _webp(img, size: int) -> bytes:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(img)          # (phone photos: honour the rotation flag)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=80 if size <= 512 else 85, method=4)
    return out.getvalue()


FD = 3


def _ffmpeg_frame(size: int, seek: str | None) -> bytes:
    """A frame (video) or the picture (HEIC/AVIF) via ffmpeg, reading the descriptor directly."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if seek:
        cmd += ["-ss", seek]
    cmd += ["-fd", str(FD), "-i", "fd:", "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", "-f", "image2pipe",
            "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True, pass_fds=(FD,), timeout=40)
    if r.returncode != 0 or not r.stdout:
        if seek:                                 # (shorter than the seek point: take the first frame)
            os.lseek(FD, 0, os.SEEK_SET)
            return _ffmpeg_frame(size, None)
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    return r.stdout


def main() -> None:
    global FD
    kind, size, FD = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 200_000_000         # (decompression bombs: refuse absurd sizes)
    if kind == "image":
        with os.fdopen(FD, "rb", closefd=False) as f:
            try:
                img = Image.open(f)
                img.draft("RGB", (size, size))   # (JPEG: decode at reduced size - much faster)
                data = _webp(img, size)
            except Exception:
                f.seek(0)
                data = _webp(Image.open(io.BytesIO(_ffmpeg_frame(size, None))), size)   # HEIC/AVIF via ffmpeg
    elif kind == "video":
        data = _webp(Image.open(io.BytesIO(_ffmpeg_frame(size, "00:00:02"))), size)
    elif kind == "pdf":
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.pdf")
            with os.fdopen(FD, "rb", closefd=False) as f, open(src, "wb") as out:
                shutil.copyfileobj(f, out)
            r = subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", str(size), "-png", src,
                                os.path.join(d, "p")], capture_output=True, timeout=40)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
            data = _webp(Image.open(os.path.join(d, "p.png")), size)
    else:
        raise SystemExit(2)
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
