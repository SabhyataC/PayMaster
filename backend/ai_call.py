"""One place for every Gemini call: short backoff, then a second model, then a clear error.

B4: a 503/429 no longer makes an upload wait minutes. We try GEMINI_MODEL up to 3 times with
a short pause (2s, 5s), then GEMINI_FALLBACK_MODEL (if set) up to 2 times. If everything fails
the caller gets an exception and falls back to its own safe path (extraction -> 'failed' and
can be retried with one click; Verify -> keeps the hard-check decision).
"""
import os
import random
import time

TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "503", "500", "UNAVAILABLE", "overloaded", "disconnected",
             "getaddrinfo", "timed out", "timeout", "Connection", "connect")
PAUSES = (2.0, 5.0)  # seconds between tries on the same model


def _transient(e):
    msg = str(e)
    return any(t in msg for t in TRANSIENT)


def models():
    """Primary model first, then the optional fallback (never the same one twice)."""
    out = [os.environ["GEMINI_MODEL"]]
    fb = os.environ.get("GEMINI_FALLBACK_MODEL", "").strip()
    if fb and fb not in out:
        out.append(fb)
    return out


def generate(client, contents, config, sleep=time.sleep):
    """Returns (model_used, response). Raises the last error if every try failed."""
    last = None
    for mi, model in enumerate(models()):
        tries = 3 if mi == 0 else 2
        for attempt in range(tries):
            try:
                return model, client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as e:
                last = e
                if not _transient(e):
                    raise
                if attempt < tries - 1:
                    sleep(PAUSES[min(attempt, len(PAUSES) - 1)] + random.random())
    raise last
