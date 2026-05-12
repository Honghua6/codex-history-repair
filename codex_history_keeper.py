#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe local Codex history vault with a small Windows GUI.

The tool intentionally exports readable session logs instead of modifying
Codex auth files, cookies, or live SQLite databases.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tomllib
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:  # pragma: no cover - command-line sync still works.
    tk = None
    ttk = None
    filedialog = None
    messagebox = None


APP_NAME = "Codex History Keeper"
CONFIG_DIR = Path.home() / ".codex_history_keeper"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "keeper.log"
DEFAULT_VAULT_ROOT = Path.home() / "Documents" / "CodexHistoryVault"
SCRIPT_PATH = Path(__file__).resolve()

SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "gh[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{20,}\b"), r"\1 [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)\b"
            r"(\s*[:=]\s*)[\"']?[A-Za-z0-9._~+/=-]{12,}[\"']?"
        ),
        r"\1\2[REDACTED]",
    ),
]

SKIP_TEXT_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<app-context>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<system",
    "<developer",
    "<user_instructions",
    "# AGENTS.md instructions",
)


@dataclass
class ExportResult:
    out_dir: Path
    session_count: int
    index_md: Path
    searchable_messages: Path


@dataclass
class RepairResult:
    rebuilt_db: Path
    backup_dir: Path
    session_count: int
    current_provider: str
    applied: bool


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def detect_current_provider(codex_home: Path) -> str:
    """Return the provider Codex is currently configured to use.

    Official account login normally has no explicit top-level model_provider,
    so Codex uses the built-in OpenAI provider. API-key setups usually set
    model_provider = "name" in config.toml.
    """
    config_path = codex_home / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "openai"
    except Exception as exc:
        log_line(f"Could not parse config.toml for provider detection: {exc}")
        return "openai"

    provider = data.get("model_provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    return "openai"


def known_provider_names(codex_home: Path) -> list[str]:
    names = {"openai", detect_current_provider(codex_home)}
    try:
        data = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        configured = data.get("model_providers")
        if isinstance(configured, dict):
            names.update(str(name) for name in configured.keys() if str(name).strip())
    except Exception:
        pass
    for path in session_files(codex_home):
        for row in read_jsonl(path):
            payload = row.get("payload") or {}
            if row.get("type") == "session_meta" and isinstance(payload, dict):
                provider = payload.get("model_provider")
                if isinstance(provider, str) and provider.strip():
                    names.add(provider.strip())
                break
    return sorted(names)


def log_line(message: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip().lstrip("\ufeff")
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    log_line(f"Skipped invalid JSON in {path}:{line_no}: {exc}")
    except FileNotFoundError:
        return []
    return rows


def load_session_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(codex_home / "session_index.jsonl"):
        session_id = row.get("id")
        if session_id:
            index[session_id] = row
    return index


def load_prompt_history(codex_home: Path) -> dict[str, list[dict[str, Any]]]:
    prompts: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(codex_home / "history.jsonl"):
        session_id = row.get("session_id")
        text = row.get("text")
        if session_id and text:
            prompts.setdefault(session_id, []).append(row)
    return prompts


def session_files(codex_home: Path) -> list[Path]:
    files: list[Path] = []
    for name in ("sessions", "archived_sessions"):
        folder = codex_home / name
        if folder.exists():
            files.extend(sorted(folder.rglob("*.jsonl")))
    return files


def is_archived_session_path(path: Path) -> bool:
    return any(part == "archived_sessions" for part in path.parts)


def extract_id_from_name(path: Path) -> str:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.stem,
        re.IGNORECASE,
    )
    return match.group(1) if match else path.stem


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def redact(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def latest_time(values: list[str | None]) -> str | None:
    parsed = [(parse_datetime(value), value) for value in values if value]
    parsed = [(stamp, value) for stamp, value in parsed if stamp is not None]
    if not parsed:
        return next((value for value in values if value), None)
    return max(parsed, key=lambda item: item[0])[1]


def earliest_time(values: list[str | None]) -> str | None:
    parsed = [(parse_datetime(value), value) for value in values if value]
    parsed = [(stamp, value) for stamp, value in parsed if stamp is not None]
    if not parsed:
        return next((value for value in values if value), None)
    return min(parsed, key=lambda item: item[0])[1]


def should_skip_text(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in SKIP_TEXT_PREFIXES)


def timestamp_from_epoch(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def history_prompt_records(prompt_history: dict[str, list[dict[str, Any]]], session_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in prompt_history.get(session_id, []):
        text = item.get("text") or ""
        if text and not should_skip_text(text):
            records.append(
                {
                    "role": "user",
                    "timestamp": timestamp_from_epoch(item.get("ts")),
                    "text": text.rstrip(),
                    "source": "history.jsonl",
                }
            )
    records.sort(key=lambda record: parse_datetime(record.get("timestamp")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return records


def summarize_title(text: str, limit: int = 56) -> str:
    single = re.sub(r"\s+", " ", text).strip()
    if len(single) <= limit:
        return single
    return single[: limit - 1].rstrip() + "..."


def unique_run_name(prefix: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def normalized_message_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalized_message_key(message: dict[str, Any]) -> tuple[str, str]:
    return normalized_message_text(message.get("text") or ""), str(message.get("timestamp") or "")


def supplement_user_messages(messages: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> bool:
    seen = {normalized_message_key(message) for message in messages if message.get("role") == "user"}
    changed = False
    for candidate in candidates:
        if candidate.get("role") != "user":
            continue
        key = normalized_message_key(candidate)
        if not key[0] or key in seen:
            continue
        messages.append(candidate)
        seen.add(key)
        changed = True
    return changed


def sort_messages_by_timestamp(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    utc_min = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return [
        message
        for _, message in sorted(
            enumerate(messages),
            key=lambda item: (parse_datetime(item[1].get("timestamp")) or utc_min, item[0]),
        )
    ]


def message_identity(message: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(message.get("role") or ""),
        str(message.get("timestamp") or ""),
        str(message.get("phase") or ""),
        str(message.get("text") or ""),
    )


def dedupe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for message in sort_messages_by_timestamp(messages):
        key = message_identity(message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(message)
    return merged


def title_score(title: str | None, session_id: str) -> tuple[int, int]:
    text = (title or "").strip()
    if not text:
        return (0, 0)
    if text == session_id:
        return (1, len(text))
    return (2, len(text))


def choose_better_title(current: str | None, candidate: str | None, session_id: str) -> str:
    return candidate or current or session_id if title_score(candidate, session_id) > title_score(current, session_id) else current or candidate or session_id


def session_priority(session: dict[str, Any]) -> tuple[int, int, int]:
    updated = parse_datetime(session.get("updated_at"))
    updated_epoch = int(updated.timestamp()) if updated is not None else 0
    message_count = int(session.get("message_count") or 0)
    archived = 1 if "archived_sessions" in str(session.get("source_file") or "") else 0
    return (message_count, updated_epoch, -archived)


def merge_session_records(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    preferred = existing if session_priority(existing) >= session_priority(candidate) else candidate
    other = candidate if preferred is existing else existing
    merged = dict(preferred)
    merged["messages"] = dedupe_messages(list(existing.get("messages") or []) + list(candidate.get("messages") or []))
    merged["message_count"] = len(merged["messages"])
    merged["title"] = choose_better_title(existing.get("title"), candidate.get("title"), str(preferred.get("id") or ""))
    merged["updated_at"] = latest_time([existing.get("updated_at"), candidate.get("updated_at")])
    merged["started_at"] = earliest_time([existing.get("started_at"), candidate.get("started_at")])
    merged["cwd"] = preferred.get("cwd") or other.get("cwd")
    merged["originator"] = preferred.get("originator") or other.get("originator")
    merged["cli_version"] = preferred.get("cli_version") or other.get("cli_version")
    merged["model_provider"] = preferred.get("model_provider") or other.get("model_provider")
    merged["_source_mtime"] = max(float(existing.get("_source_mtime") or 0.0), float(candidate.get("_source_mtime") or 0.0))
    source_files: list[str] = []
    for source in list(existing.get("source_files") or []) + [str(existing.get("source_file") or "")]:
        if source and source not in source_files:
            source_files.append(source)
    for source in list(candidate.get("source_files") or []) + [str(candidate.get("source_file") or "")]:
        if source and source not in source_files:
            source_files.append(source)
    merged["source_files"] = source_files
    merged["source_file"] = preferred.get("source_file") or other.get("source_file") or ""
    return merged


def parse_session(
    path: Path,
    session_index: dict[str, dict[str, Any]],
    prompt_history: dict[str, list[dict[str, Any]]],
    redact_enabled: bool,
) -> dict[str, Any]:
    session_id = extract_id_from_name(path)
    meta: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    fallback_messages: list[dict[str, Any]] = []

    for row in read_jsonl(path):
        timestamp = row.get("timestamp")
        row_type = row.get("type")
        payload = row.get("payload") or {}

        if row_type == "session_meta" and isinstance(payload, dict):
            meta = payload
            session_id = payload.get("id") or session_id
            continue

        if row_type == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type == "user_message":
                text = payload.get("message") or ""
                if text and not should_skip_text(text):
                    messages.append(
                        {
                            "role": "user",
                            "timestamp": timestamp,
                            "text": redact(text.rstrip(), redact_enabled),
                        }
                    )
            elif event_type == "agent_message":
                text = payload.get("message") or ""
                if text and not should_skip_text(text):
                    messages.append(
                        {
                            "role": "assistant",
                            "timestamp": timestamp,
                            "phase": payload.get("phase"),
                            "text": redact(text.rstrip(), redact_enabled),
                        }
                    )
            continue

        if row_type == "response_item" and isinstance(payload, dict):
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = text_from_content(payload.get("content")).rstrip()
            if text and not should_skip_text(text):
                fallback_messages.append(
                    {
                        "role": role,
                        "timestamp": timestamp,
                        "text": redact(text, redact_enabled),
                    }
                )

    if not messages:
        messages = fallback_messages
    else:
        supplement_user_messages(messages, fallback_messages)

    if session_id in prompt_history:
        history_messages = [
            {**record, "text": redact(record["text"], redact_enabled)}
            for record in history_prompt_records(prompt_history, session_id)
        ]
        if supplement_user_messages(messages, history_messages):
            messages = sort_messages_by_timestamp(messages)

    indexed = session_index.get(session_id, {})
    title = indexed.get("thread_name") or ""
    if not title or title.strip() == session_id:
        first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
        title = summarize_title(first_user) if first_user else (title or session_id)
    title = redact(title, redact_enabled)

    timestamps = [m.get("timestamp") for m in messages]
    updated_at = latest_time([indexed.get("updated_at"), *timestamps])
    started_at = meta.get("timestamp") or (timestamps[0] if timestamps else None)

    return {
        "id": session_id,
        "title": title,
        "source_file": str(path),
        "cwd": meta.get("cwd"),
        "originator": meta.get("originator"),
        "cli_version": meta.get("cli_version"),
        "model_provider": meta.get("model_provider"),
        "started_at": started_at,
        "updated_at": updated_at,
        "message_count": len(messages),
        "messages": messages,
        "source_files": [str(path)],
        "_source_mtime": path.stat().st_mtime,
    }


def build_history_only_session(
    session_id: str,
    prompt_history: dict[str, list[dict[str, Any]]],
    session_index: dict[str, dict[str, Any]],
    codex_home: Path,
    redact_enabled: bool,
) -> dict[str, Any] | None:
    messages = [
        {**record, "text": redact(record["text"], redact_enabled)}
        for record in history_prompt_records(prompt_history, session_id)
    ]
    if not messages:
        return None
    indexed = session_index.get(session_id, {})
    first_user = next((m["text"] for m in messages if m.get("role") == "user"), "")
    title = indexed.get("thread_name") or summarize_title(first_user) or session_id
    title = redact(title, redact_enabled)
    timestamps = [m.get("timestamp") for m in messages]
    history_path = codex_home / "history.jsonl"
    return {
        "id": session_id,
        "title": title,
        "source_file": str(history_path),
        "source_files": [str(history_path)],
        "cwd": None,
        "originator": None,
        "cli_version": None,
        "model_provider": None,
        "started_at": earliest_time(timestamps),
        "updated_at": latest_time([indexed.get("updated_at"), *timestamps]),
        "message_count": len(messages),
        "messages": messages,
        "_source_mtime": history_path.stat().st_mtime if history_path.exists() else 0.0,
    }


def session_matches_export_filters(
    session: dict[str, Any],
    wanted_ids: set[str],
    since_dt: dt.datetime | None,
) -> bool:
    if wanted_ids and str(session.get("id") or "") not in wanted_ids:
        return False
    updated = parse_datetime(session.get("updated_at"))
    if since_dt and updated and updated < since_dt:
        return False
    if since_dt and updated is None:
        source_mtime = float(session.get("_source_mtime") or 0.0)
        if source_mtime and dt.datetime.fromtimestamp(source_mtime, tz=dt.timezone.utc) < since_dt:
            return False
    return int(session.get("message_count") or 0) > 0


def to_epoch_seconds(value: str | None, fallback: float | None = None) -> int:
    parsed = parse_datetime(value)
    if parsed is not None:
        return int(parsed.timestamp())
    if fallback is not None:
        return int(fallback)
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def to_epoch_millis(value: str | None, fallback: float | None = None) -> int:
    parsed = parse_datetime(value)
    if parsed is not None:
        return int(parsed.timestamp() * 1000)
    if fallback is not None:
        return int(fallback * 1000)
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def extended_windows_path(path_text: str | None) -> str:
    if not path_text:
        path_text = str(Path.home())
    if not sys.platform.startswith("win"):
        return path_text
    text = str(Path(path_text).expanduser())
    if text.startswith("\\\\?\\") or text.startswith("\\\\"):
        return text
    if re.match(r"^[A-Za-z]:\\", text):
        return "\\\\?\\" + text
    return text


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def repair_source_text(meta: dict[str, Any]) -> str:
    source = meta.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    if isinstance(source, dict):
        if source.get("subagent"):
            return "subagent"
        if source:
            return "structured"
    return "vscode" if meta.get("originator") == "Codex Desktop" else "cli"


def extract_repair_metadata(
    path: Path,
    indexed: dict[str, Any],
    prompt_history: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    session_id = extract_id_from_name(path)
    meta: dict[str, Any] = {}
    turn_context: dict[str, Any] = {}
    first_user = ""
    has_user_event = False
    last_timestamp: str | None = None

    for row in read_jsonl(path):
        timestamp = row.get("timestamp")
        if timestamp:
            last_timestamp = timestamp
        row_type = row.get("type")
        payload = row.get("payload") or {}
        if row_type == "session_meta" and isinstance(payload, dict):
            meta = payload
            session_id = payload.get("id") or session_id
            continue
        if row_type == "turn_context" and isinstance(payload, dict) and not turn_context:
            turn_context = payload
            continue
        if row_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
            has_user_event = True
            text = payload.get("message") or ""
            if text and not should_skip_text(text):
                first_user = text.rstrip()
                continue
        if row_type == "response_item" and isinstance(payload, dict) and not first_user:
            if payload.get("type") == "message" and payload.get("role") == "user":
                has_user_event = True
                text = text_from_content(payload.get("content")).rstrip()
                if text and not should_skip_text(text):
                    first_user = text

    if not first_user and prompt_history:
        first_user = next(
            (record["text"] for record in history_prompt_records(prompt_history, session_id) if record.get("text")),
            "",
        )
        if first_user:
            has_user_event = True

    title = indexed.get("thread_name") or summarize_title(first_user) or session_id
    created_text = meta.get("timestamp") or path_timestamp_from_name(path) or last_timestamp
    updated_text = latest_time([indexed.get("updated_at"), last_timestamp, created_text])
    cwd = turn_context.get("cwd") or meta.get("cwd") or str(Path.home())
    sandbox_policy = turn_context.get("sandbox_policy") or {"type": "danger-full-access"}
    approval_mode = turn_context.get("approval_policy") or "never"
    collaboration = turn_context.get("collaboration_mode") or {}
    collab_settings = collaboration.get("settings") if isinstance(collaboration, dict) else {}
    reasoning_effort = (
        turn_context.get("model_reasoning_effort")
        or (collab_settings or {}).get("reasoning_effort")
        or None
    )
    model = turn_context.get("model") or None

    return {
        "id": session_id,
        "rollout_path": str(path),
        "created_at": to_epoch_seconds(created_text, path.stat().st_ctime),
        "updated_at": to_epoch_seconds(updated_text, path.stat().st_mtime),
        "source": repair_source_text(meta),
        "model_provider": meta.get("model_provider") or "openai",
        "cwd": extended_windows_path(str(cwd)),
        "title": title,
        "sandbox_policy": compact_json(sandbox_policy),
        "approval_mode": approval_mode,
        "tokens_used": 0,
        "has_user_event": 1 if has_user_event else 0,
        "archived": 1 if is_archived_session_path(path) else 0,
        "archived_at": to_epoch_seconds(updated_text, path.stat().st_mtime) if is_archived_session_path(path) else None,
        "git_sha": None,
        "git_branch": None,
        "git_origin_url": None,
        "cli_version": meta.get("cli_version") or "",
        "first_user_message": first_user,
        "agent_nickname": None,
        "agent_role": None,
        "memory_mode": "enabled",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "agent_path": None,
        "created_at_ms": to_epoch_millis(created_text, path.stat().st_ctime),
        "updated_at_ms": to_epoch_millis(updated_text, path.stat().st_mtime),
        "thread_source": None,
        "updated_at_iso": updated_text,
        "source_files": [str(path)],
        "_source_mtime": path.stat().st_mtime,
    }


def path_timestamp_from_name(path: Path) -> str | None:
    match = re.search(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})", path.name)
    if not match:
        return None
    date_part, time_part = match.group(1).split("T", 1)
    return f"{date_part}T{time_part.replace('-', ':')}Z"


def markdown_escape_heading(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").strip() or "Untitled"


def markdown_escape_link_text(text: str) -> str:
    return markdown_escape_heading(text).replace("[", r"\[").replace("]", r"\]")


def message_heading(message: dict[str, Any]) -> str:
    role = "User" if message.get("role") == "user" else "Assistant"
    timestamp = message.get("timestamp")
    phase = message.get("phase")
    parts = [role]
    if phase:
        parts.append(f"phase={phase}")
    if timestamp:
        parts.append(str(timestamp))
    return " - ".join(parts)


def render_session_markdown(session: dict[str, Any], exported_at: str) -> str:
    source_files = list(session.get("source_files") or [])
    primary_source = session.get("source_file") or (source_files[0] if source_files else "")
    lines = [
        f"# {markdown_escape_heading(session['title'])}",
        "",
        f"- Session ID: `{session['id']}`",
        f"- Source file: `{primary_source}`",
        f"- CWD: `{session.get('cwd') or ''}`",
        f"- Started: `{session.get('started_at') or ''}`",
        f"- Updated: `{session.get('updated_at') or ''}`",
        f"- Exported: `{exported_at}`",
        f"- Messages: `{session['message_count']}`",
        "",
    ]
    if len(source_files) > 1:
        lines.extend(["## Source Files", ""])
        lines.extend([f"- `{source}`" for source in source_files])
        lines.append("")
    lines.extend(["## Conversation", ""])
    for message in session["messages"]:
        lines.extend(
            [
                f"### {message_heading(message)}",
                "",
                message.get("text", "").rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_index_markdown(sessions: list[dict[str, Any]], exported_at: str, codex_home: Path) -> str:
    lines = [
        "# Codex History Vault",
        "",
        f"- Exported: `{exported_at}`",
        f"- Codex home: `{codex_home}`",
        f"- Sessions: `{len(sessions)}`",
        "",
        "## Conversations",
        "",
    ]
    for session in sessions:
        md_name = str(session.get("markdown") or f"conversations/{session['id']}.md")
        title = markdown_escape_link_text(session["title"])
        updated = session.get("updated_at") or ""
        count = session.get("message_count") or 0
        lines.append(f"- [{title}]({md_name}) - `{session['id']}` - `{updated}` - {count} messages")
    lines.extend(
        [
            "",
            "## How To Reuse",
            "",
            "Open a conversation Markdown file and provide it as context in the new Codex account.",
            "Use `searchable_messages.jsonl` for keyword search, indexing, or later retrieval.",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_codex_history(
    codex_home: Path,
    out_dir: Path | None = None,
    since: str | None = None,
    thread_ids: list[str] | None = None,
    max_sessions: int | None = None,
    redact_enabled: bool = True,
    dry_run: bool = False,
) -> ExportResult:
    codex_home = codex_home.expanduser().resolve()
    if not codex_home.exists():
        raise FileNotFoundError(f"Codex home does not exist: {codex_home}")

    since_dt = parse_datetime(since)
    wanted_ids = {item.strip() for item in (thread_ids or []) if item.strip()}
    exported_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if out_dir is None:
        out_dir = DEFAULT_VAULT_ROOT / unique_run_name("codex-history")
    out_dir = out_dir.expanduser().resolve()
    conversations_dir = out_dir / "conversations"

    session_index = load_session_index(codex_home)
    prompt_history = load_prompt_history(codex_home)
    combined: dict[str, dict[str, Any]] = {}

    for path in session_files(codex_home):
        session = parse_session(path, session_index, prompt_history, redact_enabled)
        session_id = str(session["id"])
        existing = combined.get(session_id)
        combined[session_id] = merge_session_records(existing, session) if existing else session

    for session_id in prompt_history:
        if session_id in combined:
            continue
        history_only = build_history_only_session(session_id, prompt_history, session_index, codex_home, redact_enabled)
        if history_only is not None:
            combined[session_id] = history_only

    parsed = [session for session in combined.values() if session_matches_export_filters(session, wanted_ids, since_dt)]

    parsed.sort(
        key=lambda item: parse_datetime(item.get("updated_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    if max_sessions is not None:
        parsed = parsed[:max_sessions]

    if dry_run:
        return ExportResult(
            out_dir=out_dir,
            session_count=len(parsed),
            index_md=out_dir / "index.md",
            searchable_messages=out_dir / "searchable_messages.jsonl",
        )

    conversations_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    searchable_path = out_dir / "searchable_messages.jsonl"
    with searchable_path.open("w", encoding="utf-8") as searchable:
        for session in parsed:
            safe_session_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(session["id"]))
            md_rel = Path("conversations") / f"{safe_session_id}.md"
            json_rel = Path("conversations") / f"{safe_session_id}.json"
            md_path = out_dir / md_rel
            json_path = out_dir / json_rel
            session["markdown"] = md_rel.as_posix()
            session["json"] = json_rel.as_posix()
            md_path.write_text(render_session_markdown(session, exported_at), encoding="utf-8")
            write_json(json_path, session)

            manifest_item = {key: value for key, value in session.items() if key not in {"messages", "_source_mtime"}}
            manifest.append(manifest_item)

            for number, message in enumerate(session["messages"], 1):
                record = {
                    "session_id": session["id"],
                    "title": session["title"],
                    "message_number": number,
                    "role": message.get("role"),
                    "timestamp": message.get("timestamp"),
                    "text": message.get("text"),
                }
                searchable.write(json.dumps(record, ensure_ascii=False) + "\n")

    write_json(out_dir / "index.json", manifest)
    (out_dir / "index.md").write_text(render_index_markdown(parsed, exported_at, codex_home), encoding="utf-8")
    return ExportResult(
        out_dir=out_dir,
        session_count=len(parsed),
        index_md=out_dir / "index.md",
        searchable_messages=searchable_path,
    )


def load_config() -> dict[str, Any]:
    default = {
        "codex_home": str(default_codex_home()),
        "vault_root": str(DEFAULT_VAULT_ROOT),
        "codex_exe": "",
        "codex_app_id": "",
    }
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            default.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_line(f"Could not read config: {exc}")
    return default


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_vault_path(path: Path) -> bool:
    try:
        return path.is_dir() and (path / "index.md").exists()
    except OSError:
        return False


def local_vault_candidates(vault_root: Path) -> list[Path]:
    if not vault_root.exists():
        return []
    return [path for path in vault_root.glob("codex-history-*") if valid_vault_path(path)]


def resolve_latest_pointer(vault_root: Path, pointer_text: str) -> Path | None:
    text = pointer_text.strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = vault_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return resolved if valid_vault_path(resolved) else None


def latest_vault(vault_root: Path) -> Path | None:
    vault_root = vault_root.expanduser().resolve()
    pointer = vault_root / "_latest.txt"
    pointed = None
    if pointer.exists():
        try:
            pointed = resolve_latest_pointer(vault_root, pointer.read_text(encoding="utf-8"))
        except OSError as exc:
            log_line(f"Could not read latest vault pointer {pointer}: {exc}")
    candidates = local_vault_candidates(vault_root)
    if not candidates:
        return pointed
    if pointed is not None:
        return pointed
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_reuse_prompt(vault_root: Path, latest: Path) -> Path:
    prompt_path = vault_root / "_reuse_this_history_in_codex.md"
    text = f"""# 在新登录方式里复用 Codex 历史

请把下面这个本地对话备份目录作为可读上下文使用：

`{latest}`

优先阅读：

- `{latest / "index.md"}`
- `{latest / "searchable_messages.jsonl"}`
- `{latest / "conversations"}`

如果我要继续某个旧任务，请先在对话备份里搜索相关标题、关键词、工作目录或时间，再读取对应的 Markdown 对话记录并总结上下文。
"""
    prompt_path.write_text(text, encoding="utf-8")
    return prompt_path


def sync_vault(config: dict[str, Any], max_sessions: int | None = None) -> ExportResult:
    codex_home = Path(config["codex_home"]).expanduser()
    vault_root = Path(config["vault_root"]).expanduser().resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    out_dir = vault_root / unique_run_name("codex-history")
    result = export_codex_history(codex_home=codex_home, out_dir=out_dir, max_sessions=max_sessions)
    pointer = vault_root / "_latest.txt"
    pointer.write_text(result.out_dir.name + "\n", encoding="utf-8")
    write_reuse_prompt(vault_root, result.out_dir)
    log_line(f"Synced {result.session_count} sessions to {result.out_dir}")
    return result


def open_path(path: Path) -> None:
    path = path.expanduser().resolve()
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def pythonw_executable() -> Path:
    current = Path(sys.executable)
    if current.name.lower() == "python.exe":
        candidate = current.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return current


def create_shortcut(
    link_path: Path,
    target_path: Path,
    arguments: str,
    working_directory: Path,
    description: str,
    icon_path: Path | None = None,
) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    icon = icon_path or target_path
    ps = "\n".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({powershell_quote(str(link_path))})",
            f"$shortcut.TargetPath = {powershell_quote(str(target_path))}",
            f"$shortcut.Arguments = {powershell_quote(arguments)}",
            f"$shortcut.WorkingDirectory = {powershell_quote(str(working_directory))}",
            f"$shortcut.Description = {powershell_quote(description)}",
            f"$shortcut.IconLocation = {powershell_quote(str(icon))}",
            "$shortcut.Save()",
        ]
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Failed to create shortcut")


def desktop_path() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home()
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_desktop_launcher() -> Path:
    link_path = desktop_path() / "Codex History Launcher.lnk"
    create_shortcut(
        link_path=link_path,
        target_path=pythonw_executable(),
        arguments=f'"{SCRIPT_PATH}" --launch',
        working_directory=SCRIPT_PATH.parent,
        description="Sync local Codex history vault, then open Codex.",
    )
    log_line(f"Installed desktop launcher: {link_path}")
    return link_path


def install_startup_sync() -> Path:
    link_path = startup_folder() / "Codex History Keeper Auto Sync.lnk"
    create_shortcut(
        link_path=link_path,
        target_path=pythonw_executable(),
        arguments=f'"{SCRIPT_PATH}" --sync --quiet',
        working_directory=SCRIPT_PATH.parent,
        description="Refresh local Codex history vault at Windows sign-in.",
    )
    log_line(f"Installed startup sync: {link_path}")
    return link_path


def remove_startup_sync() -> bool:
    link_path = startup_folder() / "Codex History Keeper Auto Sync.lnk"
    if link_path.exists():
        link_path.unlink()
        log_line(f"Removed startup sync: {link_path}")
        return True
    return False


def discover_codex_app_id() -> str:
    ps = r"""
$pkg = Get-AppxPackage OpenAI.Codex -ErrorAction SilentlyContinue
if ($pkg) {
  $manifest = Get-AppxPackageManifest -Package $pkg.PackageFullName
  $app = $manifest.Package.Applications.Application | Select-Object -First 1
  if ($app) { "$($pkg.PackageFamilyName)!$($app.Id)" }
}
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    return ""


def launch_codex(config: dict[str, Any]) -> None:
    codex_exe = str(config.get("codex_exe") or "").strip()
    if codex_exe and Path(codex_exe).exists():
        subprocess.Popen([codex_exe])
        return

    app_id = str(config.get("codex_app_id") or "").strip()
    if not app_id:
        app_id = discover_codex_app_id()
        if app_id:
            config["codex_app_id"] = app_id
            save_config(config)
    if app_id:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        return

    raise RuntimeError("没有找到 Codex 应用。请在界面里选择 Codex.exe，或从桌面启动器同步后手动打开 Codex。")


def search_records(vault_root: Path, query: str, limit: int = 80) -> list[dict[str, Any]]:
    latest = latest_vault(vault_root)
    if not latest:
        return []
    searchable = latest / "searchable_messages.jsonl"
    terms = [term.casefold() for term in query.split() if term.strip()]
    if not terms:
        return []
    results: list[dict[str, Any]] = []
    for row in read_jsonl(searchable):
        haystack = f"{row.get('title') or ''}\n{row.get('text') or ''}".casefold()
        if all(term in haystack for term in terms):
            row["markdown"] = str(latest / "conversations" / f"{row.get('session_id')}.md")
            results.append(row)
            if len(results) >= limit:
                break
    return results


def collect_repair_threads(codex_home: Path, provider_mode: str = "current") -> tuple[list[dict[str, Any]], str]:
    session_index = load_session_index(codex_home)
    prompt_history = load_prompt_history(codex_home)
    rows: list[dict[str, Any]] = []
    for path in session_files(codex_home):
        row = extract_repair_metadata(path, session_index.get(extract_id_from_name(path), {}), prompt_history)
        rows.append(row)
    rows.sort(key=lambda item: item["updated_at_ms"])
    selected_provider = detect_current_provider(codex_home)
    if provider_mode in {"current", "auto"}:
        for item in rows:
            item["model_provider"] = selected_provider
    elif provider_mode and provider_mode not in {"preserve", "current", "auto"}:
        for item in rows:
            item["model_provider"] = provider_mode
        selected_provider = provider_mode
    elif provider_mode == "preserve":
        selected_provider = next((item["model_provider"] for item in reversed(rows) if item.get("model_provider")), selected_provider)
    return rows, selected_provider


def read_sqlite_schema(db_path: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    tables: list[str] = []
    indexes: list[str] = []
    triggers: list[str] = []
    table_names: list[str] = []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        for typ, name, sql in con.execute(
            "select type, name, sql from sqlite_master "
            "where sql is not null and type in ('table','index','trigger') "
            "order by case type when 'table' then 0 when 'index' then 1 else 2 end, rootpage, name"
        ):
            if name == "sqlite_sequence" or name.startswith("sqlite_autoindex"):
                continue
            if typ == "table":
                tables.append(sql)
                table_names.append(name)
            elif typ == "index":
                indexes.append(sql)
            elif typ == "trigger":
                triggers.append(sql)
    finally:
        con.close()
    return tables, indexes, triggers, table_names


def fallback_state_schema() -> tuple[list[str], list[str], list[str], list[str]]:
    tables = [
        """CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT
)"""
    ]
    indexes = [
        "CREATE INDEX idx_threads_archived ON threads(archived)",
        "CREATE INDEX idx_threads_created_at ON threads(created_at DESC, id DESC)",
        "CREATE INDEX idx_threads_created_at_ms ON threads(created_at_ms DESC, id DESC)",
        "CREATE INDEX idx_threads_provider ON threads(model_provider)",
        "CREATE INDEX idx_threads_source ON threads(source)",
        "CREATE INDEX idx_threads_updated_at ON threads(updated_at DESC, id DESC)",
        "CREATE INDEX idx_threads_updated_at_ms ON threads(updated_at_ms DESC, id DESC)",
        "CREATE INDEX idx_threads_archived_cwd_created_at_ms ON threads(archived, cwd, created_at_ms DESC, id DESC)",
        "CREATE INDEX idx_threads_archived_cwd_updated_at_ms ON threads(archived, cwd, updated_at_ms DESC, id DESC)",
    ]
    triggers = [
        """CREATE TRIGGER threads_created_at_ms_after_insert AFTER INSERT ON threads
WHEN NEW.created_at_ms IS NULL BEGIN
    UPDATE threads SET created_at_ms = NEW.created_at * 1000 WHERE id = NEW.id;
END""",
        """CREATE TRIGGER threads_created_at_ms_after_update AFTER UPDATE OF created_at ON threads
WHEN NEW.created_at != OLD.created_at AND NEW.created_at_ms IS OLD.created_at_ms BEGIN
    UPDATE threads SET created_at_ms = NEW.created_at * 1000 WHERE id = NEW.id;
END""",
        """CREATE TRIGGER threads_updated_at_ms_after_insert AFTER INSERT ON threads
WHEN NEW.updated_at_ms IS NULL BEGIN
    UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id;
END""",
        """CREATE TRIGGER threads_updated_at_ms_after_update AFTER UPDATE OF updated_at ON threads
WHEN NEW.updated_at != OLD.updated_at AND NEW.updated_at_ms IS OLD.updated_at_ms BEGIN
    UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id;
END""",
    ]
    return tables, indexes, triggers, ["threads"]


def copy_table_rows(old_db: Path, new_db: Path, table_names: list[str]) -> None:
    skip = {"threads", "thread_dynamic_tools", "stage1_outputs", "thread_goals", "thread_spawn_edges", "sqlite_sequence"}
    src = sqlite3.connect(f"file:{old_db.as_posix()}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(str(new_db), timeout=10)
    try:
        dst.execute("pragma foreign_keys=off")
        for table in table_names:
            if table in skip:
                continue
            try:
                columns = [row[1] for row in src.execute(f"pragma table_info({table})")]
                if not columns:
                    continue
                placeholders = ",".join("?" for _ in columns)
                quoted_columns = ",".join(f'"{column}"' for column in columns)
                insert_sql = f'insert or replace into "{table}" ({quoted_columns}) values ({placeholders})'
                rows = src.execute(f'select {quoted_columns} from "{table}"').fetchall()
                if rows:
                    dst.executemany(insert_sql, rows)
            except Exception as exc:
                log_line(f"Skipped copying table {table}: {exc}")
        dst.commit()
    finally:
        src.close()
        dst.close()


def schema_has_required_threads_shape(db_path: Path) -> bool:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        table_names = {str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")}
        if "threads" not in table_names:
            return False
        columns = {str(row[1]) for row in con.execute("pragma table_info(threads)")}
        required = {"id", "model_provider", "cwd", "title", "updated_at"}
        return required.issubset(columns)
    finally:
        con.close()


def create_rebuilt_state_db(codex_home: Path, out_db: Path, provider_mode: str = "current") -> tuple[int, str]:
    old_db = codex_home / "state_5.sqlite"
    threads, current_provider = collect_repair_threads(codex_home, provider_mode)
    has_old_db = old_db.exists()
    if has_old_db:
        try:
            tables, indexes, triggers, table_names = read_sqlite_schema(old_db)
            if not schema_has_required_threads_shape(old_db):
                raise RuntimeError("state_5.sqlite schema is readable but missing a usable threads table")
        except Exception as exc:
            log_line(f"Could not read existing state DB schema from {old_db}; falling back to built-in schema: {exc}")
            tables, indexes, triggers, table_names = fallback_state_schema()
            has_old_db = False
    else:
        tables, indexes, triggers, table_names = fallback_state_schema()
        log_line(f"Missing Codex state DB; rebuilding UI index from local session sources: {old_db}")
    if out_db.exists():
        out_db.unlink()
    out_db.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(out_db), timeout=10)
    try:
        con.execute("pragma foreign_keys=off")
        for sql in tables:
            con.execute(sql)
        con.commit()
    finally:
        con.close()

    if has_old_db:
        copy_table_rows(old_db, out_db, table_names)

    con = sqlite3.connect(str(out_db), timeout=10)
    try:
        con.execute("pragma foreign_keys=off")
        thread_columns = [row[1] for row in con.execute("pragma table_info(threads)")]
        thread_keys = set().union(*(thread.keys() for thread in threads)) if threads else set()
        insert_columns = [column for column in thread_columns if column in thread_keys]
        if insert_columns:
            placeholders = ",".join("?" for _ in insert_columns)
            quoted_columns = ",".join(f'"{column}"' for column in insert_columns)
            insert_sql = f'insert or replace into threads ({quoted_columns}) values ({placeholders})'
            values = [[thread.get(column) for column in insert_columns] for thread in threads]
            con.executemany(insert_sql, values)
        for sql in indexes:
            con.execute(sql)
        for sql in triggers:
            con.execute(sql)
        con.commit()
        check = con.execute("pragma quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Rebuilt DB failed quick_check: {check}")
    finally:
        con.close()

    return len(threads), current_provider


def write_rebuilt_session_index(codex_home: Path, threads: list[dict[str, Any]]) -> None:
    target = codex_home / "session_index.jsonl"
    atomic_write_text(target, rebuilt_session_index_text(threads))


def rebuilt_session_index_text(threads: list[dict[str, Any]]) -> str:
    lines = []
    for thread in sorted(threads, key=lambda item: item["updated_at_ms"]):
        updated = parse_datetime(thread.get("updated_at_iso"))
        updated_text = (
            updated.isoformat().replace("+00:00", "Z")
            if updated is not None
            else dt.datetime.fromtimestamp(thread["updated_at"], tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )
        lines.append(
            json.dumps(
                {"id": thread["id"], "thread_name": thread["title"], "updated_at": updated_text},
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(content, encoding=encoding)
    os.replace(temp_path, path)


def updated_workspace_roots_text(state_path: Path, threads: list[dict[str, Any]]) -> str | None:
    if not state_path.exists():
        return None
    data = json.loads(state_path.read_text(encoding="utf-8"))
    roots = list(data.get("electron-saved-workspace-roots") or [])
    order = list(data.get("project-order") or roots)
    seen = {str(Path(root)).casefold() for root in roots}

    for thread in threads:
        cwd = str(thread.get("cwd") or "")
        if cwd.startswith("\\\\?\\"):
            cwd = cwd[4:]
        path = Path(cwd)
        if not path.exists() or not path.is_dir():
            continue
        text = str(path)
        lowered = text.casefold()
        if lowered in seen:
            continue
        if text.lower().endswith(r"\windows\system32") or text.lower() == str(Path.home()).lower():
            continue
        roots.append(text)
        order.append(text)
        seen.add(lowered)

    data["electron-saved-workspace-roots"] = roots
    data["project-order"] = order
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def update_saved_workspace_roots(codex_home: Path, threads: list[dict[str, Any]]) -> None:
    state_path = codex_home / ".codex-global-state.json"
    content = updated_workspace_roots_text(state_path, threads)
    if content is not None:
        atomic_write_text(state_path, content)


def windows_process_ids_by_names(names: set[str]) -> list[str]:
    import ctypes
    from ctypes import wintypes

    normalized = {name.casefold() for name in names}
    snapshot_flag = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_flag, 0)
    if snapshot == invalid_handle:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

    pids: list[str] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return pids
        while True:
            exe_name = entry.szExeFile.casefold()
            stem_name = exe_name[:-4] if exe_name.endswith(".exe") else exe_name
            if exe_name in normalized or stem_name in normalized:
                pids.append(str(entry.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def codex_processes_running() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    try:
        return windows_process_ids_by_names({"codex", "codex.exe"})
    except Exception:
        pass
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Process Codex,codex -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
        text=True,
        capture_output=True,
        check=False,
    )
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return ids


def backup_codex_state_files(codex_home: Path) -> Path:
    backup_dir = codex_home / "history_sync_backups" / unique_run_name("ui-index-repair")
    backup_dir.mkdir(parents=True, exist_ok=True)
    names = [
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
        "session_index.jsonl",
        ".codex-global-state.json",
    ]
    for name in names:
        source = codex_home / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)
    return backup_dir


def normalize_session_jsonl_provider(codex_home: Path, provider: str, backup_dir: Path) -> int:
    changed = 0
    sessions_backup = backup_dir / "session_jsonl_originals"
    for path in session_files(codex_home):
        rows: list[str] = []
        file_changed = False
        saw_session_meta = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.rstrip("\n")
                    if not raw.strip():
                        rows.append(line)
                        continue
                    try:
                        row = json.loads(raw.lstrip("\ufeff"))
                    except json.JSONDecodeError:
                        rows.append(line)
                        continue
                    payload = row.get("payload")
                    if row.get("type") == "session_meta" and isinstance(payload, dict):
                        saw_session_meta = True
                        if payload.get("model_provider") != provider:
                            payload["model_provider"] = provider
                            file_changed = True
                    rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except FileNotFoundError:
            continue

        if not saw_session_meta:
            session_id = extract_id_from_name(path)
            meta_row = {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "model_provider": provider,
                },
            }
            rows.insert(0, json.dumps(meta_row, ensure_ascii=False, separators=(",", ":")) + "\n")
            file_changed = True

        if file_changed:
            relative = path.relative_to(codex_home)
            backup_path = sessions_backup / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text("".join(rows), encoding="utf-8")
            os.replace(temp_path, path)
            changed += 1
    return changed


def repair_ui_index(config: dict[str, Any], apply: bool = False, provider_mode: str = "current") -> RepairResult:
    codex_home = Path(config["codex_home"]).expanduser().resolve()
    if not codex_home.exists():
        raise FileNotFoundError(f"Codex home does not exist: {codex_home}")
    if apply:
        running = codex_processes_running()
        if running:
            raise RuntimeError(
                "Codex 仍在运行，不能替换 UI 索引。请完全退出 Codex 后，在外部 PowerShell 里重新运行修复命令。"
            )
    backup_dir = backup_codex_state_files(codex_home)
    provider_for_jsonl = None
    if apply and provider_mode != "preserve":
        _, provider_for_jsonl = collect_repair_threads(codex_home, provider_mode)
    rebuilt_db = backup_dir / "state_5.sqlite.rebuilt"
    count, current_provider = create_rebuilt_state_db(codex_home, rebuilt_db, provider_mode)
    threads, _ = collect_repair_threads(codex_home, provider_mode)

    if apply:
        session_index_path = codex_home / "session_index.jsonl"
        global_state_path = codex_home / ".codex-global-state.json"
        rebuilt_session_index = backup_dir / "session_index.rebuilt.jsonl"
        rebuilt_session_index.write_text(rebuilt_session_index_text(threads), encoding="utf-8")
        rebuilt_global_state = None
        if global_state_path.exists():
            rebuilt_global_state = backup_dir / ".codex-global-state.rebuilt.json"
            rebuilt_global_state.write_text(updated_workspace_roots_text(global_state_path, threads) or "{}", encoding="utf-8")

        try:
            if provider_for_jsonl is not None:
                changed = normalize_session_jsonl_provider(codex_home, provider_for_jsonl, backup_dir)
                log_line(f"Normalized model_provider={provider_for_jsonl} in {changed} session JSONL file(s)")
            for name in ("state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm"):
                target = codex_home / name
                if target.exists():
                    target.unlink()
            shutil.copy2(rebuilt_db, codex_home / "state_5.sqlite")
            shutil.copy2(rebuilt_session_index, session_index_path)
            if rebuilt_global_state is not None:
                shutil.copy2(rebuilt_global_state, global_state_path)
        except Exception:
            for name in ("state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm"):
                backup_state = backup_dir / name
                live_state = codex_home / name
                if backup_state.exists():
                    shutil.copy2(backup_state, live_state)
                elif live_state.exists() and name != "state_5.sqlite":
                    live_state.unlink()
            backup_index = backup_dir / "session_index.jsonl"
            if backup_index.exists():
                shutil.copy2(backup_index, session_index_path)
            backup_global = backup_dir / ".codex-global-state.json"
            if backup_global.exists():
                shutil.copy2(backup_global, global_state_path)
            sessions_backup = backup_dir / "session_jsonl_originals"
            if sessions_backup.exists():
                for backup_path in sessions_backup.rglob("*.jsonl"):
                    relative = backup_path.relative_to(sessions_backup)
                    restore_path = codex_home / relative
                    restore_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, restore_path)
            raise
        log_line(f"Applied UI index repair with {count} threads; backup={backup_dir}")
    else:
        log_line(f"Created UI index repair preview with {count} threads: {rebuilt_db}")
    return RepairResult(rebuilt_db, backup_dir, count, current_provider, apply)


def needs_ui_index_repair(codex_home: Path, provider_mode: str = "current") -> tuple[bool, str]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return True, "state_5.sqlite is missing"
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        check = con.execute("pragma quick_check").fetchone()[0]
        if check != "ok":
            con.close()
            return True, "state_5.sqlite quick_check failed"
        db_rows = {row[0]: row[1] for row in con.execute("select id, model_provider from threads")}
        con.close()
    except Exception as exc:
        return True, f"state_5.sqlite could not be read: {exc}"

    threads, current_provider = collect_repair_threads(codex_home, provider_mode)
    session_ids = {thread["id"] for thread in threads}
    missing = session_ids - set(db_rows)
    if missing:
        return True, f"{len(missing)} local session(s) missing from UI index"
    if provider_mode != "preserve":
        jsonl_mismatches = 0
        for path in session_files(codex_home):
            provider = None
            saw_session_meta = False
            for row in read_jsonl(path):
                payload = row.get("payload") or {}
                if row.get("type") == "session_meta" and isinstance(payload, dict):
                    saw_session_meta = True
                    provider = payload.get("model_provider")
                    break
            if saw_session_meta and provider != current_provider:
                jsonl_mismatches += 1
        if jsonl_mismatches:
            return True, f"{jsonl_mismatches} session JSONL file(s) use a provider different from {current_provider}"
        mismatched = [thread_id for thread_id in session_ids if db_rows.get(thread_id) != current_provider]
        if mismatched:
            return True, f"{len(mismatched)} session(s) use a provider different from {current_provider}"
    return False, "UI index is already healthy"


def ensure_ui_index_for_launch(config: dict[str, Any]) -> str:
    codex_home = Path(config["codex_home"]).expanduser().resolve()
    running = codex_processes_running()
    if running:
        return "Codex is already running; skipped UI index repair"
    needs_repair, reason = needs_ui_index_repair(codex_home)
    if not needs_repair:
        return reason
    result = repair_ui_index(config, apply=True)
    return f"Rebuilt UI index for {result.session_count} sessions because {reason}"


class KeeperApp(tk.Tk if tk else object):
    def __init__(self, start_message: str | None = None) -> None:
        if tk is None or ttk is None:
            raise RuntimeError("Tkinter is not available in this Python installation.")
        super().__init__()
        self.config_data = load_config()
        self.result_records: list[dict[str, Any]] = []
        self.repair_running = False
        self.title(APP_NAME)
        self.geometry("980x660")
        self.minsize(820, 560)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_styles()
        self._build_layout()
        self.refresh_state()
        if start_message:
            self.after(250, lambda: messagebox.showinfo(APP_NAME, start_message))

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", padding=(10, 6))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))

    def _build_layout(self) -> None:
        header = ttk.Frame(self, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Codex 历史备份", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.state_label = ttk.Label(header, text="")
        self.state_label.grid(row=0, column=1, sticky="e")

        settings = ttk.Frame(self, padding=(18, 4, 18, 8))
        settings.grid(row=1, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Codex home").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.codex_home_var = tk.StringVar(value=self.config_data["codex_home"])
        ttk.Entry(settings, textvariable=self.codex_home_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="选择", command=self.choose_codex_home).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="备份目录").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.vault_root_var = tk.StringVar(value=self.config_data["vault_root"])
        ttk.Entry(settings, textvariable=self.vault_root_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="选择", command=self.choose_vault_root).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="Codex 程序").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.codex_exe_var = tk.StringVar(value=self.config_data.get("codex_exe", ""))
        ttk.Entry(settings, textvariable=self.codex_exe_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="选择", command=self.choose_codex_exe).grid(row=2, column=2, padx=(8, 0), pady=4)

        body = ttk.Frame(self, padding=(18, 4, 18, 10))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)

        buttons = ttk.Frame(body)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for col in range(7):
            buttons.columnconfigure(col, weight=1)

        ttk.Button(buttons, text="立即刷新", command=self.sync_now).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(buttons, text="启动 Codex", command=self.launch_now).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(buttons, text="创建桌面启动器", command=self.create_launcher).grid(row=0, column=2, sticky="ew", padx=6)
        ttk.Button(buttons, text="安装开机同步", command=self.install_startup).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Button(buttons, text="移除开机同步", command=self.remove_startup).grid(row=0, column=4, sticky="ew", padx=6)
        ttk.Button(buttons, text="查看备份", command=self.open_latest).grid(row=0, column=5, sticky="ew", padx=6)
        ttk.Button(buttons, text="复用提示", command=self.open_prompt).grid(row=0, column=6, sticky="ew", padx=(6, 0))

        repair_buttons = ttk.Frame(body)
        repair_buttons.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        repair_buttons.columnconfigure(0, weight=1)
        repair_buttons.columnconfigure(1, weight=1)
        repair_buttons.columnconfigure(2, weight=2)
        ttk.Button(repair_buttons, text="检查 UI 索引", command=self.preview_repair).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(repair_buttons, text="修复 UI 索引", command=self.apply_repair).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(
            repair_buttons,
            text="修复前请完全退出 Codex；当前窗口里会提示你到外部 PowerShell 执行。",
        ).grid(row=0, column=2, sticky="w", padx=(6, 0))

        search_frame = ttk.Frame(body)
        search_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="搜索").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(search_frame, textvariable=self.search_var)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _event: self.search())
        ttk.Button(search_frame, text="查找", command=self.search).grid(row=0, column=2, padx=(8, 0))

        table_frame = ttk.Frame(body)
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("time", "role", "title", "snippet")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=14)
        self.tree.heading("time", text="时间")
        self.tree.heading("role", text="角色")
        self.tree.heading("title", text="标题")
        self.tree.heading("snippet", text="片段")
        self.tree.column("time", width=165, anchor="w")
        self.tree.column("role", width=88, anchor="w")
        self.tree.column("title", width=240, anchor="w")
        self.tree.column("snippet", width=420, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self.open_selected_record)

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        log_frame = ttk.Frame(self, padding=(18, 0, 18, 14))
        log_frame.grid(row=3, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)
        self.log_var = tk.StringVar(value="")
        ttk.Label(log_frame, textvariable=self.log_var).grid(row=0, column=0, sticky="ew")

    def save_from_fields(self) -> None:
        self.config_data["codex_home"] = self.codex_home_var.get().strip()
        self.config_data["vault_root"] = self.vault_root_var.get().strip()
        self.config_data["codex_exe"] = self.codex_exe_var.get().strip()
        save_config(self.config_data)

    def refresh_state(self) -> None:
        vault = latest_vault(Path(self.vault_root_var.get()).expanduser())
        if vault:
            self.state_label.configure(text=f"最新：{vault.name}")
        else:
            self.state_label.configure(text="尚未生成对话备份")

    def set_log(self, text: str) -> None:
        self.log_var.set(text)
        self.refresh_state()

    def choose_codex_home(self) -> None:
        path = filedialog.askdirectory(initialdir=self.codex_home_var.get() or str(Path.home()))
        if path:
            self.codex_home_var.set(path)
            self.save_from_fields()

    def choose_vault_root(self) -> None:
        path = filedialog.askdirectory(initialdir=self.vault_root_var.get() or str(DEFAULT_VAULT_ROOT))
        if path:
            self.vault_root_var.set(path)
            self.save_from_fields()
            self.refresh_state()

    def choose_codex_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Codex.exe",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.codex_exe_var.set(path)
            self.save_from_fields()

    def sync_now(self) -> None:
        self.save_from_fields()
        self.set_log("正在备份本地历史...")

        def worker() -> None:
            try:
                result = sync_vault(self.config_data)
                self.after(
                    0,
                    lambda: self.set_log(f"已备份 {result.session_count} 个会话：{result.out_dir}"),
                )
            except Exception as exc:
                log_line(traceback.format_exc())
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_NAME, message))
                self.after(0, lambda: self.set_log("备份失败"))

        threading.Thread(target=worker, daemon=True).start()

    def launch_now(self) -> None:
        self.save_from_fields()
        self.set_log("正在备份历史并启动 Codex...")

        def worker() -> None:
            try:
                repair_note = ensure_ui_index_for_launch(self.config_data)
                result = sync_vault(self.config_data)
                launch_codex(self.config_data)
                self.after(0, lambda: self.set_log(f"{repair_note}；已备份 {result.session_count} 个会话，并已启动 Codex"))
            except Exception as exc:
                log_line(traceback.format_exc())
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_NAME, message))
                self.after(0, lambda: self.set_log("启动失败"))

        threading.Thread(target=worker, daemon=True).start()

    def create_launcher(self) -> None:
        try:
            self.save_from_fields()
            path = install_desktop_launcher()
            self.set_log(f"已创建桌面启动器：{path}")
        except Exception as exc:
            log_line(traceback.format_exc())
            messagebox.showerror(APP_NAME, str(exc))

    def install_startup(self) -> None:
        try:
            self.save_from_fields()
            path = install_startup_sync()
            self.set_log(f"已安装开机同步：{path}")
        except Exception as exc:
            log_line(traceback.format_exc())
            messagebox.showerror(APP_NAME, str(exc))

    def remove_startup(self) -> None:
        try:
            removed = remove_startup_sync()
            self.set_log("已移除开机同步" if removed else "没有找到开机同步快捷方式")
        except Exception as exc:
            log_line(traceback.format_exc())
            messagebox.showerror(APP_NAME, str(exc))

    def open_latest(self) -> None:
        vault = latest_vault(Path(self.vault_root_var.get()).expanduser())
        if vault:
            open_path(vault)
        else:
            open_path(Path(self.vault_root_var.get()).expanduser())

    def open_prompt(self) -> None:
        vault_root = Path(self.vault_root_var.get()).expanduser()
        vault = latest_vault(vault_root)
        if not vault:
            messagebox.showinfo(APP_NAME, "请先备份一次历史对话。")
            return
        open_path(write_reuse_prompt(vault_root, vault))

    def preview_repair(self) -> None:
        self.save_from_fields()
        self.set_log("正在检查并生成 UI 索引预览...")

        def worker() -> None:
            try:
                result = repair_ui_index(self.config_data, apply=False)
                self.after(
                    0,
                    lambda: self.set_log(
                        f"预览通过：可重建 {result.session_count} 个会话，provider={result.current_provider}，文件={result.rebuilt_db}"
                    ),
                )
            except Exception as exc:
                log_line(traceback.format_exc())
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_NAME, message))
                self.after(0, lambda: self.set_log("UI 索引检查失败"))

        threading.Thread(target=worker, daemon=True).start()

    def apply_repair(self) -> None:
        if self.repair_running:
            self.set_log("UI 索引修复正在后台进行...")
            return
        self.save_from_fields()
        command = f'"{sys.executable}" "{SCRIPT_PATH}" --repair-ui-index --apply-repair'
        self.repair_running = True
        self.set_log("正在后台修复 UI 索引...")

        def worker() -> None:
            try:
                running = codex_processes_running()
                if running:
                    self.after(
                        0,
                        lambda: messagebox.showinfo(
                            APP_NAME,
                            "当前 Codex 仍在运行，不能在这里直接替换索引。\n\n"
                            "请先完全退出 Codex，然后打开 PowerShell 运行：\n\n"
                            f"{command}",
                        ),
                    )
                    self.after(0, lambda: self.set_log("等待你退出 Codex 后在外部 PowerShell 执行修复命令"))
                    return
                result = repair_ui_index(self.config_data, apply=True)
                self.after(0, lambda: self.set_log(f"已修复 UI 索引：{result.session_count} 个会话，备份在 {result.backup_dir}"))
            except Exception as exc:
                log_line(traceback.format_exc())
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_NAME, message))
            finally:
                self.repair_running = False

        threading.Thread(target=worker, daemon=True).start()

    def search(self) -> None:
        vault_root = Path(self.vault_root_var.get()).expanduser()
        query = self.search_var.get().strip()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.result_records = search_records(vault_root, query)
        for index, record in enumerate(self.result_records):
            text = re.sub(r"\s+", " ", record.get("text") or "").strip()
            snippet = text[:180] + ("..." if len(text) > 180 else "")
            role = "用户" if record.get("role") == "user" else "助手"
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(record.get("timestamp") or "", role, record.get("title") or "", snippet),
            )
        self.set_log(f"找到 {len(self.result_records)} 条结果")

    def open_selected_record(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        record = self.result_records[int(selected[0])]
        markdown = Path(record.get("markdown") or "")
        if markdown.exists():
            open_path(markdown)


def cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export and reuse local Codex history safely.")
    parser.add_argument("--sync", action="store_true", help="Refresh the local history vault and exit.")
    parser.add_argument("--launch", action="store_true", help="Refresh the vault, then launch Codex.")
    parser.add_argument("--quiet", action="store_true", help="Suppress GUI fallback for background runs.")
    parser.add_argument("--install-launcher", action="store_true", help="Create a desktop launcher shortcut.")
    parser.add_argument("--install-startup", action="store_true", help="Create a Windows startup sync shortcut.")
    parser.add_argument("--remove-startup", action="store_true", help="Remove the Windows startup sync shortcut.")
    parser.add_argument("--repair-ui-index", action="store_true", help="Rebuild the Codex UI thread index from local JSONL sessions.")
    parser.add_argument("--apply-repair", action="store_true", help="Replace state_5.sqlite with the rebuilt UI index. Codex must be closed.")
    parser.add_argument(
        "--provider-mode",
        default="current",
        help="Provider metadata for rebuilt threads: current, preserve, or an explicit provider name.",
    )
    parser.add_argument("--codex-home", default=None, help="Override Codex home directory.")
    parser.add_argument("--vault-root", default=None, help="Override vault root directory.")
    parser.add_argument("--max-sessions", type=int, default=None, help="Limit exported sessions for testing.")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    config = load_config()
    if args.codex_home:
        config["codex_home"] = args.codex_home
    if args.vault_root:
        config["vault_root"] = args.vault_root
    save_config(config)

    if args.install_launcher:
        path = install_desktop_launcher()
        print(path)
        return 0
    if args.install_startup:
        path = install_startup_sync()
        print(path)
        return 0
    if args.remove_startup:
        print("removed" if remove_startup_sync() else "not installed")
        return 0
    if args.repair_ui_index:
        result = repair_ui_index(config, apply=args.apply_repair, provider_mode=args.provider_mode)
        action = "Applied" if result.applied else "Previewed"
        print(f"{action} UI index repair for {result.session_count} sessions")
        print(f"provider: {result.current_provider}")
        print(f"rebuilt_db: {result.rebuilt_db}")
        print(f"backup: {result.backup_dir}")
        return 0
    if args.sync or args.launch:
        if args.launch:
            print(ensure_ui_index_for_launch(config))
        result = sync_vault(config, max_sessions=args.max_sessions)
        print(f"Synced {result.session_count} sessions to {result.out_dir}")
        if args.launch:
            launch_codex(config)
            print("Codex launched")
        return 0
    return 2


def main() -> int:
    parser = cli_parser()
    args = parser.parse_args()
    has_cli_action = any(
        [
            args.sync,
            args.launch,
            args.install_launcher,
            args.install_startup,
            args.remove_startup,
            args.repair_ui_index,
        ]
    )
    if has_cli_action:
        try:
            return run_cli(args)
        except Exception as exc:
            log_line(traceback.format_exc())
            if args.quiet:
                return 1
            if args.launch and tk is not None:
                KeeperApp(start_message=str(exc)).mainloop()
                return 1
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if tk is None:
        print(
            "error: Tkinter is not available. On Windows, start the tool with "
            "`open_codex_history_repair_gui.cmd` to auto-detect or install a Python build that includes tkinter.",
            file=sys.stderr,
        )
        return 1
    KeeperApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
