"""autoclaude — pull projects from the Notion "Claude Projects" database and run Claude Code on them.

Workflow:
  1. Query the Notion database for pages whose title does NOT start with a status marker
     (pending = no marker; done = "✅"; in progress = "🔄"; failed = "❌").
  2. Pick the highest-priority pending page, read its content — that's the prompt.
  3. Mark the title "🔄", run `claude -p` in a per-project workspace folder.
  4. On success mark "✅", on failure "❌", and post the result tail as a Notion comment.
  5. In --watch mode, repeat. If Claude reports the usage limit was hit, sleep until
     the reset time it reports, then continue.

Zero dependencies — Python 3.10+ stdlib only. Auth via NOTION_TOKEN (env or .env file).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ----------------------------- configuration ---------------------------------

DEFAULTS = {
    "NOTION_DATABASE_ID": "10aefa04a0d14dc4add853782342b841",  # "Claude Projects" DB
    "AUTOCLAUDE_MODEL": "sonnet",            # opus | sonnet | haiku | full model id
    "AUTOCLAUDE_THINKING": "default",        # default | off | low | medium | high
    "AUTOCLAUDE_PERMISSION_MODE": "bypassPermissions",
    "AUTOCLAUDE_TIMEOUT_MIN": "120",         # max minutes per project run
    "AUTOCLAUDE_POLL_MIN": "30",             # watch mode: minutes between queue checks when empty
    "AUTOCLAUDE_WORKSPACES": str(SCRIPT_DIR / "workspaces"),
}

# MAX_THINKING_TOKENS values per thinking level ("default" leaves Claude Code's default)
THINKING_TOKENS = {"off": "0", "low": "4000", "medium": "12000", "high": "31999"}

MARK_DONE, MARK_RUNNING, MARK_FAILED = "✅", "\U0001f504", "❌"  # ✅ 🔄 ❌
ALL_MARKS = (MARK_DONE, MARK_RUNNING, MARK_FAILED)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def load_env() -> None:
    """Read KEY=VALUE lines from a .env next to this script into os.environ (no overwrite)."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cfg(key: str) -> str:
    return os.environ.get(key, DEFAULTS.get(key, ""))


# ------------------------------- Notion API ----------------------------------

def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit(
            "NOTION_TOKEN is not set.\n"
            "Create an internal integration at https://www.notion.so/profile/integrations,\n"
            "share the 'Claude Projects' database with it (page ••• menu > Connections),\n"
            f"then put NOTION_TOKEN=ntn_... in {SCRIPT_DIR / '.env'}"
        )
    req = urllib.request.Request(
        f"{NOTION_API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Notion API {method} {path} -> {e.code}: {detail}") from e


def rich_text_to_plain(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich)


def fetch_pending_projects() -> list[dict]:
    """All DB pages without a status marker, sorted by Priority (blank last) then created time."""
    pages, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{cfg('NOTION_DATABASE_ID')}/query", body)
        pages.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not data.get("has_more"):
            break

    projects = []
    for page in pages:
        title_prop = next(
            (p for p in page["properties"].values() if p["type"] == "title"), None
        )
        title = rich_text_to_plain(title_prop["title"]) if title_prop else ""
        if not title or any(m in title for m in ALL_MARKS):
            continue
        model = (page["properties"].get("Model", {}).get("select") or {}).get("name")
        thinking = (page["properties"].get("Thinking", {}).get("select") or {}).get("name")
        priority = page["properties"].get("Priority", {}).get("number")
        projects.append(
            {
                "id": page["id"],
                "title": title.strip(),
                "model": None if model in (None, "default") else model,
                "thinking": None if thinking in (None, "default") else thinking,
                "priority": priority,
                "created": page.get("created_time", ""),
            }
        )
    projects.sort(key=lambda p: (p["priority"] is None, p["priority"] or 0, p["created"]))
    return projects


def fetch_page_text(block_id: str, depth: int = 0) -> str:
    """Concatenate the plain text of a page's blocks (recursing into nested blocks)."""
    if depth > 3:
        return ""
    lines, cursor = [], None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = notion_request("GET", path)
        for block in data.get("results", []):
            btype = block["type"]
            payload = block.get(btype, {})
            text = rich_text_to_plain(payload.get("rich_text", []))
            prefix = {
                "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
                "bulleted_list_item": "- ", "numbered_list_item": "- ",
                "to_do": "- [ ] ", "quote": "> ",
            }.get(btype, "")
            if btype == "code":
                lines.append(f"```\n{text}\n```")
            elif text:
                lines.append(prefix + text)
            if block.get("has_children") and btype not in ("child_page", "child_database"):
                child = fetch_page_text(block["id"], depth + 1)
                if child:
                    lines.append(child)
        cursor = data.get("next_cursor")
        if not data.get("has_more"):
            break
    return "\n".join(lines)


def set_title(page_id: str, title: str) -> None:
    notion_request(
        "PATCH",
        f"/pages/{page_id}",
        {"properties": {"Name": {"title": [{"text": {"content": title}}]}}},
    )


def add_comment(page_id: str, text: str) -> None:
    try:
        notion_request(
            "POST",
            "/comments",
            {"parent": {"page_id": page_id}, "rich_text": [{"text": {"content": text[:1900]}}]},
        )
    except Exception as e:  # comments are best-effort; never fail the run over them
        print(f"  (could not post Notion comment: {e})")


# ------------------------------- Claude Code ---------------------------------

def find_claude() -> str:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # CLI bundled with the Claude desktop app (versioned folders)
    bundled = sorted(
        glob.glob(os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\claude.exe"))
    )
    if bundled:
        return bundled[-1]
    sys.exit(
        "Could not find the claude CLI. Install it (npm install -g @anthropic-ai/claude-code)\n"
        "or set CLAUDE_BIN in .env to the full path of claude.exe"
    )


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug[:60] or "project"


# Matches Claude Code's limit error, e.g. "Claude AI usage limit reached|1765500000"
LIMIT_RE = re.compile(r"usage limit reached\|?(\d{10,13})?", re.IGNORECASE)


def run_claude(project: dict, prompt: str, args: argparse.Namespace) -> tuple[str, str, int | None]:
    """Run claude on the prompt. Returns (status, output, limit_reset_epoch).

    status is one of "ok", "failed", "limit".
    """
    model = args.model or project["model"] or cfg("AUTOCLAUDE_MODEL")
    thinking = args.thinking or project["thinking"] or cfg("AUTOCLAUDE_THINKING")

    workspace = Path(cfg("AUTOCLAUDE_WORKSPACES")) / slugify(project["title"])
    workspace.mkdir(parents=True, exist_ok=True)
    log_dir = SCRIPT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slugify(project['title'])}.log"

    env = os.environ.copy()
    if thinking in THINKING_TOKENS:
        env["MAX_THINKING_TOKENS"] = THINKING_TOKENS[thinking]

    full_prompt = (
        f"You are working autonomously in the project folder {workspace} (your current "
        "directory). Build the project described below. Create all files here, keep the "
        "code runnable, and finish with a README.md explaining how to run it. When done, "
        "print a short summary of what you built.\n\n"
        f"# Project: {project['title']}\n\n{prompt}"
    )

    cmd = [
        find_claude(), "-p",
        "--model", model,
        "--permission-mode", args.permission_mode,
    ]
    print(f"  model={model} thinking={thinking} workspace={workspace}")
    print(f"  log: {log_file}")

    timeout = int(cfg("AUTOCLAUDE_TIMEOUT_MIN")) * 60
    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workspace,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log_file.write_text("TIMED OUT", encoding="utf-8")
        return "failed", f"Timed out after {timeout // 60} minutes.", None

    output = (proc.stdout or "") + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
    log_file.write_text(f"$ {' '.join(cmd)}\n\n{full_prompt}\n\n=== OUTPUT ===\n{output}",
                        encoding="utf-8")

    limit = LIMIT_RE.search(output)
    if limit:
        epoch = int(limit.group(1)) if limit.group(1) else None
        if epoch and epoch > 10**12:  # milliseconds
            epoch //= 1000
        return "limit", output, epoch
    if proc.returncode != 0:
        return "failed", output, None
    return "ok", output, None


# --------------------------------- commands ----------------------------------

def run_one(args: argparse.Namespace) -> str:
    """Run the next pending project. Returns "ok", "failed", "limit", or "empty"."""
    projects = fetch_pending_projects()
    if args.project:
        projects = [p for p in projects if args.project.lower() in p["title"].lower()]
    if not projects:
        print("No pending projects in Notion.")
        return "empty"

    project = projects[0]
    print(f"\n=== {project['title']} ===")
    prompt = fetch_page_text(project["id"]).strip()
    if not prompt:
        print("  Page is empty - marking failed.")
        set_title(project["id"], f"{MARK_FAILED} {project['title']}")
        add_comment(project["id"], "autoclaude: page has no content to use as a prompt.")
        return "failed"

    if args.dry_run:
        print(f"--- prompt ---\n{prompt}\n--- end (dry run, nothing executed) ---")
        return "ok"

    set_title(project["id"], f"{MARK_RUNNING} {project['title']}")
    started = time.time()
    try:
        status, output, reset_epoch = run_claude(project, prompt, args)
    except BaseException:
        set_title(project["id"], project["title"])  # un-mark so it can be retried
        raise

    minutes = (time.time() - started) / 60
    tail = output.strip()[-1500:]
    if status == "ok":
        set_title(project["id"], f"{MARK_DONE} {project['title']}")
        add_comment(project["id"], f"autoclaude: completed in {minutes:.0f} min.\n\n{tail}")
        print(f"  done in {minutes:.0f} min")
    elif status == "limit":
        set_title(project["id"], project["title"])  # back to pending, retry after reset
        when = (
            dt.datetime.fromtimestamp(reset_epoch).strftime("%H:%M") if reset_epoch else "unknown"
        )
        print(f"  usage limit hit; resets at {when}")
        args._reset_epoch = reset_epoch
    else:
        set_title(project["id"], f"{MARK_FAILED} {project['title']}")
        add_comment(project["id"], f"autoclaude: FAILED after {minutes:.0f} min.\n\n{tail}")
        print(f"  FAILED after {minutes:.0f} min (see log)")
    return status


def watch(args: argparse.Namespace) -> None:
    poll = int(cfg("AUTOCLAUDE_POLL_MIN")) * 60
    runs = 0
    while True:
        status = run_one(args)
        if status in ("ok", "failed"):
            runs += 1
            if args.max_runs and runs >= args.max_runs:
                print(f"Reached --max-runs {args.max_runs}, stopping.")
                return
            continue
        if status == "limit":
            reset = getattr(args, "_reset_epoch", None)
            sleep_s = max(120, min((reset - time.time()) + 120 if reset else 3600, 6 * 3600))
            print(f"Sleeping {sleep_s / 60:.0f} min until tokens reset...")
            time.sleep(sleep_s)
            continue
        # empty queue
        print(f"Checking again in {poll // 60} min (Ctrl+C to stop).")
        time.sleep(poll)


def list_projects() -> None:
    projects = fetch_pending_projects()
    if not projects:
        print("No pending projects.")
        return
    print(f"{len(projects)} pending project(s), in run order:")
    for i, p in enumerate(projects, 1):
        extras = ", ".join(
            f"{k}={v}" for k, v in
            [("model", p["model"]), ("thinking", p["thinking"]), ("priority", p["priority"])]
            if v is not None
        )
        print(f"  {i}. {p['title']}" + (f"  ({extras})" if extras else ""))


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(
        description="Run Claude Code on projects queued in the Notion 'Claude Projects' database."
    )
    parser.add_argument("--model", help="opus | sonnet | haiku | full model id "
                        "(overrides Notion + default)")
    parser.add_argument("--thinking", choices=["off", "low", "medium", "high"],
                        help="thinking level (overrides Notion + default)")
    parser.add_argument("--permission-mode", default=cfg("AUTOCLAUDE_PERMISSION_MODE"),
                        help="Claude Code permission mode (default: %(default)s)")
    parser.add_argument("--project", help="run the project whose title contains this text")
    parser.add_argument("--list", action="store_true", help="list pending projects and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the prompt that would run, change nothing")
    parser.add_argument("--watch", action="store_true",
                        help="keep running projects; sleep through token resets")
    parser.add_argument("--max-runs", type=int, default=0,
                        help="in --watch mode, stop after N completed/failed runs")
    args = parser.parse_args()

    if args.list:
        list_projects()
    elif args.watch:
        try:
            watch(args)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_one(args)


if __name__ == "__main__":
    main()
