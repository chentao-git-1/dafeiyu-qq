"""Bound decoded images before the upstream frame-expanding processors run."""
import pathlib
import time

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 2_000_000
MAX_FRAMES = 120
MAX_TOTAL_PIXELS = 8_000_000


def validate_image(path):
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise ValueError('image dimensions exceed the pixel budget')
        frames = getattr(image, 'n_frames', 1)
        if frames > MAX_FRAMES or width * height * frames > MAX_TOTAL_PIXELS:
            raise ValueError('animation exceeds the decoded frame budget')
        return {'width': width, 'height': height, 'frames': frames, 'format': image.format}


def download_image(url, path):
    import requests
    if not str(url).startswith(('http://', 'https://')):
        raise ValueError('unsupported image address')
    started = time.monotonic()
    try:
        with requests.get(url, stream=True, timeout=(5, 10), headers={'User-Agent':'AstrBot/pic_toolbox'}) as response:
            response.raise_for_status()
            if int(response.headers.get('Content-Length', 0)) > MAX_BYTES:
                raise ValueError('download exceeds the size budget')
            total = 0
            with open(path, 'wb') as output:
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_BYTES or time.monotonic() - started > 30:
                        raise ValueError('download exceeds the size/time budget')
                    output.write(chunk)
        return validate_image(path)
    except BaseException:
        pathlib.Path(path).unlink(missing_ok=True)
        raise
