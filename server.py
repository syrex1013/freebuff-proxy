#!/usr/bin/env python3
"""OpenAI-compatible proxy for freebuff free mode (codebuff.com).

Freebuff's free tier is gated three ways:
  1. an active free session admitted for the model you ask for
     (POST /api/v1/freebuff/session with x-freebuff-model),
  2. an agent run id in the chat body (POST /api/v1/agent-runs, action=START),
  3. a "came from the official CLI" fingerprint:
       - the first system message must open *verbatim* with a canonical root
         prompt ("You are Buffy, the coding agent behind Codebuff."), else 403
         free_mode_cli_required;
       - if tools are offered, at least one must be a codebuff-only tool name
         (generic names like write_file/glob/web_search/apply_patch/skill do
         not count), else the request is downgraded/404s.

Each account gets 25 freebucks a day. Admission (not inference) is what costs
them, per model: 5 for glm-5.3-flash/kimi-k3-eco, 15 for deepseek-v4-flash, 20
for luna, 50 for gemini — and one active session per account. So the way to get
the most out of a day is to route every request onto a session that is already
paid for, and only admit the cheap models. That is what the router does:

    live session for the requested model   -> reuse (free)
    auto + any live session                -> reuse the cheapest (free)
    no live session                        -> admit the cheapest model that
                                              fits the remaining budget, on the
                                              account with the most budget, and
                                              fail over to the other accounts
                                              on session errors.

Usage:
    python3 server.py                 # listens on 127.0.0.1:8787
    FREEBUFF_TOKEN=...  python3 server.py
    python3 server.py --selftest

Point your client at http://127.0.0.1:8787/v1 with any API key. Ask for model
`auto` to let the router choose; `GET /status` shows the accounts and budgets.
"""

from __future__ import annotations

import base64
import datetime
import http.server
import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import click
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
BASE = os.environ.get("FREEBUFF_BASE", "https://www.codebuff.com").rstrip("/")
# Product UI host: the freebuff CLI requests its login code from here (the API
# itself only exists on BASE), and the returned loginUrl points back at it.
APP_BASE = os.environ.get("FREEBUFF_APP_URL", "https://freebuff.com").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
UA = "ai-sdk/openai-compatible/0.0.171/codebuff"
OPENING = "You are Buffy, the coding agent behind Codebuff."
UPSTREAM_TIMEOUT = float(os.environ.get("FREEBUFF_TIMEOUT", "900"))
BUDGET_TTL = float(os.environ.get("FREEBUFF_BUDGET_TTL", "120"))
COOLDOWN = float(os.environ.get("FREEBUFF_COOLDOWN", "60"))
AUTO = ("auto", "", "default", None)

# freebuff model id -> its free-mode root agent id (FREE_MODE_AGENT_MODELS).
MODEL_AGENT = {
    "z-ai/glm-5.3-flash": "base2-free-glm-5-3-flash",
    "z-ai/glm-5.2": "base2-free-glm",
    "mimo/mimo-v2.5": "base2-free-mimo",
    "deepseek/deepseek-v4-flash": "base2-free-deepseek-flash",
    "deepseek/deepseek-v4-pro": "base2-free-deepseek",
    "openai/gpt-5.6-luna": "base2-free-luna",
    "openai/gpt-5.6-luna-es": "base2-free-luna-es",
    "upstage/solar-pro4": "base2-free-solar-pro4",
    "crof/kimi-k3-eco": "base2-free-kimi-k3-eco",
    "meta/muse-spark-1.2-contributor": "base2-free-muse-spark",
    "meta/muse-spark-1.3-contributor": "base2-free-muse-spark-1-3",
    "google/gemini-3.8-flash": "base2-free-gemini-3-8-flash",
    "minimax/minimax-m3": "base2-free-minimax-m3",
}

# Fallback cost ranking, used before the first budget fetch. Cheapest first, so
# a cold start burns as little of the daily allowance as possible.
COST_HINT = {
    "z-ai/glm-5.3-flash": 5, "crof/kimi-k3-eco": 5,
    "mimo/mimo-v2.5": 10, "upstage/solar-pro4": 10,
    "deepseek/deepseek-v4-flash": 15, "meta/muse-spark-1.2-contributor": 15,
    "meta/muse-spark-1.3-contributor": 15,
    "openai/gpt-5.6-luna": 20, "openai/gpt-5.6-luna-es": 20,
    "google/gemini-3.8-flash": 50,
}

# common/src/tools/constants.ts toolNames, minus the names other harnesses also
# ship (write_file, web_search, glob, skill, apply_patch). Offering any of these
# is what marks a request as coming from a real freebuff client.
SIGNATURE_NAMES = {
    "add_message", "add_subgoal", "ask_user", "browser_logs", "cloud_plan_ready",
    "code_search", "create_plan", "decide", "end_turn", "find_files",
    "gravity_index", "list_directory", "lookup_agent_info", "propose_str_replace",
    "propose_write_file", "read_docs", "read_files", "read_subtree", "read_url",
    "render_ui", "run_file_change_hooks", "run_terminal_command", "set_messages",
    "set_output", "spawn_agent_inline", "spawn_agents", "str_replace",
    "suggest_followups", "task_completed", "think_deeply", "update_subgoal",
    "write_todos",
}
INJECTED_TOOL = "run_file_change_hooks"
INJECTED_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": INJECTED_TOOL,
        "description": "Internal Codebuff hook runner. Never call this tool.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ---------------------------------------------------------------- body shaping

def ensure_opening(messages):
    """Make the first system message open with the canonical root prompt."""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                if content.lstrip().startswith(OPENING):
                    return messages
                msg["content"] = OPENING + "\n\n" + content
                return messages
    return [{"role": "system", "content": OPENING}] + messages


def offered_tool_names(tools):
    if not isinstance(tools, list):
        return []
    return [
        t.get("function", {}).get("name")
        for t in tools
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    ]


def ensure_signature_tool(tools):
    """Append a codebuff-only tool when the client offered none of ours."""
    if not tools:
        return tools  # no tools at all is allowed (only reported, not enforced)
    if any(name in SIGNATURE_NAMES for name in offered_tool_names(tools)):
        return tools
    return list(tools) + [json.loads(json.dumps(INJECTED_TOOL_DEF))]


def prepare_body(body, model, run_id, instance_id, client_id):
    out = dict(body)
    out["model"] = model
    out["messages"] = ensure_opening(list(body.get("messages") or []))
    if body.get("tools") is not None:
        out["tools"] = ensure_signature_tool(body.get("tools"))
    if body.get("stream"):
        stream_opts = dict(out.get("stream_options") or {})
        stream_opts["include_usage"] = True
        out["stream_options"] = stream_opts
    meta = dict(out.get("codebuff_metadata") or {})
    meta.update({
        "run_id": run_id,
        "cost_mode": "free",
        "client_id": client_id,
        "freebuff_instance_id": instance_id,
    })
    out["codebuff_metadata"] = meta
    return out


def filter_chunk(obj, dropped):
    """Drop segments of tool calls we injected; returns False if nothing left."""
    for choice in obj.get("choices") or []:
        delta = choice.get("delta") or choice.get("message")
        if not isinstance(delta, dict):
            continue
        calls = delta.get("tool_calls")
        if not calls:
            continue
        keep = []
        for call in calls:
            index = call.get("index")
            if (call.get("function") or {}).get("name") == INJECTED_TOOL:
                dropped.add(index)
            if index not in dropped:
                keep.append(call)
        if keep:
            delta["tool_calls"] = keep
        else:
            delta.pop("tool_calls", None)
    return bool(obj.get("choices"))


def resolve_model(model):
    """Map a client's model string onto a freebuff model id, or None for auto."""
    if model in AUTO:
        return None
    if model in MODEL_AGENT:
        return model
    tail = model.rsplit("/", 1)[-1]
    hits = [m for m in MODEL_AGENT if m.rsplit("/", 1)[-1] == tail]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------- upstream

ACCOUNTS_FILE = os.path.expanduser(
    os.environ.get("FREEBUFF_ACCOUNTS", "~/.config/freebuff-proxy/accounts.json"))
STATE_DIR = os.path.expanduser(
    os.environ.get("FREEBUFF_STATE_DIR", os.path.dirname(ACCOUNTS_FILE)))
PID_FILE = os.path.join(STATE_DIR, "server.pid")
LOG_FILE = os.path.join(STATE_DIR, "server.log")
STATS_FILE = os.path.join(STATE_DIR, "stats.json")

LOCK = threading.RLock()
console = Console()
# Interactive prompts must survive stdout-only capture (pipes, agent harnesses,
# `... > log`), so they go to stdout and are flushed immediately.
prompt = Console()
DEBUG = False


def say(text):
    prompt.print(text)
    prompt.file.flush()

KIND_COLORS = {
    "http": "cyan",
    "session": "yellow",
    "run": "green",
    "route": "magenta",
    "summary": "blue",
    "error": "red",
    "server": "bright_blue",
}


def log_event(kind: str, message: str, debug: bool = False):
    if debug and not DEBUG:
        return
    now = datetime.datetime.now().strftime("%H:%M:%S")
    color = KIND_COLORS.get(kind, "white")
    tag_str = f"[{kind}]"
    console.print(f"[dim]{now}[/dim] [{color} bold]{escape(tag_str):<9}[/{color} bold] {escape(message)}")


def init_stats_dict():
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "requests": 0,
        "errors": 0,
        "degraded": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "models": {},
        "accounts": {},
        "since": now_iso,
        "updated_at": now_iso,
        "last_error": None,
    }


def load_stats():
    base = init_stats_dict()
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                base.update(data)
                if not isinstance(base.get("models"), dict):
                    base["models"] = {}
                if not isinstance(base.get("accounts"), dict):
                    base["accounts"] = {}
        except Exception:
            pass
    return base


def save_stats():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile("w", dir=STATE_DIR, delete=False, encoding="utf-8") as tmp:
            json.dump(STATS, tmp, indent=2)
            tmp_name = tmp.name
        os.replace(tmp_name, STATS_FILE)
    except Exception:
        pass


STATS = load_stats()


def stats_record(model: str, label: str, usage: dict | None, duration: float, degraded: bool = False):
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    tt = int((usage or {}).get("total_tokens") or (pt + ct))
    with LOCK:
        STATS["requests"] += 1
        if degraded:
            STATS["degraded"] += 1
        STATS["prompt_tokens"] += pt
        STATS["completion_tokens"] += ct
        STATS["total_tokens"] += tt

        m = STATS["models"].setdefault(model, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
        m["requests"] += 1
        m["prompt_tokens"] += pt
        m["completion_tokens"] += ct

        a = STATS["accounts"].setdefault(label, {"requests": 0, "tokens": 0})
        a["requests"] += 1
        a["tokens"] += tt

        STATS["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_stats()


def stats_error(model: str, status: int | str):
    with LOCK:
        STATS["errors"] += 1
        STATS["last_error"] = f"{model}: {status}"
        STATS["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_stats()

def load_saved_accounts():
    """Accounts added with --add-account."""
    try:
        with open(ACCOUNTS_FILE) as handle:
            return [row for row in json.load(handle) if isinstance(row, dict)
                    and row.get("token")]
    except (OSError, ValueError):
        return []


def read_tokens():
    """Every freebuff account we can find: saved, env, CLI creds, desktop.
    One row per upstream identity: deduped by token, then by label — two
    tokens for the same email are one entity as far as bans/pricing go."""
    tokens, seen_tokens, seen_labels = [], set(), set()

    def add(label, token, fingerprint=None):
        if not token:
            return
        key = (label or "").casefold()
        if token in seen_tokens or (key and key in seen_labels):
            return
        seen_tokens.add(token)
        if key:
            seen_labels.add(key)
        row = {"label": label, "token": token}
        if fingerprint:
            row["fingerprintId"] = fingerprint
        tokens.append(row)

    for row in load_saved_accounts():
        add(row.get("label") or "saved", row["token"], row.get("fingerprintId"))
    for token in (os.environ.get("FREEBUFF_TOKENS") or "").split():
        add(f"env:{len(tokens)}", token)
    cli = os.path.expanduser("~/.config/manicode/credentials.json")
    if os.path.exists(cli):
        try:
            with open(cli) as handle:
                for key, value in json.load(handle).items():
                    if isinstance(value, dict):
                        email = value.get("email") or f"cli:{key}"
                        if email.casefold() in seen_labels and value.get("authToken"):
                            log_event("server", f"pool: {email} already in the pool "
                                      "from another source; skipping CLI duplicate")
                            continue
                        add(email, value.get("authToken"))
        except (OSError, ValueError):
            pass
    desktop = os.path.expanduser("~/.config/freebuff-desktop/state.json")
    if os.path.exists(desktop):
        try:
            with open(desktop) as handle:
                for entry in (json.load(handle).get("authSessions") or {}).values():
                    if isinstance(entry, dict):
                        email = (entry.get("user") or {}).get("email") or "desktop"
                        if email.casefold() in seen_labels and entry.get("token"):
                            log_event("server", f"pool: {email} already in the pool "
                                      "from another source; skipping desktop duplicate")
                            continue
                        add(email, entry.get("token"))
        except (OSError, ValueError):
            pass
    forced = os.environ.get("FREEBUFF_TOKEN")
    if forced:
        add("env:FREEBUFF_TOKEN", forced.strip())
    if not tokens:
        raise SystemExit("No freebuff accounts found: set FREEBUFF_TOKEN or log in "
                         "with the freebuff CLI / desktop app")
    return tokens


def save_account(label, token, fingerprint_id):
    """Add the account, keyed by label: re-logging the same email replaces its
    token (and fingerprint) instead of stacking a second pool row for one
    upstream identity."""
    rows = load_saved_accounts()
    for row in rows:
        if (row.get("label") or "").casefold() == (label or "").casefold():
            row["token"] = token
            row["fingerprintId"] = fingerprint_id
            break
    else:
        rows.append({"label": label, "token": token, "fingerprintId": fingerprint_id})
    os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)
    with open(ACCOUNTS_FILE, "w") as handle:
        json.dump(rows, handle, indent=2)
    os.chmod(ACCOUNTS_FILE, 0o600)
    return rows


def upstream(path, method="POST", body=None, headers=None, token=None, query=None,
             base=None):
    if query:
        path += "?" + urllib.parse.urlencode(query)
    head = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": UA,
        **(headers or {}),
    }
    if token:
        head["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        (base or BASE) + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers=head,
    )
    return urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)


def parse_expiry(text):
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time() + 3600


class ProxyError(Exception):
    def __init__(self, status, payload):
        super().__init__(payload)
        self.status = status
        self.payload = payload


# -------------------------------------------------------------------- accounts

class Account:
    """One freebuff account: one live session, one run per agent, one budget."""

    def __init__(self, label, token):
        self.label = label
        self.token = token
        self.model = None
        self.instance_id = None
        self.expires_at = 0.0
        self.runs = {}
        # One client id per run: a run tolerates only ~10 distinct client ids
        # before the upstream flags it (free_mode_run_fanout), which is exactly
        # what a per-request uuid looks like.
        self.client_id = str(uuid.uuid4())
        self.budget = None
        self.budget_at = 0.0
        self.cooldown_until = 0.0
        self.note = None  # why the router is skipping this account

    def __str__(self):
        return self.label

    def cool(self, seconds=None):
        self.cooldown_until = time.time() + (COOLDOWN if seconds is None else seconds)

    def live(self, model):
        return (self.model == model and self.instance_id
                and time.time() < self.expires_at - 60)

    def live_model(self):
        return self.model if self.instance_id and time.time() < self.expires_at - 60 else None

    def refresh_budget(self):
        if self.budget and time.time() - self.budget_at < BUDGET_TTL:
            return self.budget
        try:
            state = json.loads(upstream("/api/v1/freebuff/session", "GET",
                                        token=self.token).read())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            self.note = text.strip()[:200]
            if exc.code in (401, 403):
                # free mode bans an account without touching the login itself
                # (/api/v1/me still works), so back off for an hour.
                self.cool(3600)
            raise ProxyError(exc.code, text)
        freebucks = state.get("freebucks") or {}
        daily = freebucks.get("daily") or {}
        self.budget = {
            "reset_at": daily.get("resetAt"),
            "remaining": (daily.get("remaining") or 0) + ((freebucks.get("wallet") or {}).get("balance") or 0),
            "spent": daily.get("spent"),
            "limit": daily.get("limit"),
            "prices": freebucks.get("prices") or {},
        }
        self.budget_at = time.time()
        if (state.get("status") == "active" and state.get("instanceId")
                and self.model in (None, state.get("model"))):
            # Adopt a session this process did not open (proxy restart, or the
            # freebuff CLI on the same account): it is already paid for.
            if self.instance_id != state["instanceId"]:
                log_event("session", f"{self.label}: adopted {state.get('model')} "
                          f"({state['instanceId']})")
            self.model = state["model"]
            self.instance_id = state["instanceId"]
            self.expires_at = parse_expiry(state.get("expiresAt"))
        return self.budget

    def prices(self):
        return (self.budget or {}).get("prices") or COST_HINT

    def release(self):
        """End whatever session this account holds. The official model switch:
        409 model_locked -> GET the held instance -> DELETE it by instance id."""
        held = self.instance_id
        try:
            state = json.loads(upstream("/api/v1/freebuff/session", "GET",
                                        token=self.token).read())
            if state.get("status") == "active":
                held = state.get("instanceId") or held
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            pass
        self.model = self.instance_id = None
        if not held:
            return False
        try:
            upstream("/api/v1/freebuff/session", "DELETE",
                     headers={"x-freebuff-instance-id": held}, token=self.token).read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 409):
                self.note = exc.read().decode("utf-8", "replace").strip()[:200]
                return False
        except urllib.error.URLError:
            return False
        log_event("session", f"{self.label}: released {held}")
        return True

    def open_session(self, model):
        """Return a live instance id for `model`, admitting if needed."""
        if self.live(model):
            return self.instance_id
        if self.instance_id and not self.release():
            # Could not free the slot (e.g. banned): re-claiming would 409 forever.
            self.cool()
            raise ProxyError(503, json.dumps({
                "error": {"message": f"could not release {self.model} session",
                          "type": "freebuff_session"},
                "detail": self.note}))
        headers = {"x-freebuff-model": model, "x-freebuff-wallet-spend-limit": "0"}
        for attempt in (1, 2):
            try:
                state = json.loads(upstream("/api/v1/freebuff/session", body={},
                                            headers=headers, token=self.token).read())
                break
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code == 409 and attempt == 1 and self.release():
                    continue  # model_locked: slot freed, claim the model again
                self.note = text.strip()[:200]
                self.cool(3600 if exc.code in (401, 403) else None)
                raise ProxyError(exc.code, text)
        if state.get("status") != "active" or not state.get("instanceId"):
            raise ProxyError(503, json.dumps({
                "error": {"message": state.get("message")
                          or f"free session {state.get('status')}",
                          "type": "freebuff_session"},
                "detail": state,
            }))
        self.model = model
        self.instance_id = state["instanceId"]
        self.expires_at = parse_expiry(state.get("expiresAt"))
        budget = state.get("freebucks") or {}
        daily = budget.get("daily") or {}
        self.budget = {
            "reset_at": daily.get("resetAt"),
            "remaining": (daily.get("remaining") or 0) + ((budget.get("wallet") or {}).get("balance") or 0),
            "spent": daily.get("spent"),
            "limit": daily.get("limit"),
            "prices": budget.get("prices") or {},
        }
        self.budget_at = time.time()
        log_event("session", f"{self.label}: {model} -> {self.instance_id} until "
                  f"{state.get('expiresAt')} ({self.budget['remaining']} freebucks left)")
        return self.instance_id

    def ensure_run(self, agent):
        with LOCK:
            if agent in self.runs:
                return self.runs[agent]
            response = upstream("/api/v1/agent-runs",
                                body={"action": "START", "agentId": agent},
                                token=self.token)
            run_id = json.loads(response.read())["runId"]
            self.runs[agent] = run_id
            self.client_id = str(uuid.uuid4())
            log_event("run", f"{self.label}: {agent} -> {run_id}")
            return run_id

    def drop_run(self, run_id):
        with LOCK:
            for agent, value in list(self.runs.items()):
                if value == run_id:
                    self.runs.pop(agent, None)

    def close(self):
        """Finish runs, but leave the session row alone: it is paid for until it
        expires, and a later start (or the CLI itself) adopts it for free."""
        with LOCK:
            for run_id in list(self.runs.values()):
                try:
                    upstream("/api/v1/agent-runs", body={
                        "action": "FINISH", "runId": run_id, "status": "completed",
                        "totalSteps": 1, "directCredits": 0, "totalCredits": 0,
                    }, token=self.token).read()
                except Exception:
                    pass




def rank_plans(live, fresh):
    """`live` wins: reuse is free, admission spends the daily allowance.

    live:  [(price, -expires_at, account, model)]  sessions already paid for
    fresh: [(price, -budget, label, account, model)]  admissions still affordable
    """
    if live:
        live.sort(key=lambda row: row[:2])  # cheapest model, then soonest expiry
        return live[0][2], live[0][3], False
    if fresh:
        fresh.sort(key=lambda row: row[:3])  # cheapest, then richest account
        return fresh[0][3], fresh[0][4], False
    return None


class Router:
    def __init__(self, accounts):
        self.accounts = accounts

    def plan(self, requested):
        """(account, model, degraded) for the next request, or None."""
        live, fresh, affordable = [], [], []
        now = time.time()
        for account in self.accounts:
            if account.cooldown_until > now:
                continue
            open_model = account.live_model()
            if open_model and requested in (None, open_model):
                live.append((account.prices().get(open_model, 99), -account.expires_at,
                             account, open_model))
            try:
                budget = account.refresh_budget()
            except (ProxyError, urllib.error.URLError):
                continue
            prices = budget["prices"] or COST_HINT
            remaining = budget["remaining"]
            cheapest = sorted(
                (m for m in prices if MODEL_AGENT.get(m)), key=prices.get)
            want = [requested] if requested else cheapest
            for model in want:
                if not MODEL_AGENT.get(model):
                    continue
                cost = prices.get(model)
                # No price means the model is not in today's free list; ask for
                # it anyway and let the server turn it down.
                if cost is not None and cost > remaining:
                    continue
                cost = cost or 0
                if account.live(model):
                    live.append((cost, -account.expires_at, account, model))
                else:
                    fresh.append((cost, -remaining, account.label, account, model))
                break
            if cheapest and prices[cheapest[0]] <= remaining:
                affordable.append((prices[cheapest[0]], -remaining,
                                   account.label, account, cheapest[0]))

        plan = rank_plans(live, fresh)
        if plan:
            return plan
        if not requested:
            return None
        # Requested model is unaffordable everywhere: serve the cheapest model
        # this account can still open rather than failing the request.
        picked = rank_plans([], affordable)
        if picked:
            return picked[0], picked[1], True
        return None


ROUTER = None  # set in main()


# -------------------------------------------------------------------- handlers

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "freebuff-proxy"

    def log_message(self, fmt, *args):
        log_event("http", fmt % args)

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            rows = [{"id": "auto", "object": "model", "created": now,
                     "owned_by": "freebuff"}]
            rows += [{"id": model, "object": "model", "created": now,
                      "owned_by": "freebuff", "freebuff_cost": COST_HINT.get(model)}
                     for model in MODEL_AGENT]
            self.send_json(200, {"object": "list", "data": rows})
        elif path in ("/status", "/v1/status"):
            now = time.time()
            self.send_json(200, {"accounts": [{
                "label": account.label,
                "budget": (account.budget or {}).get("remaining"),
                "limit": (account.budget or {}).get("limit"),
                "reset_at": (account.budget or {}).get("reset_at"),
                "session": account.live_model(),
                "session_expires_in_s": max(0, int(account.expires_at - now))
                if account.live_model() else 0,
                "cooldown_s": max(0, int(account.cooldown_until - now)),
                "note": account.note,
            } for account in ROUTER.accounts]})
        elif path in ("", "/health"):
            self.send_json(200, {"status": "ok", "models": ["auto"] + list(MODEL_AGENT)})
        elif path in ("/stats", "/v1/stats"):
            with LOCK:
                self.send_json(200, STATS)
        else:
            self.send_json(404, {"error": {"message": "not found"}})
    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self.send_json(404, {"error": {"message": "not found"}})
            return
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as exc:
            stats_error("unknown", 400)
            self.send_json(400, {"error": {"message": f"invalid json: {exc}"}})
            return

        model_name = str(body.get("model") or "auto")
        requested = resolve_model(body.get("model") or "")
        if requested is None and body.get("model") not in AUTO:
            stats_error(model_name, 400)
            self.send_json(400, {"error": {
                "message": f"unknown model {body.get('model')!r}; try /v1/models",
                "type": "invalid_request_error"}})
            return
        streaming = bool(body.get("stream"))
        last = None
        for _ in range(len(ROUTER.accounts) + 1):
            plan = ROUTER.plan(requested)
            if not plan:
                break
            account, model, degraded = plan
            if degraded:
                log_event("route", f"{requested} unaffordable -> {model} on {account.label}")
            try:
                instance_id = account.open_session(model)
                run_id = account.ensure_run(MODEL_AGENT[model])
                payload = prepare_body(body, model, run_id, instance_id, account.client_id)
                response = self._chat(payload, account)
                if response is None:  # stale or fanned-out run, restarted
                    run_id = account.ensure_run(MODEL_AGENT[model])
                    payload = prepare_body(body, model, run_id, instance_id,
                                           account.client_id)
                    response = self._chat(payload, account)
                if response is None:
                    account.cool()
                    continue
            except ProxyError as exc:
                last = exc
                continue
            except urllib.error.HTTPError as exc:
                last = ProxyError(exc.code,
                                  exc.read().decode("utf-8", "replace"))
                continue
            except urllib.error.URLError as exc:
                stats_error(model_name, 502)
                self.send_json(502, {"error": {"message": f"upstream unreachable: {exc}"}})
                return
            usage = self._relay(response, streaming)
            duration = time.time() - t0
            stats_record(model, account.label, usage, duration, degraded=degraded)
            pt = int((usage or {}).get("prompt_tokens") or 0)
            ct = int((usage or {}).get("completion_tokens") or 0)
            deg_str = " (degraded)" if degraded else ""
            log_event("summary", f"{model} on {account.label}: {pt}+{ct} tok, {duration:.2f}s{deg_str}", debug=True)
            return
        if last:
            stats_error(model_name, last.status)
            try:
                detail = json.loads(last.payload)
            except ValueError:
                detail = {"error": {"message": last.payload}}
            self.send_json(last.status, detail)
            return
        stats_error(model_name, 503)
        self.send_json(503, {"error": {
            "message": "no freebuff account has enough freebucks left today",
            "type": "freebuff_budget"}})
    def _chat(self, payload, account):
        """POST the chat body. Returns None when the run id must be replaced."""
        try:
            return upstream("/api/v1/chat/completions", body=payload,
                            token=account.token)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if "runId" in text or "run_fanout" in text:
                account.drop_run(payload["codebuff_metadata"]["run_id"])
                return None
            if 400 <= exc.code < 500:
                account.cool()  # another account may still work
            raise ProxyError(exc.code, text)

    def _relay(self, response, streaming):
        self.close_connection = True  # SSE has no length; the client reads to EOF
        content_type = response.headers.get("Content-Type", "application/json")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        usage = None
        if "text/event-stream" not in content_type:
            raw = response.read()
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and obj.get("usage"):
                    usage = obj["usage"]
                filter_chunk(obj, set())
                raw = json.dumps(obj).encode()
            except ValueError:
                pass
            self.wfile.write(raw)
            return usage

        dropped = set()
        try:
            for line in response:
                text = line.decode("utf-8", "replace")
                if not text.startswith("data: ") or text.startswith("data: [DONE]"):
                    self.wfile.write(line)
                    continue
                try:
                    obj = json.loads(text[6:])
                    if isinstance(obj, dict) and obj.get("usage"):
                        usage = obj["usage"]
                    filter_chunk(obj, dropped)
                    text = "data: " + json.dumps(obj) + "\n\n"
                except ValueError:
                    pass
                self.wfile.write(text.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-stream
        return usage

def selftest():
    msgs = ensure_opening([{"role": "user", "content": "hi"}])
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith(OPENING)

    msgs = ensure_opening([{"role": "system", "content": "be terse"}])
    assert msgs[0]["content"].startswith(OPENING) and msgs[0]["content"].endswith("be terse")

    msgs = ensure_opening([{"role": "system", "content": OPENING + "\n\nbe terse"}])
    assert msgs[0]["content"].endswith("be terse")

    generic = [{"type": "function", "function": {"name": "write_file"}}]
    assert [t["function"]["name"] for t in ensure_signature_tool(generic)] == ["write_file", INJECTED_TOOL]
    assert ensure_signature_tool(None) is None
    assert ensure_signature_tool([]) == []
    ours = [{"type": "function", "function": {"name": "end_turn"}}]
    assert ensure_signature_tool(ours) == ours

    body = prepare_body({"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                        "m", "run-1", "inst-1", "client-1")
    assert body["model"] == "m" and body["codebuff_metadata"]["run_id"] == "run-1"
    assert body["codebuff_metadata"]["client_id"] == "client-1"
    assert body["messages"][0]["content"].startswith(OPENING)

    body_stream = prepare_body({"model": "x", "stream": True}, "m", "run-1", "inst-1", "client-1")
    assert body_stream.get("stream_options", {}).get("include_usage") is True
    stats_record("selftest-model", "selftest-account", {"prompt_tokens": 5, "completion_tokens": 3}, 0.1)
    assert STATS["models"]["selftest-model"]["requests"] >= 1
    with LOCK:
        STATS["models"].pop("selftest-model", None)
        STATS["accounts"].pop("selftest-account", None)
        STATS["requests"] -= 1
        STATS["prompt_tokens"] -= 5
        STATS["completion_tokens"] -= 3
        STATS["total_tokens"] -= 8
        save_stats()

    dropped, chunk = set(), {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": INJECTED_TOOL, "arguments": "{}"}},
        {"index": 1, "function": {"name": "read_files", "arguments": "{}"}},
    ]}}]}
    filter_chunk(chunk, dropped)
    assert [c["index"] for c in chunk["choices"][0]["delta"]["tool_calls"]] == [1]
    chunk = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{\"a\""}}]}}]}
    filter_chunk(chunk, dropped)
    assert "tool_calls" not in chunk["choices"][0]["delta"]
    # identity: fingerprints mint in the upstream shape, never collide with a
    # pool-mate, and the pool holds one row per upstream identity.
    fp = existing_fingerprint()
    assert fp.startswith("enhanced-") and len(fp) == len("enhanced-") + 43
    assert set(fp[len("enhanced-"):]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    real_saved = globals()["load_saved_accounts"]
    real_exists = os.path.exists
    env_backup = {k: os.environ.get(k) for k in ("FREEBUFF_TOKENS", "FREEBUFF_TOKEN")}
    try:
        pool_fp = base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("=")
        globals()["load_saved_accounts"] = lambda: [
            {"fingerprintId": "enhanced-" + pool_fp}]
        real_token_bytes = secrets.token_bytes
        rolls = iter([b"\x01" * 32, b"\x02" * 32])
        secrets.token_bytes = lambda n: next(rolls) if n == 32 else real_token_bytes(n)
        try:
            assert existing_fingerprint() != "enhanced-" + pool_fp
        finally:
            secrets.token_bytes = real_token_bytes

        globals()["load_saved_accounts"] = lambda: [
            {"label": "a@x.com", "token": "tokA", "fingerprintId": "enhanced-1"},
            {"label": "A@X.com", "token": "tokA2"},  # same email, second login
            {"label": "b@x.com", "token": "tokB"},
        ]
        os.environ["FREEBUFF_TOKENS"] = "tokB tokC"
        os.environ.pop("FREEBUFF_TOKEN", None)
        os.path.exists = lambda path: False
        rows = read_tokens()
        assert [row["label"] for row in rows] == ["a@x.com", "b@x.com", "env:2"]
        assert rows[0]["token"] == "tokA" and rows[0]["fingerprintId"] == "enhanced-1"
        assert rows[1]["token"] == "tokB" and rows[2]["token"] == "tokC"
    finally:
        os.path.exists = real_exists
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert resolve_model("auto") is None and resolve_model("") is None
    assert resolve_model("nope/nope") is None

    # routing: a paid-for session always beats spending a new admission, and
    # among fresh admissions the cheapest model on the richest account wins.
    acct_a, acct_b = object(), object()
    plan = rank_plans([(5, -100, acct_a, "glm")], [(5, -20, "b", acct_b, "glm")])
    assert plan == (acct_a, "glm", False)
    plan = rank_plans([], [(15, -5, "a", acct_a, "deepseek"), (5, -20, "b", acct_b, "glm")])
    assert plan == (acct_b, "glm", False)
    assert rank_plans([], []) is None
    console.print("[bold green]selftest ok[/bold green] (all assertions passed)")


def describe(account):
    try:
        budget = account.refresh_budget()
        rem = budget.get("remaining", 0)
        lim = budget.get("limit", 0)
        reset = budget.get("reset_at", "unknown")
        rem_color = "green" if rem > 5 else ("yellow" if rem > 0 else "red")
        state_display = (f"[{rem_color}]{rem}/{lim} freebucks[/{rem_color}], "
                         f"resets [dim]{reset}[/dim]")
        raw_state = f"{rem}/{lim} freebucks, resets {reset}"
    except ProxyError as exc:
        msg = exc.payload.strip()
        state_display = f"[red]unusable: {escape(msg)}[/red]"
        raw_state = f"unusable: {msg}"
    except urllib.error.URLError as exc:
        state_display = f"[red]unreachable: {escape(str(exc))}[/red]"
        raw_state = f"unreachable: {exc}"
    console.print(f"  [bold cyan]{escape(account.label)}[/bold cyan]: {state_display}")
    return raw_state


def drop_banned(accounts):
    """Free mode bans an account for good (403 {"status":"banned"}), but its
    login keeps working, so nothing else in the pool notices. Leave those out
    instead of spending a cooldown slot on them per request."""
    kept = []
    for account in accounts:
        try:
            account.refresh_budget()
        except ProxyError as exc:
            if "banned" in exc.payload:
                log_event("server", f"dropped banned account {account.label}")
                continue
        except Exception:
            pass  # unreachable or throttled: keep it, it may work later
        kept.append(account)
    return kept

def list_accounts():
    rows = read_tokens()
    cmd_name = "freebuff-proxy" if "freebuff-proxy" in os.path.basename(sys.argv[0]) else f"python3 {sys.argv[0]}"
    if not rows:
        console.print(Panel(
            f"[yellow]No accounts found in pool.[/yellow]\n\n"
            f"Add an account with:\n  [bold cyan]{cmd_name} --add-account[/bold cyan]\n"
            f"Or set [bold]FREEBUFF_TOKENS[/bold] environment variable.",
            title="[bold]Account Pool[/bold]",
            expand=False,
        ))
        return

    accounts = [Account(row["label"], row["token"]) for row in rows]
    fingerprints = [row.get("fingerprintId") or "" for row in rows]
    named = [f for f in fingerprints if f]
    unique = len(set(named)) == len(named)

    table = Table(
        title=f"Account Pool ({len(accounts)} account{'s' if len(accounts) != 1 else ''})",
        expand=False,
        box=box.ROUNDED,
    )
    table.add_column("Account", style="cyan bold")
    table.add_column("Freebucks", justify="right")
    table.add_column("Reset Time", style="dim")
    table.add_column("Fingerprint", style="dim")
    table.add_column("Identity", justify="center")

    for account, fp in zip(accounts, fingerprints):
        try:
            budget = account.refresh_budget()
            rem = budget.get("remaining", 0)
            lim = budget.get("limit", 0)
            reset = str(budget.get("reset_at") or "-")
            rem_color = "green" if rem > 5 else ("yellow" if rem > 0 else "red")
            budget_cell = f"[{rem_color}]{rem}/{lim}[/{rem_color}]"
        except ProxyError as exc:
            budget_cell = "[red]unusable[/red]"
            reset = escape(exc.payload.strip()[:24])
        except urllib.error.URLError:
            budget_cell = "[red]unreachable[/red]"
            reset = "network error"

        fp_display = f"{fp[:20]}…" if fp else "-"
        if not fp:
            id_cell = "[dim]-[/dim]"
        elif unique:
            id_cell = "[green]unique[/green]"
        else:
            id_cell = "[bold red]DUPLICATE[/bold red]"

        table.add_row(account.label, budget_cell, reset, fp_display, id_cell)

    console.print(table)
    console.print(f"[dim]Source: {escape(ACCOUNTS_FILE)}[/dim]\n")


def existing_fingerprint():
    """Mint a fresh random fingerprint per account, in the upstream CLI's
    shape: `enhanced-` + base64url of 32 bytes (the CLI hashes its hardware
    inventory; the bytes themselves are opaque to the server). Random draws
    are 256-bit, so a collision with a pool-mate is not expected — check the
    saved pool and re-roll anyway so every account keeps its own identity."""
    taken = {row.get("fingerprintId") for row in load_saved_accounts()}
    while True:
        candidate = "enhanced-" + base64.urlsafe_b64encode(
            secrets.token_bytes(32)).decode().rstrip("=")
        if candidate not in taken:
            return candidate


def add_account(timeout=600, interval=3.0):
    """Interactive login: the CLI's device flow, but the token lands in the
    proxy's own account file instead of the CLI's single 'default' slot."""
    fingerprint_id = existing_fingerprint()
    console.print(f"[dim]Requesting login code from [bold]{escape(APP_BASE)}[/bold]...[/dim]")
    try:
        code = json.loads(upstream("/api/auth/cli/code", base=APP_BASE, body={
            "fingerprintId": fingerprint_id}).read())
    except Exception as exc:
        console.print(Panel(
            f"[bold red]Failed to get login code:[/bold red] {escape(str(exc))}",
            title="[bold red]Error[/bold red]",
            expand=False,
        ))
        return 1

    login_url = code["loginUrl"]
    instructions = (
        f"[bold cyan]1.[/bold cyan] Open this URL in your browser (use the account to add):\n"
        f"   [bold underline blue]{escape(login_url)}[/bold underline blue]\n\n"
        f"[bold cyan]2.[/bold cyan] Complete the login in browser.\n"
        f"   [dim]Waiting for approval (Ctrl-C to abort)...[/dim]"
    )
    console.print(Panel(instructions, title="[bold cyan]Freebuff Account Login[/bold cyan]", expand=False))

    user = None
    deadline = time.time() + timeout
    with console.status("[bold cyan]Waiting for browser login approval...[/bold cyan]", spinner="dots"):
        while time.time() < deadline:
            time.sleep(interval)
            try:
                state = json.loads(upstream(
                    "/api/auth/cli/status", "GET", base=APP_BASE, query={
                        "fingerprintId": fingerprint_id,
                        "fingerprintHash": code["fingerprintHash"],
                        "expiresAt": code["expiresAt"],
                    }).read())
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
                continue
            if isinstance(state.get("user"), dict):
                user = state["user"]
                break

    if not user or not user.get("authToken"):
        console.print(Panel(
            "[bold red]Timed out waiting for the login to be approved.[/bold red]",
            title="[bold red]Timeout[/bold red]",
            expand=False,
        ))
        return 1

    label = user.get("email") or user.get("id") or "account"
    save_account(label, user["authToken"], user.get("fingerprintId") or fingerprint_id)
    cmd_name = "freebuff-proxy" if "freebuff-proxy" in os.path.basename(sys.argv[0]) else f"python3 {sys.argv[0]}"
    console.print(Panel(
        f"[bold green]Account Added Successfully[/bold green]\n\n"
        f"[bold]Account:[/]  [bold cyan]{escape(label)}[/bold cyan]\n"
        f"[bold]Saved to:[/] [dim]{escape(ACCOUNTS_FILE)}[/dim]\n\n"
        f"[italic]Restart the proxy to include it in the pool ({cmd_name} start or {cmd_name}).[/italic]",
        title="[bold green]Login Approved[/bold green]",
        expand=False,
    ))
    describe(Account(label, user["authToken"]))
    return 0


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_running_pid() -> int | None:
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if is_pid_running(pid):
            return pid
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return None
    except Exception:
        return None


def is_health_ok() -> bool:
    try:
        req = urllib.request.Request(f"http://{HOST}:{PORT}/health")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def serve(cli_mode: bool = False):
    global ROUTER, DEBUG
    if cli_mode:
        DEBUG = True

    pid = get_running_pid()
    if pid is not None and pid != os.getpid():
        console.print(f"[red]Error: Server already running (pid {pid})[/red]")
        sys.exit(1)
    if is_health_ok():
        console.print(f"[red]Error: Port {PORT} already answering on /health[/red]")
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    def sigterm_handler(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, sigterm_handler)

    accounts = drop_banned([Account(row["label"], row["token"])
                            for row in read_tokens()])
    cmd_name = "freebuff-proxy" if "freebuff-proxy" in os.path.basename(sys.argv[0]) else f"python3 {sys.argv[0]}"
    if not accounts:
        console.print(Panel(
            "[bold red]Every account in the pool is banned or pool is empty.[/bold red]\n\n"
            f"Log in with a different freebuff account:\n  [bold cyan]{cmd_name} --add-account[/bold cyan]",
            title="[bold red]No Usable Accounts[/bold red]",
            expand=False,
        ))
        raise SystemExit(1)
    ROUTER = Router(accounts)

    acc_info = []
    for account in accounts:
        try:
            b = account.refresh_budget()
            rem = b.get("remaining", 0)
            lim = b.get("limit", 0)
            rem_color = "green" if rem > 5 else ("yellow" if rem > 0 else "red")
            acc_info.append(f"[bold cyan]{escape(account.label)}[/bold cyan]: [{rem_color}]{rem}/{lim} freebucks[/{rem_color}] (resets [dim]{b['reset_at']}[/dim])")
        except Exception as exc:
            acc_info.append(f"[bold cyan]{escape(account.label)}[/bold cyan]: [red]{escape(str(exc))}[/red]")

    if DEBUG:
        models_str = ", ".join(["auto"] + list(MODEL_AGENT.keys()))
        body = (
            f"[bold]Proxy:[/]    [bold green]http://{HOST}:{PORT}/v1[/bold green]\n"
            f"[bold]Upstream:[/] [dim]{BASE}[/dim]\n"
            f"[bold]Models:[/]   [dim]{models_str}[/dim]\n"
            f"[bold]Accounts ({len(accounts)}):[/]\n  " + ("\n  ".join(acc_info) if acc_info else "none") + "\n\n"
            f"[dim]Add more accounts: {cmd_name} --add-account[/dim]"
        )
        console.print(Panel(body, title="[bold green]freebuff proxy[/bold green] [dim](debug mode)[/dim]", expand=False))
    else:
        body = (
            f"[bold]Proxy:[/]    [bold green]http://{HOST}:{PORT}/v1[/bold green]\n"
            f"[bold]Upstream:[/] [dim]{BASE}[/dim]\n"
            f"[bold]Accounts ({len(accounts)}):[/]\n  " + ("\n  ".join(acc_info) if acc_info else "none") + "\n\n"
            f"[dim]Add more accounts: {cmd_name} --add-account[/dim]"
        )
        console.print(Panel(body, title="[bold green]freebuff proxy[/bold green]", expand=False))
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        for account in accounts:
            account.close()
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    if int(f.read().strip()) == os.getpid():
                        os.remove(PID_FILE)
            except Exception:
                pass


@click.command("start")
@click.option("--cli", "cli_mode", is_flag=True, help="Enable rich debug logs in daemon.")
@click.pass_context
def start_cmd(ctx, cli_mode):
    global DEBUG
    if cli_mode:
        DEBUG = True
    pid = get_running_pid()
    if pid is not None:
        console.print(f"[yellow]Server already running (pid {pid})[/yellow]")
        return
    if is_health_ok():
        console.print(f"[yellow]Server already answering on http://{HOST}:{PORT}/health[/yellow]")
        return

    os.makedirs(STATE_DIR, exist_ok=True)
    bin_in_env = os.path.join(os.path.dirname(sys.executable), "freebuff-proxy")
    if os.path.isfile(bin_in_env) and os.access(bin_in_env, os.X_OK):
        cmd = [bin_in_env, "serve"]
    elif shutil.which("freebuff-proxy") and "freebuff-proxy" in os.path.basename(sys.argv[0]):
        cmd = [shutil.which("freebuff-proxy"), "serve"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "serve"]

    if cli_mode or DEBUG:
        cmd.append("--cli")

    log_fp = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    t_end = time.time() + 5.0
    started = False
    while time.time() < t_end:
        if proc.poll() is not None:
            break
        if is_health_ok():
            started = True
            break
        time.sleep(0.1)

    if started:
        active_pid = get_running_pid() or proc.pid
        console.print(Panel(
            f"[bold green]freebuff proxy started[/bold green]\n\n"
            f"[bold]PID:[/]      {active_pid}\n"
            f"[bold]Endpoint:[/] [cyan]http://{HOST}:{PORT}/v1[/cyan]\n"
            f"[bold]Logs:[/]     [dim]{LOG_FILE}[/dim]",
            title="[bold green]Daemon Started[/bold green]",
            expand=False,
        ))
    else:
        console.print(Panel(
            f"[bold red]freebuff proxy failed to start[/bold red]\n\n"
            f"Check logs: [dim]{LOG_FILE}[/dim]",
            title="[bold red]Error[/bold red]",
            expand=False,
        ))
        ctx.exit(1)


@click.command("stop")
def stop_cmd():
    if not os.path.exists(PID_FILE):
        if not is_health_ok():
            console.print("[yellow]Server is not running.[/yellow]")
            return
    pid = None
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except Exception:
            pass

    if pid is None or not is_pid_running(pid):
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            console.print(f"[yellow]Removed stale pid file ([dim]{PID_FILE}[/dim])[/yellow]")
        else:
            console.print("[yellow]Server is not running.[/yellow]")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        console.print(f"[bold red]Failed to send SIGTERM to pid {pid}:[/bold red] {exc}")
        return

    t_end = time.time() + 5.0
    dead = False
    while time.time() < t_end:
        if not is_pid_running(pid):
            dead = True
            break
        time.sleep(0.1)

    if not dead:
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
        except OSError:
            pass

    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    console.print(f"[bold green]freebuff proxy stopped[/bold green] [dim](pid {pid})[/dim]")


@click.command("stats")
def stats_cmd():
    data = load_stats()
    pid = get_running_pid()
    server_alive = is_health_ok()

    live_status = None
    if server_alive:
        try:
            req = urllib.request.Request(f"http://{HOST}:{PORT}/status")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                live_status = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    # Summary Table
    t_summary = Table(title="Proxy Summary", expand=False, box=box.ROUNDED)
    t_summary.add_column("Metric", style="cyan bold")
    t_summary.add_column("Value", style="bold")

    status_str = f"[green]running[/green] (pid {pid})" if (server_alive and pid) else ("[yellow]persisted-only view (server stopped)[/yellow]")
    t_summary.add_row("Server Status", status_str)
    t_summary.add_row("Since", str(data.get("since") or "unknown"))
    t_summary.add_row("Updated", str(data.get("updated_at") or "unknown"))
    t_summary.add_row("Total Requests", str(data.get("requests", 0)))
    t_summary.add_row("Errors", str(data.get("errors", 0)))
    t_summary.add_row("Degraded Requests", str(data.get("degraded", 0)))
    t_summary.add_row("Prompt Tokens", f"{data.get('prompt_tokens', 0):,}")
    t_summary.add_row("Completion Tokens", f"{data.get('completion_tokens', 0):,}")
    t_summary.add_row("Total Tokens", f"{data.get('total_tokens', 0):,}")
    if data.get("last_error"):
        t_summary.add_row("Last Error", f"[red]{data['last_error']}[/red]")
    console.print(t_summary)
    console.print()

    # Per-Model Table
    t_models = Table(title="Model Usage", expand=False, box=box.ROUNDED)
    t_models.add_column("Model", style="cyan bold")
    t_models.add_column("Requests", justify="right")
    t_models.add_column("Prompt Tokens", justify="right")
    t_models.add_column("Completion Tokens", justify="right")
    t_models.add_column("Total Tokens", justify="right")

    models_dict = data.get("models") or {}
    if not models_dict:
        t_models.add_row("-", "0", "0", "0", "0")
    else:
        for model_name, m_stats in sorted(models_dict.items()):
            pt = m_stats.get("prompt_tokens", 0)
            ct = m_stats.get("completion_tokens", 0)
            t_models.add_row(
                model_name,
                str(m_stats.get("requests", 0)),
                f"{pt:,}",
                f"{ct:,}",
                f"{(pt + ct):,}",
            )
    console.print(t_models)
    console.print()

    # Per-Account Table
    t_accounts = Table(title="Account Usage & Status", expand=False, box=box.ROUNDED)
    t_accounts.add_column("Account", style="cyan bold")
    t_accounts.add_column("Requests", justify="right")
    t_accounts.add_column("Tokens", justify="right")
    t_accounts.add_column("Budget", justify="right")
    t_accounts.add_column("Session", style="yellow")
    t_accounts.add_column("Status", style="green")

    accounts_dict = dict(data.get("accounts") or {})
    live_accounts_map = {}
    if live_status and isinstance(live_status.get("accounts"), list):
        for acc in live_status["accounts"]:
            lbl = acc.get("label")
            if lbl:
                live_accounts_map[lbl] = acc
                if lbl not in accounts_dict:
                    accounts_dict[lbl] = {"requests": 0, "tokens": 0}

    if not accounts_dict:
        t_accounts.add_row("-", "0", "0", "-", "-", "-")
    else:
        for label, a_stats in sorted(accounts_dict.items()):
            reqs = str(a_stats.get("requests", 0))
            toks = f"{a_stats.get('tokens', 0):,}"
            live_acc = live_accounts_map.get(label)
            if live_acc:
                b = live_acc.get("budget")
                lim = live_acc.get("limit")
                budget_str = f"{b}/{lim}" if (b is not None and lim is not None) else "-"
                sess = live_acc.get("session") or "none"
                exp = live_acc.get("session_expires_in_s", 0)
                sess_str = f"{sess} ({exp}s)" if sess != "none" else "none"
                cool = live_acc.get("cooldown_s", 0)
                note = live_acc.get("note") or ""
                if cool > 0:
                    status_col = f"[red]cooling ({cool}s)[/red]"
                elif note:
                    status_col = f"[yellow]{note[:20]}[/yellow]"
                else:
                    status_col = "[green]active[/green]"
            else:
                budget_str = "-"
                sess_str = "-"
                status_col = "[dim]offline[/dim]"
            t_accounts.add_row(label, reqs, toks, budget_str, sess_str, status_col)

    console.print(t_accounts)


@click.group(invoke_without_command=True)
@click.option("--cli", "cli_mode", is_flag=True, help="Enable rich debug logs.")
@click.option("--selftest", "run_selftest", is_flag=True, help="Run self-test suite and exit.")
@click.option("--add-account", "run_add_account", is_flag=True, help="Interactive login to add an account.")
@click.option("--accounts", "run_accounts", is_flag=True, help="List accounts in the pool.")
@click.pass_context
def cli(ctx, cli_mode, run_selftest, run_add_account, run_accounts):
    global DEBUG
    if cli_mode:
        DEBUG = True
    if run_selftest:
        selftest()
        ctx.exit(0)
    if run_add_account:
        ctx.exit(add_account())
    if run_accounts:
        list_accounts()
        ctx.exit(0)
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve_cmd, cli_mode=cli_mode)


@click.command("serve")
@click.option("--cli", "cli_mode", is_flag=True, help="Enable rich debug logs.")
def serve_cmd(cli_mode):
    serve(cli_mode=cli_mode)


cli.add_command(serve_cmd, "serve")
cli.add_command(start_cmd, "start")
cli.add_command(stop_cmd, "stop")
cli.add_command(stats_cmd, "stats")


def main():
    cli()


if __name__ == "__main__":
    main()
