#!/usr/bin/env python3
"""Where a trial's model calls go.

The default is Grok CLI's own catalog (no custom base URL). An on-prem
OpenAI-compatible gateway is opt-in. The secret stays in the environment;
this module only ever handles the variable's name.

Grok CLI reads a custom endpoint from the trial ``GROK_HOME`` config, not
from a flag. See ``model_sections``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ENV_BASE_URL = "SPEEDREAD_BASE_URL"
ENV_MODELS = "SPEEDREAD_MODELS"
ENV_API_KEY_ENV = "SPEEDREAD_API_KEY_ENV"

# grok_eval's historical default when nobody sets a model.
GROK_EVAL_DEFAULT_MODELS = "grok-4.7"


def add_arguments(parser, *, default_models: str):
    """Flags. ``None`` means "not passed", so an env var can still win."""
    parser.add_argument("--base-url", default=None,
                        help=f"OpenAI-compatible base URL (env {ENV_BASE_URL}). Unset keeps Grok's own API.")
    parser.add_argument("--models", default=None,
                        help=f"comma-separated model ids (env {ENV_MODELS}; default {default_models})")
    parser.add_argument("--api-key-env", default=None,
                        help=f"name of the env var that holds the key, not the key (env {ENV_API_KEY_ENV})")


def _pick(flag, env_name, default, environ):
    if flag is not None:
        return flag
    env = environ.get(env_name)
    if env:
        return env
    return default


def resolve(flag_base_url, flag_models, flag_api_key_env, *, default_models, environ=None):
    """Flag, then env, then default. A custom base URL requires a key-var name."""
    environ = os.environ if environ is None else environ
    base = _pick(flag_base_url, ENV_BASE_URL, None, environ)
    models_raw = _pick(flag_models, ENV_MODELS, default_models, environ)
    key_env = _pick(flag_api_key_env, ENV_API_KEY_ENV, None, environ)
    if isinstance(base, str):
        base = base.strip().rstrip("/") or None
    if isinstance(key_env, str):
        key_env = key_env.strip() or None
    models = [part.strip() for part in (models_raw or "").split(",") if part.strip()]
    if not models:
        raise SystemExit("no models configured")
    if base and not (base.startswith("http://") or base.startswith("https://")):
        raise SystemExit(f"base URL must be http or https, got {base!r}")
    if key_env and not key_env.isidentifier():
        raise SystemExit(f"api key env var name is not an identifier: {key_env!r}")
    if base and not key_env:
        raise SystemExit(
            f"a custom base URL needs --api-key-env or {ENV_API_KEY_ENV} "
            "(the variable's name, not the secret)"
        )
    return {
        "base_url": base,
        "models": models,
        "api_key_env": key_env,
    }


def redact(text, secret):
    if secret and text and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def public_view(endpoint, secret=None):
    """Fields safe to print. Never includes a key value."""
    view = {
        "base_url": endpoint["base_url"],
        "models": list(endpoint["models"]),
        "api_key_env": endpoint["api_key_env"],
    }
    if secret:
        blob = redact(json.dumps(view), secret)
        view = json.loads(blob)
    return view


def model_sections(endpoint):
    """TOML ``[model.*]`` blocks for one trial's GROK_HOME.

    Grok CLI 1.0.92 selects ``-m`` against the section key and sends ``model``
    to ``base_url``. ``env_key`` is the variable name. ``api_backend`` is
    ``chat_completions``, the documented default for an OpenAI-compatible
    endpoint. The key value is not written.
    """
    base = endpoint.get("base_url")
    if not base:
        return ""
    lines = []
    key_env = endpoint.get("api_key_env")
    for model in endpoint["models"]:
        quoted = '"' + model.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines += [
            "",
            f"[model.{quoted}]",
            f"model = {quoted}",
            f"base_url = {_q(base)}",
            f"name = {quoted}",
            'api_backend = "chat_completions"',
        ]
        if key_env:
            lines.append(f"env_key = {_q(key_env)}")
    return "\n".join(lines) + "\n"


def _q(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def models_url(base_url):
    return base_url.rstrip("/") + "/models"


def list_model_ids(base_url, api_key_env, environ=None, timeout=15):
    """GET ``{base}/models``. Returns ids. Does not return the key."""
    environ = os.environ if environ is None else environ
    if not api_key_env:
        raise SystemExit("preflight needs the name of the API-key env var")
    secret = environ.get(api_key_env)
    if not secret:
        raise SystemExit(f"{api_key_env} is unset")
    request = urllib.request.Request(
        models_url(base_url),
        headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode(errors="replace")[:300], secret)
        raise SystemExit(f"preflight {models_url(base_url)} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"preflight could not reach {models_url(base_url)}: {redact(str(exc.reason), secret)}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit("preflight: /models did not return JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SystemExit("preflight: /models JSON has no data list")
    ids = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
        elif isinstance(item, str):
            ids.append(item)
    return ids


def preflight_report(endpoint, environ=None):
    """Text for stdout. The key value is redacted if it somehow appears."""
    environ = os.environ if environ is None else environ
    secret = environ.get(endpoint["api_key_env"] or "", "")
    ids = list_model_ids(endpoint["base_url"], endpoint["api_key_env"], environ)
    missing = [model for model in endpoint["models"] if model not in ids]
    lines = [
        f"preflight {models_url(endpoint['base_url'])}",
        f"key_env {endpoint['api_key_env']} is set",
        "models " + ", ".join(ids),
    ]
    if missing:
        lines.append("missing " + ", ".join(missing))
    text = "\n".join(lines) + "\n"
    text = redact(text, secret)
    if missing:
        raise SystemExit(text)
    return text
