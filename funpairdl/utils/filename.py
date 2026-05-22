import re
from pathlib import Path


# Characters illegal in Windows filenames
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r'[\s.]+$')


def sanitize_filename(name: str, max_length: int = 200) -> str:
    name = _ILLEGAL_CHARS.sub("_", name)
    name = _TRAILING_DOTS_SPACES.sub("", name)
    name = " ".join(name.split())  # collapse whitespace

    if len(name) > max_length:
        name = name[:max_length].rstrip()

    return name or "unnamed"


def make_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1

    while True:
        new_path = parent / f"{stem} ({counter}){suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def parse_content_disposition(cd: str) -> str:
    """Extract filename from a Content-Disposition header value.

    Handles:
      filename*=UTF-8''encoded%20name.ext  (RFC 5987)
      filename="quoted name.ext"
      filename=unquoted.ext
    """
    from urllib.parse import unquote

    if not cd:
        return ""

    # Prefer filename* (RFC 5987 encoded)
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''(.+?)(?:;|$)", cd)
    if m:
        return unquote(m.group(1).strip().strip('"'))

    # Fall back to filename=
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd)
    if m:
        return m.group(1)

    m = re.search(r"filename\s*=\s*([^\s;]+)", cd)
    if m:
        return m.group(1).strip('"').strip("'")

    return ""


def guess_file_type(filename: str) -> str:
    from funpairdl.constants import SCRIPT_EXTENSIONS, VIDEO_EXTENSIONS

    suffix = Path(filename).suffix.lower()
    if suffix in SCRIPT_EXTENSIONS:
        return "funscript"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "other"
