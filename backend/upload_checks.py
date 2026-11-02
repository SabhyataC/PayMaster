import io
import re

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_PDF_PAGES = 20
MAX_IMAGE_PIXELS = 25_000_000
MAX_NAME_CHARS = 200

FORMATS = {".pdf": "text_pdf", ".png": "scan", ".jpg": "scan", ".jpeg": "scan",
           ".txt": "email", ".eml": "email"}

ACTIVE_PDF_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/OpenAction", b"/AA")


class UploadRejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()
    return name[:MAX_NAME_CHARS]


def _check_pdf(raw: bytes):
    if not raw.startswith(b"%PDF-"):
        raise UploadRejected(422, "This is not a real PDF")
    head = raw[:4096]
    if b"%PDF-" not in head:
        raise UploadRejected(422, "This is not a real PDF")
    for marker in ACTIVE_PDF_MARKERS:
        if re.search(re.escape(marker) + rb"(?![A-Za-z])", raw):
            raise UploadRejected(422, "PDFs with scripts, launch actions or embedded files are not accepted")
    try:
        import pymupdf
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise UploadRejected(422, "The PDF is damaged or unreadable")
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise UploadRejected(422, "Password-protected PDFs are not accepted")
        if doc.page_count < 1:
            raise UploadRejected(422, "The PDF has no pages")
        if doc.page_count > MAX_PDF_PAGES:
            raise UploadRejected(413, f"The PDF has {doc.page_count} pages; the limit is {MAX_PDF_PAGES}")
    finally:
        doc.close()


def _check_image(raw: bytes, fmt_name: str):
    from PIL import Image
    magic = {"png": raw.startswith(b"\x89PNG\r\n\x1a\n"), "jpg": raw.startswith(b"\xff\xd8\xff")}
    if not magic[fmt_name]:
        raise UploadRejected(422, "This is not a real image of that type")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.verify()
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            real = (img.format or "").lower()
    except Exception:
        raise UploadRejected(422, "The image is damaged or too large")
    if width * height > MAX_IMAGE_PIXELS:
        raise UploadRejected(413, "The image has too many pixels")
    if real not in ("png", "jpeg"):
        raise UploadRejected(422, "This is not a real image of that type")


def _check_text(raw: bytes):
    if len(raw) > MAX_TEXT_BYTES:
        raise UploadRejected(413, "Email text is larger than 1 MB")
    if b"\x00" in raw:
        raise UploadRejected(422, "This is not a text file")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadRejected(422, "Email text must be UTF-8")


def validate_upload(filename: str, raw: bytes):
    """Returns (clean_name, source_format). Raises UploadRejected with a status and message."""
    name = clean_name(filename)
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    fmt = FORMATS.get(suffix)
    if not fmt:
        raise UploadRejected(422, "Upload a PDF, PNG, JPG, or an email as .txt/.eml")
    if not raw:
        raise UploadRejected(422, "The file is empty")
    if len(raw) > MAX_BYTES:
        raise UploadRejected(413, "File is larger than 10 MB")
    if suffix == ".pdf":
        _check_pdf(raw)
    elif suffix == ".png":
        _check_image(raw, "png")
    elif suffix in (".jpg", ".jpeg"):
        _check_image(raw, "jpg")
    else:
        _check_text(raw)
    return name, fmt
