"""ALMA — backend de upload para o Google Drive via OAuth 2.0.

Variáveis de ambiente (Render):
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   credenciais OAuth (tipo "Aplicativo da Web")
  GOOGLE_REFRESH_TOKEN                      gerado uma vez em /oauth2 (veja abaixo)
  GOOGLE_DRIVE_FOLDER_ID                    ID da pasta de destino
Opcionais:
  OAUTH_REDIRECT_URI   padrão: https://alma-25e9.onrender.com/oauth2callback
  SETUP_TOKEN          se definida, /oauth2 exige ?key=<SETUP_TOKEN>. Sem ela, as rotas ficam ABERTAS.
  ALLOWED_ORIGINS      origens CORS, separadas por vírgula
  MAX_UPLOAD_MB        limite por arquivo (padrão 100)

Para gerar o refresh token: abra /oauth2, autorize e copie o token para
GOOGLE_REFRESH_TOKEN. Depois, defina SETUP_TOKEN para trancar as rotas.
"""
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone

# O Google pode devolver os escopos em ordem/forma diferente; não tratar isso como erro.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from itsdangerous import BadSignature, URLSafeTimedSerializer

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

# "drive.file" só enxerga arquivos criados pelo próprio app: uma pasta criada
# manualmente no Drive daria 404 no upload. Por isso o escopo completo "drive".
SCOPES = ["https://www.googleapis.com/auth/drive"]

REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "https://alma-25e9.onrender.com/oauth2callback"
)

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

_creds = None


def oauth_config():
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("GOOGLE_CLIENT_ID ou GOOGLE_CLIENT_SECRET não configurado")
    return {"web": {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }}


def make_flow():
    # Sem PKCE: o callback cria um Flow novo e não teria o code_verifier gerado em /oauth2.
    return Flow.from_client_config(
        oauth_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def credentials():
    global _creds
    if _creds is None:
        refresh = os.environ.get("GOOGLE_REFRESH_TOKEN")
        if not refresh:
            raise RuntimeError("GOOGLE_REFRESH_TOKEN não configurado")
        cfg = oauth_config()["web"]
        _creds = Credentials(
            token=None, refresh_token=refresh, token_uri=cfg["token_uri"],
            client_id=cfg["client_id"], client_secret=cfg["client_secret"], scopes=SCOPES,
        )
    return _creds


def drive():
    # Cliente novo por requisição: o httplib2 do googleapiclient não é thread-safe.
    return build("drive", "v3", credentials=credentials(), cache_discovery=False)


def _setup_token():
    return os.environ.get("SETUP_TOKEN") or None


def _state_serializer():
    secret = _setup_token() or os.environ.get("GOOGLE_CLIENT_SECRET") or ""
    return URLSafeTimedSerializer(secret, salt="alma-oauth")


@app.get("/")
def home():
    return send_from_directory(ROOT_DIR, "index.html")


@app.get("/health")
def health():
    ok = all(os.environ.get(k) for k in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_DRIVE_FOLDER_ID"))
    return jsonify(status="ok", configured=ok)


@app.get("/oauth2")
def oauth2():
    token = _setup_token()
    if token and not hmac.compare_digest(request.args.get("key", ""), token):
        return jsonify(ok=False, error="Chave inválida."), 403
    state = _state_serializer().dumps("ok")
    url, _ = make_flow().authorization_url(access_type="offline", prompt="consent", state=state)
    return redirect(url)


@app.get("/oauth2callback")
def oauth2callback():
    try:  # valida o "state" (anti-CSRF) assinado em /oauth2, válido por 10 min
        _state_serializer().loads(request.args.get("state", ""), max_age=600)
    except BadSignature:
        return jsonify(ok=False, error="Estado inválido ou expirado. Recomece em /oauth2."), 400
    if request.args.get("error"):
        return jsonify(ok=False, error="Autorização negada."), 400
    try:
        flow = make_flow()
        # Atrás do proxy do Render, request.url chega como http://; o oauthlib exige https.
        flow.fetch_token(authorization_response=REDIRECT_URI + "?" + request.query_string.decode())
    except Exception:
        log.exception("Falha ao trocar o código por token")
        return jsonify(ok=False, error="Falha ao obter o token. Recomece em /oauth2."), 400
    refresh = flow.credentials.refresh_token
    if not refresh:
        return jsonify(ok=False, error="O Google não enviou refresh token. Recomece em /oauth2."), 400
    resp = jsonify(ok=True, refresh_token=refresh,
                   proximo_passo="Copie para GOOGLE_REFRESH_TOKEN no Render.")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/upload")
def upload():
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id or not os.environ.get("GOOGLE_REFRESH_TOKEN"):
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
    props = {"tipo": tipo, "dono": (request.form.get("dono") or "")[:100],
             "para": (request.form.get("para") or "")[:100]}

    try:
        media = MediaIoBaseUpload(f.stream, mimetype=mime, chunksize=5 * 1024 * 1024, resumable=True)
        created = drive().files().create(
            body={"name": name, "parents": [folder_id], "appProperties": props},
            media_body=media, fields="id,name", supportsAllDrives=True,
        ).execute()
    except RefreshError:
        log.error("Refresh token inválido/expirado. Refaça a autorização em /oauth2.")
        return jsonify(ok=False, error="Autorização do Google expirada. Avise o administrador."), 503
    except HttpError as e:
        log.error("Erro do Google Drive (%s): %s", getattr(e.resp, "status", "?"), e)
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
