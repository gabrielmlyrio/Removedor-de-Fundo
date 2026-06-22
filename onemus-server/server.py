"""One For All — app web + API OneMus (yt-dlp)."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import yt_dlp

ROOT_DIR = Path(__file__).resolve().parent.parent
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".otf": "font/otf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
}


def friendly_error(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if "copyright" in lower:
        return "Vídeo removido do YouTube por direitos autorais."
    if "video unavailable" in lower or "is unavailable" in lower:
        return "Vídeo indisponível (removido, privado ou bloqueado na sua região)."
    if "private video" in lower:
        return "Vídeo privado — sem permissão para acessar."
    if "sign in" in lower or "age" in lower:
        return "Vídeo restrito — exige login ou confirmação de idade."
    if text.upper().startswith("ERROR:"):
        return text.split(":", 1)[1].strip()
    return text or "Erro desconhecido."


def sanitize_filename(name: str, fallback: str = "musica") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or fallback))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:120]
    return cleaned or fallback


def safe_root_path(rel_path: str) -> Path | None:
    rel = rel_path.lstrip("/").replace("\\", "/")
    if not rel or rel.endswith("/"):
        return None
    candidate = (ROOT_DIR / rel).resolve()
    try:
        candidate.relative_to(ROOT_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def extract_tracks(info: dict[str, Any] | None, source_url: str) -> list[dict[str, str]]:
    if not info:
        return []
    tracks: list[dict[str, str]] = []
    entries = info.get("entries")
    if entries is not None:
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id")
            if not video_id:
                continue
            tracks.append(
                {
                    "videoId": video_id,
                    "title": entry.get("title") or f"Vídeo {video_id}",
                    "url": entry.get("webpage_url")
                    or entry.get("url")
                    or f"https://www.youtube.com/watch?v={video_id}",
                }
            )
        return tracks
    video_id = info.get("id")
    if not video_id:
        return []
    return [
        {
            "videoId": video_id,
            "title": info.get("title") or f"Vídeo {video_id}",
            "url": info.get("webpage_url") or source_url,
        }
    ]


def resolve_url(url: str) -> list[dict[str, str]]:
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": "only_download",
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = extract_tracks(info, url)
    if not tracks:
        raise RuntimeError("Nenhuma faixa encontrada neste link.")
    return tracks


def extract_audio_info(video_id: str) -> dict[str, Any]:
    if not VIDEO_ID_RE.match(video_id):
        raise ValueError("ID de vídeo inválido.")
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Vídeo indisponível.")
    stream_url = info.get("url")
    if not stream_url:
        raise RuntimeError("URL de áudio indisponível.")
    return {
        "stream_url": stream_url,
        "title": info.get("title") or video_id,
        "ext": info.get("ext") or "webm",
        "filesize": int(info.get("filesize") or info.get("filesize_approx") or 0),
    }


class OneForAllHandler(BaseHTTPRequestHandler):
    server_version = "OneForAll/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Length")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path) -> None:
        suffix = file_path.suffix.lower()
        content_type = MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"

        if path in ("/health", "/health/"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "ffmpeg": bool(shutil.which("ffmpeg")),
                    "engine": "yt-dlp",
                    "online": True,
                },
            )
            return

        audio_prefix = "/api/audio/"
        if path.startswith(audio_prefix):
            video_id = urllib.parse.unquote(path[len(audio_prefix) :]).split("/")[0]
            try:
                meta = extract_audio_info(video_id)
            except Exception as exc:
                self._send_json(500, {"error": friendly_error(str(exc))})
                return

            filename = f"{sanitize_filename(meta['title'], video_id)}.{meta['ext']}"
            req = urllib.request.Request(
                meta["stream_url"],
                headers={"User-Agent": "Mozilla/5.0 (OneForAll/1.0)"},
            )
            try:
                upstream = urllib.request.urlopen(req, timeout=300)
            except urllib.error.HTTPError as exc:
                self._send_json(exc.code, {"error": f"Falha ao baixar áudio ({exc.code})."})
                return
            except Exception as exc:
                self._send_json(502, {"error": friendly_error(str(exc))})
                return

            try:
                content_length = int(upstream.headers.get("Content-Length") or meta["filesize"] or 0)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if content_length > 0:
                    self.send_header("Content-Length", str(content_length))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self._send_cors()
                self.end_headers()

                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            finally:
                upstream.close()
            return

        static_path = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        file_path = safe_root_path(static_path)
        if file_path:
            self._send_file(file_path)
            return

        self._send_json(404, {"error": "Não encontrado."})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path != "/api/resolve":
            self._send_json(404, {"error": "Rota não encontrada."})
            return

        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "JSON inválido."})
            return

        url = str(body.get("url") or "").strip()
        if not url:
            self._send_json(400, {"error": "URL vazia."})
            return

        try:
            tracks = resolve_url(url)
        except Exception as exc:
            self._send_json(500, {"error": friendly_error(str(exc))})
            return

        self._send_json(200, {"tracks": tracks, "source": "online"})


def run_server() -> None:
    httpd = HTTPServer((HOST, PORT), OneForAllHandler)
    print(f"One For All online em http://{HOST}:{PORT}")
    print("OneMus integrado — abra no navegador e use normalmente.")
    print("Ctrl+C para encerrar.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
