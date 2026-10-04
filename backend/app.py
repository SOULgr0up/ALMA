"""ALMA — backend de upload para o Google Drive.

Variáveis de ambiente (Render):
  GOOGLE_SERVICE_ACCOUNT_JSON  conteúdo COMPLETO do JSON da Service Account (texto)
  GOOGLE_DRIVE_FOLDER_ID       ID da pasta de destino (compartilhada com o e-mail da Service Account)
Opcionais:
  ALLOWED_ORIGINS              origens permitidas via CORS, separadas por vírgula
                               (só necessário se o front-end estiver em outro domínio)
  MAX_UPLOAD_MB                limite por arquivo (padrão 100)
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
SCOPES = ["https://www.googleapis.com/auth/drive"]

# tipo MIME -> extensão (lista fechada: só fotos e vídeos)
ALLOWED = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif",
    "video/webm": ".webm", "video/mp4": ".mp4", "video/quicktime": ".mov",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alma")

if os.environ.get("ALLOWED_ORIGINS"):
    CORS(app, origins=[o.strip() for o in os.environ["ALLOWED_ORIGINS"].split(",") if o.strip()])

_drive = None


def drive():
    """Cria o cliente do Drive uma única vez, a partir das variáveis do Render."""
    global _drive
    if _drive is None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON não configurada")
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive


@app.get("/")
def home():
    # serve apenas o index.html (o código do backend nunca é exposto)
    return send_from_directory(ROOT_DIR, "index.html")


@app.get("/health")
def health():
    ok = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("GOOGLE_DRIVE_FOLDER_ID"))
    return jsonify(status="ok", configured=ok)


@app.post("/upload")
def upload():
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id or not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return jsonify(ok=False, error="Servidor não configurado."), 500

    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="Nenhum arquivo enviado."), 400

    mime = (f.mimetype or "").split(";")[0].strip().lower()
    if mime not in ALLOWED:
        return jsonify(ok=False, error="Tipo de arquivo não permitido."), 415

    tipo = "foto" if mime.startswith("image/") else "video"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{tipo}_{uuid.uuid4().hex[:8]}{ALLOWED[mime]}"
    props = {  # metadados mínimos
        "tipo": tipo,
        "dono": (request.form.get("dono") or "")[:100],
        "para": (request.form.get("para") or "")[:100],
    }

    try:
        media = MediaIoBaseUpload(f.stream, mimetype=mime, chunksize=5 * 1024 * 1024, resumable=True)
        created = drive().files().create(
            body={"name": name, "parents": [folder_id], "appProperties": props},
            media_body=media,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        log.error("Erro do Google Drive: %s", e)
        return jsonify(ok=False, error="Falha ao salvar no Google Drive."), 502
    except Exception:
        log.exception("Erro inesperado no upload")
        return jsonify(ok=False, error="Erro interno no servidor."), 500

    return jsonify(ok=True, id=created["id"], nome=created["name"], tipo=tipo), 201


@app.errorhandler(413)
def too_large(_e):
    return jsonify(ok=False, error=f"Arquivo maior que {MAX_MB} MB."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
