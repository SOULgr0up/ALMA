"""ALMA — backend de upload para o Google Drive via OAuth 2.0."""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

REDIRECT_URI = "https://alma-25e9.onrender.com/oauth2callback"

ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alma")

if os.environ.get("ALLOWED_ORIGINS"):
    CORS(
        app,
        origins=[
            o.strip()
            for o in os.environ["ALLOWED_ORIGINS"].split(",")
            if o.strip()
        ],
    )

_drive = None


def oauth_config():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID ou GOOGLE_CLIENT_SECRET não configurado"
        )

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def drive():
    global _drive

    if _drive is not None:
        return _drive

    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not refresh_token:
        raise RuntimeError("GOOGLE_REFRESH_TOKEN não configurado")

    config = oauth_config()["web"]

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=config["token_uri"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=SCOPES,
    )

    _drive = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    return _drive


@app.get("/")
def home():
    return send_from_directory(ROOT_DIR, "index.html")


@app.get("/health")
def health():
    configured = bool(
        os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("GOOGLE_REFRESH_TOKEN")
        and os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    )

    return jsonify(
        status="ok",
        configured=configured,
    )


@app.get("/oauth2")
def oauth2():
    flow = Flow.from_client_config(
        oauth_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    return f'<a href="{authorization_url}">Autorizar Google Drive</a>'


@app.get("/oauth2callback")
def oauth2callback():
    flow = Flow.from_client_config(
        oauth_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    flow.fetch_token(
        authorization_response=request.url
    )

    credentials = flow.credentials

    if not credentials.refresh_token:
        return jsonify(
            ok=False,
            error="Google não forneceu um refresh token."
        ), 400

    return jsonify(
        ok=True,
        refresh_token=credentials.refresh_token,
    )


@app.post("/upload")
def upload():
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

    if not folder_id:
        return jsonify(
            ok=False,
            error="GOOGLE_DRIVE_FOLDER_ID não configurado."
        ), 500

    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        return jsonify(
            ok=False,
            error="GOOGLE_REFRESH_TOKEN não configurado."
        ), 500

    f = request.files.get("file")

    if not f:
        return jsonify(
            ok=False,
            error="Nenhum arquivo enviado."
        ), 400

    mime = (f.mimetype or "").split(";")[0].strip().lower()

    if mime not in ALLOWED:
        return jsonify(
            ok=False,
            error="Tipo de arquivo não permitido."
        ), 415

    tipo = "foto" if mime.startswith("image/") else "video"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    name = (
        f"{stamp}_{tipo}_{uuid.uuid4().hex[:8]}"
        f"{ALLOWED[mime]}"
    )

    props = {
        "tipo": tipo,
        "dono": (request.form.get("dono") or "")[:100],
        "para": (request.form.get("para") or "")[:100],
    }

    try:
        media = MediaIoBaseUpload(
            f.stream,
            mimetype=mime,
            chunksize=5 * 1024 * 1024,
            resumable=True,
        )

        created = (
            drive()
            .files()
            .create(
                body={
                    "name": name,
                    "parents": [folder_id],
                    "appProperties": props,
                },
                media_body=media,
                fields="id,name",
            )
            .execute()
        )

    except HttpError as e:
        log.error("Erro do Google Drive: %s", e)

        return jsonify(
            ok=False,
            error="Falha ao salvar no Google Drive."
        ), 502

    except Exception:
        log.exception("Erro inesperado no upload")

        return jsonify(
            ok=False,
            error="Erro interno no servidor."
        ), 500

    return jsonify(
        ok=True,
        id=created["id"],
        nome=created["name"],
        tipo=tipo,
    ), 201


@app.errorhandler(413)
def too_large(_e):
    return jsonify(
        ok=False,
        error=f"Arquivo maior que {MAX_MB} MB."
    ), 413


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
    )