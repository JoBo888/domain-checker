"""Standalone .com domain availability checker for Open Name Lab.

This service is intentionally independent of the Souldesha/Ananta backend.
It exposes only:
    GET  /health
    POST /domain-check
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)

MAX_NAMES = 100
MAX_NAME_LENGTH = 40
MAX_WORKERS = 4
REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "OpenNameLab-DomainChecker/1.0"


def _cors_origins():
    """Read allowed browser origins from CORS_ORIGINS.

    Set CORS_ORIGINS to a comma-separated list in production, for example:
    https://souldesha.de,https://www.souldesha.de
    The default is permissive so the service can be tested before the final
    website origin is known.
    """
    raw = os.environ.get("CORS_ORIGINS", "*").strip()
    if raw == "*":
        return "*"
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


CORS(app, resources={r"/*": {"origins": _cors_origins()}})


def _ascii_name(value):
    """Convert a user-entered name to a conservative ASCII domain label."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = re.sub(r"\.(?:com|net|org|de)$", "", text)
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _validated_names(raw_names):
    if not isinstance(raw_names, list):
        raise ValueError("Das Feld 'names' muss eine Liste sein.")

    names = []
    seen = set()
    for raw in raw_names[:MAX_NAMES]:
        name = _ascii_name(raw)
        if not (3 <= len(name) <= MAX_NAME_LENGTH):
            continue
        if name not in seen:
            seen.add(name)
            names.append(name)

    if not names:
        raise ValueError("Keine gültigen Domainnamen gefunden.")
    return sorted(names, key=str.casefold)


def _check_one(name):
    domain = name + ".com"
    url = "https://rdap.verisign.com/com/v1/domain/" + urllib.parse.quote(domain, safe="")
    last_detail = "Keine eindeutige Antwort"

    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/rdap+json, application/json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if 200 <= response.status < 300:
                    return {
                        "domain": domain,
                        "status": "registered",
                        "label": "registriert",
                        "detail": "Verisign-RDAP: Eintrag gefunden",
                    }
                last_detail = "Verisign-RDAP antwortete mit HTTP " + str(response.status)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {
                    "domain": domain,
                    "status": "available",
                    "label": "wahrscheinlich frei",
                    "detail": "Verisign-RDAP: kein Eintrag gefunden",
                }
            last_detail = "Verisign-RDAP antwortete mit HTTP " + str(exc.code)
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            last_detail = "Verisign-RDAP nicht erreichbar"
            if reason:
                last_detail += " (temporäre Netzwerkantwort)"

        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))

    return {
        "domain": domain,
        "status": "unknown",
        "label": "nicht eindeutig",
        "detail": last_detail,
    }


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "domain-checker"})


@app.post("/domain-check")
def domain_check():
    data = request.get_json(silent=True) or {}
    try:
        names = _validated_names(data.get("names", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_one, name): name for name in names}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                name = futures[future]
                results.append({
                    "domain": name + ".com",
                    "status": "unknown",
                    "label": "nicht eindeutig",
                    "detail": "Interner Prüffehler – bitte erneut versuchen",
                })

    results.sort(key=lambda item: item["domain"].casefold())
    return jsonify({"results": results, "checked": len(results)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
