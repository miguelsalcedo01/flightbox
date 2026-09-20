"""Jev (TypeSafe AI): a decision model, called for advisory scores only.

Jev is NOT an LLM — no text generation. It answers one typed question at a
time (noul / choice / score) against a state object, through OpenRouter's
Decisions API. One caller uses it today: approvals.py (risk, advisory). It
may never let Jev decide anything — see the docstring there for the hard
constraint.

Tiny client, stdlib `urllib` only, matching cloud.py's house style: a plain
Request, a bare except around the socket, and an error message that carries
the HTTP status and nothing else — never the response body, never the API
key, because both land in the trace db.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

# Same reasoning as cloud.py's USER_AGENT: identify the client honestly
# rather than let a default header read as something it is not.
USER_AGENT = "flightbox-jev/1.0 (+https://flightbox.dev)"

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"


def advisor_enabled(jev_advisor_cfg: Optional[bool]) -> bool:
    """Opt-in rule. `jev_advisor_cfg` is `cfg.defaults.jev_advisor`.

    Off unless the engineer wrote `jev_advisor: true`. OPENROUTER_API_KEY is
    already set on most machines for the coding agents' own models, so keying
    off its presence would start sending approval details to a new third
    party on upgrade, unasked — not something a governor does quietly. Pure:
    takes the config value in, never reads cfg itself.
    """
    return jev_advisor_cfg is True


class JevUnavailable(RuntimeError):
    """Jev could not be reached, or answered in a shape we cannot use.

    Raised on a missing key, a non-2xx response, a timeout, or a malformed
    body. The message is status-only by construction — see build_request and
    ask for why the body and the key never make it into the text.
    """


def build_request(api_key: str, state: Any, questions: dict) -> urllib.request.Request:
    """Pure: build the Decisions API request. No network, so it is testable
    without ever touching OpenRouter."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode("utf-8")
    return urllib.request.Request(
        DECISIONS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )


def ask(state: Any, questions: dict, timeout: float = 3.0) -> dict:
    """Ask Jev one or more questions about `state`. Returns the decoded
    `answers` dict, keyed the same as `questions`.

    Raises JevUnavailable on anything that keeps the caller from getting a
    usable answer. The API key is read from the environment here, not passed
    in, so a caller can never accidentally log it alongside the request.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise JevUnavailable("OPENROUTER_API_KEY is not set")
    req = build_request(api_key, state, questions)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise JevUnavailable(f"jev request failed (HTTP {e.code})") from None
    except urllib.error.URLError as e:
        raise JevUnavailable(f"jev unreachable ({e.reason})") from None
    except TimeoutError:
        raise JevUnavailable(f"jev timed out after {timeout:.1f}s") from None
    except OSError as e:
        # A read timeout can surface as a bare socket.timeout/OSError rather
        # than wrapped in URLError, depending on platform — status-only here
        # too, never the underlying socket text.
        raise JevUnavailable(f"jev request failed ({e.__class__.__name__})") from None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise JevUnavailable("jev returned a body that could not be parsed") from None
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise JevUnavailable("jev response had no usable 'answers'")
    return answers


# ── pure question-building / answer-interpreting (no network) ───────────────

RISK_LEVELS = ("low", "medium", "high")

RISK_CRITERIA = {
    "low": "Reversible, small blast radius, no production, money, or credentials "
           "involved — e.g. a docs tweak, a local-only change, a dry run.",
    "medium": "Some blast radius or limited reversibility — touches a shared "
              "environment or a real user-facing surface, but not production data, "
              "money movement, or credentials.",
    "high": "Hard or impossible to reverse, wide blast radius, or touches "
            "production, money, or credentials directly — e.g. a production "
            "deploy, a database migration, a payment, a secret rotation.",
}


def risk_state(name: str, description: str, details: dict) -> dict:
    """The state Jev sees for a risk question — exactly the approval's own
    fields, nothing inferred and nothing added."""
    return {"name": name, "description": description, "details": details}


def risk_question() -> dict:
    """One `choice` question: which of the three risk levels fits best."""
    return {"risk": {"type": "choice", "instructions":
                     "How risky is approving this action? Judge reversibility, "
                     "blast radius, and whether production, money, or "
                     "credentials are involved.",
                     "criteria": RISK_CRITERIA}}


def interpret_risk(answers: dict) -> dict | None:
    """Pull {risk, confidence, probabilities} out of a Jev answers dict, or
    None if the shape is not one we recognize. Pure — no network, so every
    malformed-answer path is testable directly."""
    answer = answers.get("risk") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    if choice not in RISK_LEVELS:
        return None
    return {"risk": choice, "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities") or {}}
