from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .core import (
    MAX_BYTES,
    Stop,
    canonical_id,
    compact,
    require,
    sha256,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiError(Stop):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, cfg, mutating=False):
        url = cfg["immich_url"].rstrip("/")
        u = urllib.parse.urlsplit(url)
        require(
            u.scheme in ("http", "https")
            and u.netloc
            and not (u.username or u.password or u.query or u.fragment),
            "immich_url non valido",
        )
        self.base = url if u.path.endswith("/api") else url + "/api"
        self.key = cfg["api_key"]
        require("\n" not in self.key and "\r" not in self.key, "API key non valida")
        self.timeout = cfg["http_timeout"]
        self.mutating = mutating
        self.opener = urllib.request.build_opener(NoRedirect())
        self.permissions = set()
        self.allowed_trash_id = None

    def raw(self, method, path, body=None, headers=None, max_bytes=MAX_BYTES):
        require(path.startswith("/") and "://" not in path, "API path non relativo")
        require(
            method == "GET" or self.mutating,
            "API in scrittura vietata in audit/verifica",
        )
        if method == "DELETE":
            try:
                payload = json.loads(body)
            except Exception as e:
                raise Stop("DELETE senza payload validato") from e
            require(
                path == "/assets"
                and self.allowed_trash_id is not None
                and isinstance(payload, dict)
                and payload.get("force") is False
                and payload == {"ids": [self.allowed_trash_id], "force": False},
                "Permesso solo DELETE force:false della sorgente appena verificata",
            )
        h = {
            "x-api-key": self.key,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
        h.update(headers or {})
        req = urllib.request.Request(
            self.base + path, data=body, headers=h, method=method
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                result = r.read(max_bytes + 1)
                require(len(result) <= max_bytes, "Risposta API troppo grande")
                return r.status, dict(r.headers), result
        except urllib.error.HTTPError as e:
            detail = (
                e.read(1500).decode(errors="replace").replace(self.key, "[REDACTED]")
            )
            raise ApiError(
                f"API {method} {path}: HTTP {e.code}: {detail}", e.code
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(
                f"API {method} {path}: rete/timeout; esito della scrittura non deducibile: "
                + str(e).replace(self.key, "[REDACTED]")
            ) from e

    def request(self, method, path, data=None):
        _, _, b = self.raw(
            method,
            path,
            None if data is None else compact(data).encode(),
            {"Content-Type": "application/json"},
            max_bytes=32 * 1024 * 1024,
        )
        try:
            return json.loads(b) if b.strip() else None
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError("Risposta non JSON: " + path) from e

    def need(self, permissions):
        missing = set(permissions) - self.permissions
        require(
            "all" in self.permissions or not missing,
            "Permessi API mancanti: " + ", ".join(sorted(missing)),
        )

    def preflight(self):
        version = self.request("GET", "/server/version")
        me = self.request("GET", "/users/me")
        self.owner = canonical_id(me["id"])
        self.permissions = set(
            self.request("GET", "/api-keys/me").get("permissions", [])
        )
        self.need(
            {"asset.read", "user.read", "asset.view", "asset.download"}
            | (
                {
                    "asset.upload",
                    "asset.copy",
                    "asset.update",
                    "asset.delete",
                    "job.create",
                }
                if self.mutating
                else set()
            )
        )
        features = self.request("GET", "/server/features")
        settings = self.request("GET", "/server/config")
        require(
            features.get("trash") is True
            and type(settings.get("trashDays")) is int
            and settings["trashDays"] > 0,
            "Cestino disabilitato/durata zero: originale NON verrà cancellato",
        )
        return dict(
            owner=self.owner,
            version=version,
            trash_days=settings["trashDays"],
            trash_enabled=True,
        )

    def asset(self, i):
        r = self.request("GET", "/assets/" + canonical_id(i))
        require(r.get("id") == i, "Risposta asset inattesa")
        return r

    def upload(self, fields, filename, image, sidecar):
        require(
            not any(c in filename for c in '\r\n"\\/') and filename.endswith(".jpg"),
            "Nome upload non valido",
        )
        boundary = "immich-native-" + uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            # 3.2.4 applies body.filename to BOTH media and sidecar in its file filter.
            # Omit this optional global field; use each multipart part's own filename.
            if name == "filename":
                continue
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + str(value).encode()
                + b"\r\n"
            )
        for name, fn, ctype, data in [
            ("assetData", filename, "image/jpeg", image),
            ("sidecarData", filename + ".xmp", "application/rdf+xml", sidecar),
        ]:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                + data
                + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        _, _, raw = self.raw(
            "POST",
            "/assets",
            body,
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
            32 * 1024 * 1024,
        )
        try:
            r = json.loads(raw)
        except Exception as e:
            raise ApiError("Upload: risposta non JSON, riprendere dal journal") from e
        require(
            r.get("status") in ("created", "duplicate"), "Upload con stato inatteso"
        )
        canonical_id(r["id"])
        return r

    def media(self, i, kind):
        path = (
            "/assets/"
            + canonical_id(i)
            + (
                "/original"
                if kind == "original"
                else "/thumbnail?"
                + urllib.parse.urlencode({"size": kind, "edited": "false"})
            )
        )
        status, h, b = self.raw("GET", path, max_bytes=MAX_BYTES)
        require(status == 200, "Media HTTP non 200")
        ctype = next(
            (v for k, v in h.items() if k.lower() == "content-type"), ""
        ).split(";")[0]
        require(ctype.startswith("image/") and len(b) > 32, "Risposta non immagine")
        image_sig = (
            b[:3] == b"\xff\xd8\xff"
            or b.startswith(b"\x89PNG\r\n\x1a\n")
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")
            or b[4:8] == b"ftyp"
        )
        require(image_sig, "Firma immagine non valida")
        return b, dict(
            status=status, content_type=ctype, bytes=len(b), sha256=sha256(b)
        )

    def trash_source(self, i):
        # The engine arms this only after rechecking the full target and its thumbnails.
        require(self.allowed_trash_id == i, "Sorgente non autorizzata al cestino")
        return self.request("DELETE", "/assets", {"ids": [i], "force": False})
