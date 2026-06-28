import base64
import errno
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
SUBJECT_TEMPLATE_DIR = BASE_DIR / "materias"
ASSET_DIR = BASE_DIR / "assets"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
PHOTO_DB = DATA_DIR / "photos.json"

HOST = os.getenv("PORTAFOLIO_HOST", "127.0.0.1")
PORT = int(os.getenv("PORTAFOLIO_PORT", "5000"))
USERNAME = os.getenv("PORTAFOLIO_USUARIO", "admin")
PASSWORD = os.getenv("PORTAFOLIO_PASSWORD", "1234")
SECRET_KEY = os.getenv("PORTAFOLIO_SECRET") or hashlib.sha256(
    f"{USERNAME}:{PASSWORD}:portafolio-itse".encode("utf-8")
).hexdigest()

SESSION_COOKIE = "portafolio_sesion"
SESSION_SECONDS = 60 * 60 * 8
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DATA_LOCK = threading.Lock()

SUBJECTS = {
    "ingles": {
        "title": "Inglés",
        "short": "Inglés",
        "description": "Evidencias, prácticas y proyectos de la materia.",
        "accent": "teal",
    },
    "matematicas": {
        "title": "Matemáticas",
        "short": "Mate",
        "description": "Ejercicios, talleres y soluciones destacadas.",
        "accent": "amber",
    },
    "redaccion": {
        "title": "Redacción",
        "short": "Redacción",
        "description": "Ensayos, borradores, lecturas y trabajos escritos.",
        "accent": "coral",
    },
    "digitales": {
        "title": "Digitales",
        "short": "Digitales",
        "description": "Actividades, capturas y avances de tecnología.",
        "accent": "violet",
    },
}


def ensure_storage() -> None:
    ASSET_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    for slug in SUBJECTS:
        (UPLOAD_DIR / slug).mkdir(exist_ok=True)
    if not PHOTO_DB.exists():
        PHOTO_DB.write_text("[]\n", encoding="utf-8")


def load_photos() -> List[Dict[str, str]]:
    ensure_storage()
    with DATA_LOCK:
        try:
            photos = json.loads(PHOTO_DB.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []
    if not isinstance(photos, list):
        return []
    return [photo for photo in photos if isinstance(photo, dict)]


def save_photos(photos: List[Dict[str, str]]) -> None:
    ensure_storage()
    temporary = PHOTO_DB.with_suffix(".tmp")
    with DATA_LOCK:
        temporary.write_text(
            json.dumps(photos, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(PHOTO_DB)


def render_template(path: Path, context: Dict[str, str]) -> str:
    content = path.read_text(encoding="utf-8")
    for key, value in context.items():
        content = content.replace("{{ " + key + " }}", value)
    return content


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_date(iso_value: str) -> str:
    try:
        value = datetime.fromisoformat(iso_value)
    except ValueError:
        return ""
    return value.strftime("%d/%m/%Y %H:%M")


def photo_count_text(count: int) -> str:
    return "1 foto" if count == 1 else f"{count} fotos"


def photo_title(photo: Dict[str, str]) -> str:
    title = (photo.get("caption") or "").strip()
    return title or "Sin título"


def title_group_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    without_marks = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    normalized = without_marks.casefold().strip()
    return re.sub(r"\s+", " ", normalized) or "sin título"


def group_photos_by_title(
    photos: List[Dict[str, str]],
) -> List[Tuple[str, List[Dict[str, str]]]]:
    groups: Dict[str, Tuple[str, List[Dict[str, str]]]] = {}
    for photo in photos:
        title = photo_title(photo)
        key = title_group_key(title)
        if key not in groups:
            groups[key] = (title, [])
        groups[key][1].append(photo)
    return list(groups.values())


def status_flash_html(status: str) -> str:
    messages = {
        "subido": ("ok", "Foto guardada correctamente."),
        "borrado": ("ok", "Foto borrada correctamente."),
        "no-encontrado": ("error", "No pude encontrar esa foto."),
    }
    kind, message = messages.get(status, ("", ""))
    return flash_html(kind, message) if message else ""


def clean_filename(filename: str) -> str:
    original = Path(filename).name
    stem = Path(original).stem
    suffix = Path(original).suffix.lower()
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_stem = normalized.encode("ascii", "ignore").decode("ascii")
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", ascii_stem).strip("-_")
    if not safe_stem:
        safe_stem = "foto"
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ".jpg"
    return f"{safe_stem[:60]}{suffix}"


def photo_url(photo: Dict[str, str]) -> str:
    subject = quote(photo.get("subject", ""))
    filename = quote(photo.get("filename", ""))
    return f"/uploads/{subject}/{filename}"


def safe_redirect_path(value: str, fallback: str = "/") -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    return value


def redirect_path_with_status(path: str, status: str) -> str:
    parsed = urlparse(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["estado"] = [status]
    return urlunparse(
        parsed._replace(query=urlencode(query, doseq=True), fragment="")
    )


def delete_photo(subject: str, filename: str) -> bool:
    if subject not in SUBJECTS or not filename:
        return False

    safe_filename = Path(filename).name
    photos = load_photos()
    remaining = []
    deleted = False
    for photo in photos:
        same_subject = photo.get("subject") == subject
        same_file = photo.get("filename") == safe_filename
        if not deleted and same_subject and same_file:
            deleted = True
            continue
        remaining.append(photo)

    if not deleted:
        return False

    save_photos(remaining)
    candidate = (UPLOAD_DIR / subject / safe_filename).resolve()
    allowed_root = (UPLOAD_DIR / subject).resolve()
    if allowed_root in candidate.parents and candidate.is_file():
        try:
            candidate.unlink()
        except OSError:
            pass
    return True


def photos_for_subject(subject: str) -> List[Dict[str, str]]:
    photos = [photo for photo in load_photos() if photo.get("subject") == subject]
    return sorted(photos, key=lambda item: item.get("uploaded_at", ""), reverse=True)


def make_signature(payload: str) -> str:
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_cookie(username: str) -> str:
    session = {
        "username": username,
        "expires": int(time.time()) + SESSION_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(session, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"{payload}.{make_signature(payload)}"


def read_session(cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    jar = cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except cookies.CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    if not morsel:
        return None
    token = morsel.value
    if "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, make_signature(payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (json.JSONDecodeError, ValueError):
        return None
    if int(data.get("expires", 0)) < int(time.time()):
        return None
    username = str(data.get("username", ""))
    if not hmac.compare_digest(username, USERNAME):
        return None
    return username


def nav_html(active: str = "") -> str:
    subject_links = "\n".join(
        f'<a class="nav-link {"is-active" if active == slug else ""}" '
        f'href="/materias/{slug}">{escape(info["short"])}</a>'
        for slug, info in SUBJECTS.items()
    )
    upload_active = "is-active" if active == "subir" else ""
    home_active = "is-active" if active == "inicio" else ""
    return f"""
    <header class="topbar">
        <a class="brand {home_active}" href="/">
            <img class="brand-mark" src="/assets/logo-it.png" alt="IT">
            <span>Portafolio ITSE</span>
        </a>
        <nav class="nav-actions" aria-label="Materias">
            {subject_links}
            <a class="nav-link {upload_active}" href="/subir">Subir</a>
            <a class="nav-link nav-link-muted" href="/logout">Salir</a>
        </nav>
    </header>
    """


def flash_html(kind: str, message: str) -> str:
    if not message:
        return ""
    return f'<div class="flash flash-{escape(kind)}">{escape(message)}</div>'


def gallery_html(
    subject: Optional[str] = None,
    limit: Optional[int] = None,
    return_to: str = "/",
) -> str:
    photos = load_photos()
    if subject:
        photos = [photo for photo in photos if photo.get("subject") == subject]
    photos = sorted(photos, key=lambda item: item.get("uploaded_at", ""), reverse=True)
    if limit:
        photos = photos[:limit]

    if not photos:
        action = "/subir" if subject is None else f"/subir?materia={quote(subject)}"
        return f"""
        <section class="empty-state">
            <div class="empty-visual"></div>
            <div>
                <h2>No hay fotos todavía</h2>
                <p>Cuando subas una imagen aparecerá en esta galería.</p>
                <a class="button button-primary" href="{action}">Subir foto</a>
            </div>
        </section>
        """

    groups = []
    for title, group_photos in group_photos_by_title(photos):
        cards = []
        for photo in group_photos:
            info = SUBJECTS.get(photo.get("subject", ""), {})
            caption = photo_title(photo)
            subject_name = info.get("title", "Materia")
            cards.append(
                f"""
                <figure class="photo-card">
                    <a href="{photo_url(photo)}" target="_blank" rel="noreferrer">
                        <img src="{photo_url(photo)}" alt="{escape(caption)}" loading="lazy">
                    </a>
                    <figcaption>
                        <div class="photo-card-text">
                            <strong>{escape(caption)}</strong>
                            <span>{escape(subject_name)} · {escape(format_date(photo.get("uploaded_at", "")))}</span>
                        </div>
                        <form class="delete-photo-form" action="/borrar-foto" method="post" onsubmit="return confirm('¿Borrar esta foto?');">
                            <input type="hidden" name="materia" value="{escape(photo.get("subject", ""))}">
                            <input type="hidden" name="filename" value="{escape(photo.get("filename", ""))}">
                            <input type="hidden" name="next" value="{escape(return_to)}">
                            <button class="button button-danger" type="submit">Borrar</button>
                        </form>
                    </figcaption>
                </figure>
                """
            )
        groups.append(
            f"""
            <section class="topic-folder" aria-label="Carpeta {escape(title)}">
                <header class="topic-folder-header">
                    <span class="folder-icon" aria-hidden="true"></span>
                    <div>
                        <h3>{escape(title)}</h3>
                        <p>{photo_count_text(len(group_photos))}</p>
                    </div>
                </header>
                <div class="gallery-grid">{"".join(cards)}</div>
            </section>
            """
        )
    return f'<section class="gallery-groups">{"".join(groups)}</section>'


def subject_cards_html() -> str:
    photos = load_photos()
    counts = {
        slug: sum(1 for photo in photos if photo.get("subject") == slug)
        for slug in SUBJECTS
    }
    cards = []
    for slug, info in SUBJECTS.items():
        count = counts.get(slug, 0)
        cards.append(
            f"""
            <article class="subject-card accent-{escape(info["accent"])}">
                <a href="/materias/{slug}" aria-label="Abrir {escape(info["title"])}">
                    <div class="subject-media">
                        <span>{escape(info["short"])}</span>
                    </div>
                    <div class="subject-card-body">
                        <h2>{escape(info["title"])}</h2>
                        <p>{escape(info["description"])}</p>
                        <span class="photo-count">{photo_count_text(count)}</span>
                    </div>
                </a>
            </article>
            """
        )
    return "\n".join(cards)


def subject_options_html(selected: str = "") -> str:
    return "\n".join(
        f'<option value="{slug}" {"selected" if slug == selected else ""}>'
        f'{escape(info["title"])}</option>'
        for slug, info in SUBJECTS.items()
    )


def parse_form(headers, body: bytes) -> Tuple[Dict[str, str], Dict[str, Dict[str, object]]]:
    content_type = headers.get("Content-Type", "")
    fields: Dict[str, str] = {}
    files: Dict[str, Dict[str, object]] = {}

    if content_type.startswith("multipart/form-data"):
        raw_header = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8")
        message = BytesParser(policy=policy.default).parsebytes(raw_header + body)
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = {
                    "filename": filename,
                    "content": payload,
                    "content_type": part.get_content_type(),
                }
            else:
                charset = part.get_content_charset() or "utf-8"
                fields[name] = payload.decode(charset, errors="replace").strip()
        return fields, files

    parsed = parse_qs(body.decode("utf-8", errors="replace"))
    fields = {key: values[-1].strip() for key, values in parsed.items() if values}
    return fields, files


STATUS_TEXT = {
    200: "OK",
    303: "See Other",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
}


def status_line(status: int) -> str:
    return f"{status} {STATUS_TEXT.get(status, 'OK')}"


def subject_slug_from_path(path: str) -> str:
    slug = path[len("/materias/") :]
    if slug.endswith(".html"):
        slug = slug[:-5]
    return slug


def wsgi_current_user(environ: Dict[str, object]) -> Optional[str]:
    return read_session(str(environ.get("HTTP_COOKIE", "") or ""))


def wsgi_request_headers(environ: Dict[str, object]) -> Dict[str, str]:
    return {
        "Content-Type": str(environ.get("CONTENT_TYPE", "") or ""),
        "Content-Length": str(environ.get("CONTENT_LENGTH", "") or ""),
    }


def wsgi_html(
    html_text: str,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None,
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    data = html_text.encode("utf-8")
    response_headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "no-store"),
    ]
    response_headers.extend((headers or {}).items())
    return status, response_headers, data if send_body else b""


def wsgi_redirect(
    location: str,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    response_headers = [("Location", location), ("Content-Length", "0")]
    response_headers.extend((headers or {}).items())
    return 303, response_headers, b""


def wsgi_not_found(send_body: bool = True) -> Tuple[int, List[Tuple[str, str]], bytes]:
    return wsgi_html(
        """
        <!doctype html>
        <html lang="es">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/Styles.css">
            <title>No encontrado</title>
        </head>
        <body class="plain-page">
            <main class="plain-message">
                <h1>Página no encontrada</h1>
                <a class="button button-primary" href="/">Volver al portafolio</a>
            </main>
        </body>
        </html>
        """,
        status=404,
        send_body=send_body,
    )


def wsgi_file(
    path: Path,
    content_type: str,
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    if not path.is_file():
        return wsgi_not_found(send_body)
    data = path.read_bytes()
    return (
        200,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "public, max-age=3600"),
        ],
        data if send_body else b"",
    )


def wsgi_asset(
    path: str,
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) != 2:
        return wsgi_not_found(send_body)
    _, filename = parts
    candidate = (ASSET_DIR / Path(filename).name).resolve()
    if ASSET_DIR.resolve() not in candidate.parents or not candidate.is_file():
        return wsgi_not_found(send_body)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return wsgi_file(candidate, content_type, send_body=send_body)


def wsgi_upload(
    path: str,
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) != 3:
        return wsgi_not_found(send_body)
    _, subject, filename = parts
    if subject not in SUBJECTS:
        return wsgi_not_found(send_body)
    candidate = (UPLOAD_DIR / subject / Path(filename).name).resolve()
    allowed_root = (UPLOAD_DIR / subject).resolve()
    if allowed_root not in candidate.parents or not candidate.is_file():
        return wsgi_not_found(send_body)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return wsgi_file(candidate, content_type, send_body=send_body)


def wsgi_show_login(
    environ: Dict[str, object],
    error: str = "",
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    if wsgi_current_user(environ):
        return wsgi_redirect("/")
    return wsgi_html(
        render_template(
            TEMPLATE_DIR / "login.html",
            {
                "error": flash_html("error", error) if error else "",
                "username": escape(USERNAME),
            },
        ),
        send_body=send_body,
    )


def wsgi_show_dashboard(
    status: str = "",
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    return wsgi_html(
        render_template(
            TEMPLATE_DIR / "dashboard.html",
            {
                "nav": nav_html("inicio"),
                "flash": status_flash_html(status),
                "subject_cards": subject_cards_html(),
                "recent_gallery": gallery_html(limit=6, return_to="/"),
            },
        ),
        send_body=send_body,
    )


def wsgi_show_upload(
    query: Dict[str, List[str]],
    message: str = "",
    kind: str = "ok",
    send_body: bool = True,
    status: int = 200,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    selected = query.get("materia", [""])[0]
    if selected not in SUBJECTS:
        selected = ""
    return wsgi_html(
        render_template(
            TEMPLATE_DIR / "subir.html",
            {
                "nav": nav_html("subir"),
                "options": subject_options_html(selected),
                "flash": flash_html(kind, message),
            },
        ),
        status=status,
        send_body=send_body,
    )


def wsgi_show_subject(
    slug: str,
    status: str = "",
    send_body: bool = True,
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    info = SUBJECTS[slug]
    count = len(photos_for_subject(slug))
    return wsgi_html(
        render_template(
            SUBJECT_TEMPLATE_DIR / f"{slug}.html",
            {
                "nav": nav_html(slug),
                "count": str(count),
                "count_word": "foto" if count == 1 else "fotos",
                "flash": status_flash_html(status),
                "gallery": gallery_html(slug, return_to=f"/materias/{slug}"),
                "upload_link": f"/subir?materia={slug}",
                "subject_title": escape(info["title"]),
            },
        ),
        send_body=send_body,
    )


def wsgi_read_body(
    environ: Dict[str, object],
) -> Tuple[
    Optional[bytes],
    Optional[Tuple[int, List[Tuple[str, str]], bytes]],
]:
    try:
        length = int(str(environ.get("CONTENT_LENGTH", "") or "0"))
    except ValueError:
        length = 0
    if length > MAX_UPLOAD_BYTES + 256_000:
        return None, wsgi_show_upload(
            {},
            "La imagen supera el límite de 8 MB.",
            "error",
            status=413,
        )
    stream = environ.get("wsgi.input")
    if not hasattr(stream, "read"):
        return b"", None
    return stream.read(length), None


def wsgi_handle_login(
    environ: Dict[str, object],
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    body, response = wsgi_read_body(environ)
    if response is not None:
        return response
    fields, _ = parse_form(wsgi_request_headers(environ), body or b"")
    username = fields.get("usuario", "")
    password = fields.get("password", "")
    valid_user = hmac.compare_digest(username, USERNAME)
    valid_password = hmac.compare_digest(password, PASSWORD)
    if not (valid_user and valid_password):
        return wsgi_show_login(environ, "Usuario o contraseña incorrectos.")

    cookie_value = make_session_cookie(username)
    cookie = (
        f"{SESSION_COOKIE}={cookie_value}; HttpOnly; SameSite=Lax; "
        f"Path=/; Max-Age={SESSION_SECONDS}"
    )
    return wsgi_redirect("/", headers={"Set-Cookie": cookie})


def wsgi_handle_upload(
    environ: Dict[str, object],
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    body, response = wsgi_read_body(environ)
    if response is not None:
        return response
    fields, files = parse_form(wsgi_request_headers(environ), body or b"")
    subject = fields.get("materia", "")
    caption = fields.get("titulo", "")
    file_data = files.get("foto")

    if subject not in SUBJECTS:
        return wsgi_show_upload({}, "Selecciona una materia válida.", "error")
    if not file_data or not file_data.get("content"):
        return wsgi_show_upload(
            {"materia": [subject]}, "Selecciona una imagen.", "error"
        )

    original = str(file_data.get("filename", "foto"))
    extension = Path(original).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return wsgi_show_upload(
            {"materia": [subject]},
            "Formato no permitido. Usa JPG, PNG, GIF o WEBP.",
            "error",
        )

    content = bytes(file_data.get("content", b""))
    if len(content) > MAX_UPLOAD_BYTES:
        return wsgi_show_upload(
            {"materia": [subject]}, "La imagen supera el límite de 8 MB.", "error"
        )

    safe_name = clean_filename(original)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_name = f"{timestamp}-{secrets.token_hex(3)}-{safe_name}"
    destination = UPLOAD_DIR / subject / final_name
    destination.write_bytes(content)

    photos = load_photos()
    photos.append(
        {
            "subject": subject,
            "filename": final_name,
            "original_name": original,
            "caption": caption[:120],
            "uploaded_at": datetime.now().isoformat(timespec="minutes"),
        }
    )
    save_photos(photos)
    return wsgi_redirect(f"/materias/{subject}?estado=subido")


def wsgi_handle_delete_photo(
    environ: Dict[str, object],
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    body, response = wsgi_read_body(environ)
    if response is not None:
        return response
    fields, _ = parse_form(wsgi_request_headers(environ), body or b"")
    subject = fields.get("materia", "")
    filename = fields.get("filename", "")
    fallback = f"/materias/{subject}" if subject in SUBJECTS else "/"
    return_to = safe_redirect_path(fields.get("next", ""), fallback)
    status = "borrado" if delete_photo(subject, filename) else "no-encontrado"
    return wsgi_redirect(redirect_path_with_status(return_to, status))


def wsgi_logout() -> Tuple[int, List[Tuple[str, str]], bytes]:
    return wsgi_redirect(
        "/login",
        headers={
            "Set-Cookie": (
                f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; "
                "Path=/; Max-Age=0"
            )
        },
    )


def wsgi_method_not_allowed() -> Tuple[int, List[Tuple[str, str]], bytes]:
    data = b"Metodo no permitido"
    return (
        405,
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(data))),
            ("Allow", "GET, HEAD, POST"),
        ],
        data,
    )


def handle_wsgi_request(
    environ: Dict[str, object],
) -> Tuple[int, List[Tuple[str, str]], bytes]:
    ensure_storage()
    method = str(environ.get("REQUEST_METHOD", "GET") or "GET").upper()
    raw_path = str(environ.get("PATH_INFO", "") or "/")
    path = (raw_path if raw_path.startswith("/") else f"/{raw_path}").rstrip("/") or "/"
    query = parse_qs(str(environ.get("QUERY_STRING", "") or ""))
    send_body = method != "HEAD"

    if method not in {"GET", "HEAD", "POST"}:
        return wsgi_method_not_allowed()

    if method in {"GET", "HEAD"}:
        if path in {"/Styles.css", "/styles.css"}:
            return wsgi_file(BASE_DIR / "Styles.css", "text/css; charset=utf-8", send_body)
        if path == "/favicon.ico":
            return wsgi_file(ASSET_DIR / "logo-it.png", "image/png", send_body)
        if path.startswith("/assets/"):
            return wsgi_asset(path, send_body)
        if path.startswith("/uploads/"):
            return wsgi_upload(path, send_body)
        if path == "/login":
            return wsgi_show_login(environ, send_body=send_body)
        if method == "GET" and path == "/logout":
            return wsgi_logout()

        if not wsgi_current_user(environ):
            return wsgi_redirect("/login")

        if path == "/":
            return wsgi_show_dashboard(query.get("estado", [""])[0], send_body)
        if path == "/subir":
            return wsgi_show_upload(query, send_body=send_body)
        if path.startswith("/materias/"):
            slug = subject_slug_from_path(path)
            if slug in SUBJECTS:
                return wsgi_show_subject(slug, query.get("estado", [""])[0], send_body)
        return wsgi_not_found(send_body)

    if path == "/login":
        return wsgi_handle_login(environ)
    if path == "/subir":
        if not wsgi_current_user(environ):
            return wsgi_redirect("/login")
        return wsgi_handle_upload(environ)
    if path == "/borrar-foto":
        if not wsgi_current_user(environ):
            return wsgi_redirect("/login")
        return wsgi_handle_delete_photo(environ)
    return wsgi_not_found()


def application(environ, start_response):
    status, headers, body = handle_wsgi_request(environ)
    start_response(status_line(status), headers)
    return [body]


class PortfolioHandler(BaseHTTPRequestHandler):
    server_version = "PortafolioITSE/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    @property
    def current_user(self) -> Optional[str]:
        return read_session(self.headers.get("Cookie"))

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in {"/Styles.css", "/styles.css"}:
            return self.serve_file(
                BASE_DIR / "Styles.css",
                "text/css; charset=utf-8",
                send_body=False,
            )
        if path == "/favicon.ico":
            return self.serve_file(ASSET_DIR / "logo-it.png", "image/png", send_body=False)
        if path.startswith("/assets/"):
            return self.serve_asset(path, send_body=False)
        if path.startswith("/uploads/"):
            return self.serve_upload(path, send_body=False)
        if path == "/login":
            return self.send_html(
                render_template(
                    TEMPLATE_DIR / "login.html",
                    {
                        "error": "",
                        "username": escape(USERNAME),
                    },
                ),
                send_body=False,
            )
        if not self.current_user:
            return self.redirect("/login")
        return self.send_html("", send_body=False)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in {"/Styles.css", "/styles.css"}:
            return self.serve_file(BASE_DIR / "Styles.css", "text/css; charset=utf-8")
        if path == "/favicon.ico":
            return self.serve_file(ASSET_DIR / "logo-it.png", "image/png")
        if path.startswith("/assets/"):
            return self.serve_asset(path)
        if path.startswith("/uploads/"):
            return self.serve_upload(path)
        if path == "/login":
            return self.show_login()
        if path == "/logout":
            return self.logout()

        if not self.current_user:
            return self.redirect("/login")

        if path == "/":
            return self.show_dashboard(query.get("estado", [""])[0])
        if path == "/subir":
            return self.show_upload(query)
        if path.startswith("/materias/"):
            slug = subject_slug_from_path(path)
            if slug in SUBJECTS:
                status = query.get("estado", [""])[0]
                return self.show_subject(slug, status)
        return self.not_found()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            return self.handle_login()
        if parsed.path == "/subir":
            if not self.current_user:
                return self.redirect("/login")
            return self.handle_upload()
        if parsed.path == "/borrar-foto":
            if not self.current_user:
                return self.redirect("/login")
            return self.handle_delete_photo()
        return self.not_found()

    def read_request_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_UPLOAD_BYTES + 256_000:
            self.send_html(
                render_template(
                    TEMPLATE_DIR / "subir.html",
                    {
                        "nav": nav_html("subir"),
                        "options": subject_options_html(),
                        "flash": flash_html(
                            "error", "La imagen supera el límite de 8 MB."
                        ),
                    },
                ),
                status=413,
            )
            return None
        return self.rfile.read(length)

    def show_login(self, error: str = "") -> None:
        if self.current_user:
            return self.redirect("/")
        self.send_html(
            render_template(
                TEMPLATE_DIR / "login.html",
                {
                    "error": flash_html("error", error) if error else "",
                    "username": escape(USERNAME),
                },
            )
        )

    def handle_login(self) -> None:
        body = self.read_request_body()
        if body is None:
            return
        fields, _ = parse_form(self.headers, body)
        username = fields.get("usuario", "")
        password = fields.get("password", "")
        valid_user = hmac.compare_digest(username, USERNAME)
        valid_password = hmac.compare_digest(password, PASSWORD)
        if not (valid_user and valid_password):
            return self.show_login("Usuario o contraseña incorrectos.")

        cookie_value = make_session_cookie(username)
        cookie = (
            f"{SESSION_COOKIE}={cookie_value}; HttpOnly; SameSite=Lax; "
            f"Path=/; Max-Age={SESSION_SECONDS}"
        )
        self.redirect("/", headers={"Set-Cookie": cookie})

    def logout(self) -> None:
        self.redirect(
            "/login",
            headers={
                "Set-Cookie": (
                    f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; "
                    "Path=/; Max-Age=0"
                )
            },
        )

    def show_dashboard(self, status: str = "") -> None:
        self.send_html(
            render_template(
                TEMPLATE_DIR / "dashboard.html",
                {
                    "nav": nav_html("inicio"),
                    "flash": status_flash_html(status),
                    "subject_cards": subject_cards_html(),
                    "recent_gallery": gallery_html(limit=6, return_to="/"),
                },
            )
        )

    def show_upload(self, query: Dict[str, List[str]], message: str = "", kind: str = "ok") -> None:
        selected = query.get("materia", [""])[0]
        if selected not in SUBJECTS:
            selected = ""
        self.send_html(
            render_template(
                TEMPLATE_DIR / "subir.html",
                {
                    "nav": nav_html("subir"),
                    "options": subject_options_html(selected),
                    "flash": flash_html(kind, message),
                },
            )
        )

    def handle_upload(self) -> None:
        body = self.read_request_body()
        if body is None:
            return
        fields, files = parse_form(self.headers, body)
        subject = fields.get("materia", "")
        caption = fields.get("titulo", "")
        file_data = files.get("foto")

        if subject not in SUBJECTS:
            return self.show_upload({}, "Selecciona una materia válida.", "error")
        if not file_data or not file_data.get("content"):
            return self.show_upload(
                {"materia": [subject]}, "Selecciona una imagen.", "error"
            )

        original = str(file_data.get("filename", "foto"))
        extension = Path(original).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            return self.show_upload(
                {"materia": [subject]},
                "Formato no permitido. Usa JPG, PNG, GIF o WEBP.",
                "error",
            )

        content = bytes(file_data.get("content", b""))
        if len(content) > MAX_UPLOAD_BYTES:
            return self.show_upload(
                {"materia": [subject]}, "La imagen supera el límite de 8 MB.", "error"
            )

        safe_name = clean_filename(original)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        final_name = f"{timestamp}-{secrets.token_hex(3)}-{safe_name}"
        destination = UPLOAD_DIR / subject / final_name
        destination.write_bytes(content)

        photos = load_photos()
        photos.append(
            {
                "subject": subject,
                "filename": final_name,
                "original_name": original,
                "caption": caption[:120],
                "uploaded_at": datetime.now().isoformat(timespec="minutes"),
            }
        )
        save_photos(photos)
        self.redirect(f"/materias/{subject}?estado=subido")

    def handle_delete_photo(self) -> None:
        body = self.read_request_body()
        if body is None:
            return
        fields, _ = parse_form(self.headers, body)
        subject = fields.get("materia", "")
        filename = fields.get("filename", "")
        fallback = f"/materias/{subject}" if subject in SUBJECTS else "/"
        return_to = safe_redirect_path(fields.get("next", ""), fallback)
        status = "borrado" if delete_photo(subject, filename) else "no-encontrado"
        self.redirect(redirect_path_with_status(return_to, status))

    def show_subject(self, slug: str, status: str = "") -> None:
        info = SUBJECTS[slug]
        count = len(photos_for_subject(slug))
        self.send_html(
            render_template(
                SUBJECT_TEMPLATE_DIR / f"{slug}.html",
                {
                    "nav": nav_html(slug),
                    "count": str(count),
                    "count_word": "foto" if count == 1 else "fotos",
                    "flash": status_flash_html(status),
                    "gallery": gallery_html(slug, return_to=f"/materias/{slug}"),
                    "upload_link": f"/subir?materia={slug}",
                    "subject_title": escape(info["title"]),
                },
            )
        )

    def serve_upload(self, path: str, send_body: bool = True) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3:
            return self.not_found()
        _, subject, filename = parts
        if subject not in SUBJECTS:
            return self.not_found()
        candidate = (UPLOAD_DIR / subject / Path(filename).name).resolve()
        allowed_root = (UPLOAD_DIR / subject).resolve()
        if allowed_root not in candidate.parents or not candidate.is_file():
            return self.not_found()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.serve_file(candidate, content_type, send_body=send_body)

    def serve_asset(self, path: str, send_body: bool = True) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 2:
            return self.not_found()
        _, filename = parts
        candidate = (ASSET_DIR / Path(filename).name).resolve()
        if ASSET_DIR.resolve() not in candidate.parents or not candidate.is_file():
            return self.not_found()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.serve_file(candidate, content_type, send_body=send_body)

    def serve_file(self, path: Path, content_type: str, send_body: bool = True) -> None:
        if not path.is_file():
            return self.not_found()
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def send_html(
        self,
        html_text: str,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        send_body: bool = True,
    ) -> None:
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def redirect(self, location: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def not_found(self) -> None:
        self.send_html(
            """
            <!doctype html>
            <html lang="es">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <link rel="stylesheet" href="/Styles.css">
                <title>No encontrado</title>
            </head>
            <body class="plain-page">
                <main class="plain-message">
                    <h1>Página no encontrada</h1>
                    <a class="button button-primary" href="/">Volver al portafolio</a>
                </main>
            </body>
            </html>
            """,
            status=404,
        )


class PortfolioServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server() -> Tuple[PortfolioServer, int]:
    ports = [PORT, *range(PORT + 1, PORT + 21)]
    last_error: Optional[OSError] = None

    for port in ports:
        try:
            return PortfolioServer((HOST, port), PortfolioHandler), port
        except OSError as error:
            last_error = error
            if error.errno != errno.EADDRINUSE:
                raise

    raise RuntimeError(
        f"No pude abrir un puerto entre {PORT} y {PORT + 20}. "
        "Cierra otra ejecución del portafolio e inténtalo de nuevo."
    ) from last_error


def run() -> None:
    ensure_storage()
    server, active_port = create_server()
    print(f"Portafolio ITSE listo en http://{HOST}:{active_port}")
    print(f"Usuario: {USERNAME} | Contraseña: {PASSWORD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
