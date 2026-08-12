import os
import time
import json
import base64
import argparse
import requests
import re
import shutil
from difflib import SequenceMatcher
from datetime import datetime
import random

parser = argparse.ArgumentParser(description="Self-chat story generator")
parser.add_argument(
    "--config",
    default="",
    help="Path to a custom JSON task list to use instead of the default "
    "(~/local-ai-files/tasks.json). The file may be a plain task list or an "
    "object with 'tasks' plus optional 'genre_checklists'. Each task may also "
    "carry its own 'checklist' (editor/moderator) that wins over the genre "
    "checklist. Combine with --defaults to also include the default tasks.",
)
parser.add_argument(
    "--defaults",
    action="store_true",
    help="Also load the default tasks (~/local-ai-files/tasks.json) in addition "
    "to the ones from --config.",
)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Validate the task config and environment and print the full plan "
    "for each task (genre/checklist resolution, script enforcement, medium "
    "feasibility) without making any LLM call, then exit.",
)
args = parser.parse_args()
STORY_BASE_DIR = os.path.expanduser("~/local-ai-files/stories")

BASE_URL = "http://localhost:3001"
USERNAME_A = "kolpo"
USERNAME_B = "kaya"
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 10.0
SLEEP_BETWEEN_TURNS = 5.0
MAX_MESSAGES_PER_AGENT = 15
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
SELF_CHAT_PROMPT_FILE = "/home/palash/local-ai-files/self_chat.txt"
STARTING_CONVERSATION = open(SELF_CHAT_PROMPT_FILE).read()

SLEEP_BETWEEN_ROUNDS = 900

USERNAME_EDITOR = "editor"
USERNAME_MODERATOR = "moderator"
EDITOR_PROMPT_FILE = "/home/palash/local-ai-files/context/editor.txt"
MODERATOR_PROMPT_FILE = "/home/palash/local-ai-files/context/moderator.txt"

DEFAULT_TASKS_FILE = os.path.expanduser("~/local-ai-files/tasks.json")


GENRE_CHECKLISTS_FILE = os.path.expanduser("~/local-ai-files/genre_checklists.json")


def load_genre_checklists(extra=None):
    """Base checklists from genre_checklists.json, overridden per genre by any
    'genre_checklists' carried in a task config file."""
    checklists = {}
    try:
        with open(GENRE_CHECKLISTS_FILE, encoding="utf-8") as f:
            checklists = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[checklist] Could not load {GENRE_CHECKLISTS_FILE}: {e} — using empty checklists")
    if extra:
        checklists.update(extra)
    return checklists


def _parse_tasks(items):
    tasks = []
    for item in items:
        task = (item.get("task") or "").strip()
        if not task:
            continue
        languages = item.get("languages") or ["English"]
        if isinstance(languages, str):
            languages = [l.strip() for l in languages.split(",")]
        mediums = item.get("mediums") or ["image", "text"]
        if isinstance(mediums, str):
            mediums = [m.strip() for m in mediums.split(",")]
        roles = item.get("roles") or ["free"]
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",")]
        genre = (item.get("genre") or "").strip() or "General"
        details = (item.get("details") or "").strip() if type(item) == 'str' else str(item.get("details"))
        checklist = item.get("checklist") or {}
        path = (item.get("path") or "").strip() or None
        inactive = item.get("inactive") or False
        tasks.append(
            {
                "task": task,
                "genre": genre,
                "languages": languages,
                "mediums": mediums,
                "roles": roles,
                "details": details,
                "checklist": checklist,
                "path": path,
                "inactive": inactive
            }
        )
    return tasks


def load_config_file(tasks_file):
    """Load a task config file. Accepts either a plain task list or an object
    with 'tasks' plus optional 'genre_checklists' (genre -> {editor, moderator}).
    Returns (tasks, genre_checklists)."""
    if not os.path.isfile(tasks_file):
        print(f"Tasks file not found: {tasks_file}")
        return [], {}
    with open(tasks_file, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(
                f"[config] Invalid JSON in {tasks_file}: {e.msg} at line "
                f"{e.lineno}, column {e.colno} (char {e.pos}). Check for "
                "trailing commas after the last property/array item."
            )
            raise SystemExit(1) from e
    if isinstance(data, dict):
        return _parse_tasks(data.get("tasks") or []), data.get("genre_checklists") or {}
    return _parse_tasks(data), {}


def load_tasks():
    """Combine tasks from --config and/or the default file. No role tag => free.
    Genre checklists from the config file override the default file's."""
    checklists = {}
    if args.config:
        tasks, cfg_checklists = load_config_file(args.config)
        source = args.config
        checklists.update(cfg_checklists)
        if args.defaults:
            defaults, def_checklists = load_config_file(DEFAULT_TASKS_FILE)
            existing = {t["task"] for t in tasks}
            tasks.extend(t for t in defaults if t["task"] not in existing)
            checklists.update(def_checklists)
            source = f"{args.config} + defaults"
    else:
        tasks, def_checklists = load_config_file(DEFAULT_TASKS_FILE)
        checklists.update(def_checklists)
        source = DEFAULT_TASKS_FILE
    return tasks, source, checklists


def checklist_for(genre, role, task_checklist=None):
    """role is 'editor' or 'moderator'. A task's own checklist wins, then the
    genre's entry, then the 'default' entry, then nothing."""
    items = (task_checklist or {}).get(role)
    if not items:
        entry = GENRE_CHECKLISTS.get(genre) or {}
        items = entry.get(role)
    if not items:
        items = GENRE_CHECKLISTS.get("default", {}).get(role) or []
    if not items:
        return "- No genre-specific checks defined for this genre."
    return "\n".join(f"- {item}" for item in items)


# Unicode block ranges used to sanity-check that a story is actually written in
# the language it was assigned, without needing an LLM call to find out.
_SCRIPT_RANGES = {
    "bengali": (0x0980, 0x09FF),
    "hindi": (0x0900, 0x097F),
}


def check_language_script(text, language):
    lang_key = (language or "").strip().lower()
    rng = _SCRIPT_RANGES.get(lang_key)
    if not rng:
        return True  # English or an unmapped language — skip this check
    lo, hi = rng
    body = re.sub(r"(?s)<small.*?</small>", "", text)
    total_letters = sum(1 for ch in body if ch.isalpha())
    if total_letters == 0:
        return False
    script_chars = sum(1 for ch in body if lo <= ord(ch) <= hi)
    return (script_chars / total_letters) > 0.5


def run_dry_run():
    """Print the full plan for every task and validate the environment without
    making any LLM call. Exits before any session is created."""
    def indent(text):
        return "\n".join("    " + line for line in text.splitlines())

    print("=" * 68)
    print(f"DRY RUN — {len(TASKS)} task(s) from {TASKS_SOURCE}")
    print("No LLM calls will be made.\n")

    for idx, spec in enumerate(TASKS, 1):
        task = spec["task"]
        genre = spec.get("genre") or "General"
        mediums = spec.get("mediums") or []
        languages = spec.get("languages") or []
        roles = spec.get("roles") or ["free"]
        details = spec.get("details") or ""
        checklist = spec.get("checklist") or {}
        source = "task" if checklist else "genre/default"
        inactive = spec.get('inactive') or False

        print(f"=== Task {idx}: {task}")
        print(f"  genre:       {genre}   (checklist source: {source})")
        print(f"  path:        {spec.get('path') or STORY_BASE_DIR}")
        print(f"  inactive:        {inactive}")
        print(f"  languages:   {', '.join(languages)}")
        for lang in languages:
            if (lang or "").strip().lower() in _SCRIPT_RANGES:
                print(f"               - '{lang}' -> script enforcement active (bengali/hindi)")
            else:
                print(f"               - '{lang}' -> no script check (unmapped)")
        for medium in mediums:
            flag = ""
            if medium.strip().lower() == "audio":
                flag = "   WARNING: no audio tool exists — round will be skipped by the guard"
            print(f"  mediums:     {medium}{flag}")
        print(f"  roles:       {', '.join(roles)}")
        print(f"  details:     {'present (' + str(len(details)) + ' chars)' if details else 'EMPTY'}")
        print("  editor checklist (resolved):")
        print(indent(checklist_for(genre, "editor", checklist)))
        print("  moderator checklist (resolved):")
        print(indent(checklist_for(genre, "moderator", checklist)))
        print()

    print("=" * 68)
    print("ENVIRONMENT")
    print(f"  tasks source:        {TASKS_SOURCE}")
    print(f"  genre_checklists:    {GENRE_CHECKLISTS_FILE}")
    if not os.path.isfile(GENRE_CHECKLISTS_FILE):
        print("                         MISSING — falling back to empty checklists")
    else:
        print(f"                         loaded ({len(GENRE_CHECKLISTS)} genre(s))")

    handled = {
        SELF_CHAT_PROMPT_FILE: {"%task%", "%mediums%", "%_lang%", "%details%"},
        EDITOR_PROMPT_FILE: {"%genre%", "%mediums%", "%language%", "%details%", "%checklist%"},
        MODERATOR_PROMPT_FILE: {"%genre%", "%mediums%", "%language%", "%details%", "%checklist%"},
    }
    for path, placeholders in handled.items():
        name = os.path.basename(path)
        if not os.path.isfile(path):
            print(f"  prompt file {name}: MISSING")
            continue
        with open(path, encoding="utf-8") as f:
            found = set(re.findall(r"%[a-z_]+%", f.read()))
        unhandled = found - placeholders
        if unhandled:
            print(f"  prompt file {name}: ok, but UNHANDLED placeholders {sorted(unhandled)}")
        else:
            print(f"  prompt file {name}: ok ({len(found)} placeholder(s) replaced by code)")
    print()


_PROHIBITED_NAMES = ("Kaya", "Kolpo", "কায়া", "কল্প", "काया", "कल्प")


def verify_task_fulfillment(original_text, check_text, mediums, language):
    """Deterministic (no-LLM) checks that catch the failure classes an editor/
    moderator LLM keeps missing: declared medium never delivered, header fields
    dropped during editing, citations dropped, wrong script/language, and
    agent-name leaks. Returns a list of problem strings (empty = all good)."""
    problems = []

    if "audio" in mediums:
        problems.append(
            "Medium 'audio' was declared, but no audio-generation tool exists yet "
            "in TOOLS — this task cannot currently be fulfilled by the agents."
        )

    if "image" in mediums and not re.search(r"!\[[^\]]*\]\([^)]+\)", check_text):
        problems.append("Medium 'image' was declared but no image is embedded in the final story.")

    for field in ["Task prompt", "Genre", "Mediums", "Language(s)"]:
        if f"**{field}:**" in original_text and f"**{field}:**" not in check_text:
            problems.append(f"Editor dropped the '{field}' header field.")

    if "## Citations & References" in original_text and "## Citations & References" not in check_text:
        problems.append("Editor dropped the Citations & References section.")

    if not check_language_script(check_text, language):
        problems.append(f"Story does not appear to be predominantly written in the declared language '{language}'.")

    stripped = re.sub(r"(?s)<small.*?</small>|!\[[^\]]*\]\([^)]*\)", "", check_text)
    for bad in _PROHIBITED_NAMES:
        if re.search(rf"\b{re.escape(bad)}\b", stripped):
            problems.append(f"Prohibited name '{bad}' still appears in the story text.")

    if "<!-- EDITOR FLAG:" in check_text:
        flags = re.findall(r"<!--\s*EDITOR FLAG:\s*(.*?)-->", check_text)
        for flag in flags:
            problems.append(f"Editor flagged an unresolved problem: {flag.strip()}")

    return problems


def is_duplicate(new_text, previous_text, threshold=0.8):
    if not previous_text:
        return False
    return (
        SequenceMatcher(None, new_text.strip(), previous_text.strip()).ratio()
        > threshold
    )


def login(username, password):
    resp = requests.post(
        f"{BASE_URL}/api/login",
        json={"username": username, "password": password},
        timeout=15,
    )

    resp.raise_for_status()
    return resp.json()["token"]


def create_session(token, name, system_prompts=None, context_tokens=None):
    body = {"name": name}
    if system_prompts:
        body["system_prompts"] = system_prompts
    if context_tokens:
        body["context_tokens"] = context_tokens
    resp = requests.post(
        f"{BASE_URL}/api/sessions",
        json=body,
        headers={"X-Auth-Token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def delete_session(token, session_id):
    resp = requests.delete(
        f"{BASE_URL}/api/sessions/{session_id}",
        headers={"X-Auth-Token": token},
        timeout=15,
    )
    if resp.status_code != 200:
        print(
            f"Warning: could not delete session {session_id} (HTTP {resp.status_code})"
        )
        return False
    print(f"Deleted session {session_id}")
    return True


def image_url_to_b64(image_url):
    if not image_url:
        return None
    rel_name = image_url.split("/output/")[-1]
    comfy_output = os.path.expanduser("~/local-ai-files/ComfyUI/output")
    abs_path = os.path.join(comfy_output, rel_name)
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def register_agent_tokens(tokens, usernames=None):
    try:
        requests.post(
            f"{BASE_URL}/api/register-agent",
            json={"tokens": tokens, "usernames": usernames or []},
            timeout=10,
        )
    except Exception as e:
        print(f"[wait] Could not register agent tokens: {e}")


def active_real_users():
    try:
        r = requests.get(f"{BASE_URL}/api/active-users", timeout=10)
        users = r.json().get("users", [])
    except Exception as e:
        print(f"[wait] Could not check active users: {e}")
        return []
    return users


def wait_for_user_to_leave():
    return
'''
    while True:
        real = active_real_users()
        if not real:
            print("Resuming agent workflow")
            return
        print(
            f"[wait] Real user(s) active ({', '.join(real)}) "
            f"— pausing self-chat until they log out..."
        )
        #time.sleep(POLL_INTERVAL_SECONDS)
'''

def call_llm(token, session_id, message, image_b64=None):
    headers = {"X-Auth-Token": token}

    payload = {
        "session_id": session_id,
        "message": message,
        "client_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if image_b64:
        payload["image"] = image_b64

    submit_respo = requests.post(
        f"{BASE_URL}/api/chat",
        json=payload,
        headers=headers,
        timeout=30,
    )
    submit_respo.raise_for_status()
    task_id = submit_respo.json()["task_id"]

    status_url = f"{BASE_URL}/api/status/{task_id}"

    while True:
        status_resp = requests.get(status_url, headers=headers, timeout=40)
        status_resp.raise_for_status()
        data = status_resp.json()

        status = data.get("status")

        if status == "done":
            return {
                "text": data["response"],
                "image": data.get("image"),
                "searches": data.get("_search_details"),
            }
        if status == "error":
            raise RuntimeError(f"Task failed: {data}")
        print(f"Polling break for {POLL_INTERVAL_SECONDS} seconds")
        time.sleep(POLL_INTERVAL_SECONDS)
        print("Over")


def build_input(speaker, message_number, incoming, lang, task):
    current_agent = AGENT_NAMES[speaker]
    partner_agent = AGENT_NAMES["B" if speaker == "A" else "A"]

    lines = [
        f"[SYSTEM DIRECTIVE: You are responding as {current_agent}. Your partner is {partner_agent}.]\n",
        f"[Turn {message_number}/{MAX_MESSAGES_PER_AGENT}]\n",
    ]

    if message_number <= 2:
        lines.append(
            f"[PHASE 1: ALIGNMENT] Agree on the plan/approach immediately with {partner_agent} for this task: {task}."
        )
    elif message_number >= MAX_MESSAGES_PER_AGENT - 2:
        lines.append(
            f"[PHASE 3: FINALIZATION] Consolidate the work, write the final scene/panels, "
            f"and terminate the turn sequence by appending {STOP_PHRASE}."
        )
    else:
        lines.append(
            f"[PHASE 2: DIRECT EXECUTION] Continue building content turn-by-turn. "
            f"Do not send meta-talk or prematurely end the story. Speak in {lang}."
        )

    if incoming:
        lines.extend(["", "----------", incoming])
    return "\n".join(lines)


def run_single_conversation(token_a, token_b, round_number, task, mediums, languages, roles=None, genre="General", details="", checklist=None, path=None):
    medium = random.sample(mediums, 2 if len(mediums) > 1 else 1)
    language = random.choice(languages)

    s = STARTING_CONVERSATION.replace("%task%", task)
    s = s.replace("%mediums%", " , ".join(medium))
    s = s.replace("%_lang%", language)
    s = s.replace("%details%", details or "None")

    print(s)
    prompt_block = {"name": "Self-Chat Directive", "content": s}
    session_a = create_session(
        token_a,
        f"{AGENT_NAMES['A']} round {round_number}",
        system_prompts=[prompt_block],
    )
    session_b = create_session(
        token_b,
        f"{AGENT_NAMES['B']} round {round_number}",
        system_prompts=[prompt_block],
    )

    transcript = []
    counts = {"A": 0, "B": 0}

    current_speaker = "A"

    incoming = ""
    shared_image_b64 = None

    stories_dir, fname = start_story(round_number, task, task, medium, language, roles, genre, path)
    citations = {}

    while True:
        counts[current_speaker] += 1
        message_number = counts[current_speaker]
        idx = len(transcript)
        token = token_a if current_speaker == "A" else token_b
        session = session_a if current_speaker == "A" else session_b

        prompt = build_input(
            current_speaker,
            message_number,
            "" if not transcript else incoming,
            language,
            task,
        )

        wait_for_user_to_leave()

        result = call_llm(token, session, prompt, image_b64=shared_image_b64)
        reply = result["text"]
        if not reply.strip():
            prompt += "\n[SYSTEM ERROR: Your previous output was empty. Generate real story content now.]"
            result = call_llm(token, session, prompt, image_b64=shared_image_b64)
            reply = result["text"]
            if not reply.strip():
                print(
                    f"Round {round_number} ended: {AGENT_NAMES[current_speaker]} "
                    f"returned no content after a retry\n"
                )
                break
        if is_duplicate(reply, incoming):
            # Re-prompt agent to generate new content instead of repeating
            prompt += "\n[SYSTEM ERROR: Your previous output was identical to your partner's. Generate unique content now.]"
            result = call_llm(token, session, prompt, image_b64=shared_image_b64)
            reply = result["text"]

        entry = {
            "speaker": AGENT_NAMES[current_speaker],
            "message": message_number,
            "text": reply,
            "image": result.get("image"),
            "searches": result.get("searches"),
        }
        transcript.append(entry)
        append_story_entry(entry, fname, citations, stories_dir, round_number, idx)

        if STOP_PHRASE in reply.upper():
            print(f"Round {round_number} ended by {AGENT_NAMES[current_speaker]}\n")
            break
        if counts[current_speaker] >= MAX_MESSAGES_PER_AGENT:
            print(
                f"Round {round_number} ended: {AGENT_NAMES[current_speaker]} "
                f"reached the {MAX_MESSAGES_PER_AGENT}-message cap\n"
            )
            break

        incoming = reply
        shared_image = result.get("image")
        if shared_image:
            shared_image_b64 = image_url_to_b64(shared_image)
            incoming += f"\n\n[IMAGE SHARED: {shared_image}]"
        shared_searches = result.get("searches")
        if shared_searches:
            block = []
            for s in shared_searches:
                if not isinstance(s, dict):
                    continue
                query = s.get("query", "")
                block.append(f"- Query: {query}")
                for r in s.get("results") or []:
                    title = r.get("title") or r.get("url") or ""
                    url = r.get("url", "")
                    snippet = (r.get("snippet") or r.get("content") or "")[:200]
                    line = f"  - {title}" + (f" | {snippet}" if snippet else "")
                    if url:
                        line += f" ({url})"
                    block.append(line)
            if block:
                incoming += "\n\n[WEB SEARCH REPORTS SHARED:]\n" + "\n".join(block)
        current_speaker = "B" if current_speaker == "A" else "A"
        print(f"LLM Rest for {SLEEP_BETWEEN_TURNS} seconds")
        time.sleep(SLEEP_BETWEEN_TURNS)
        print("LLM Rest Over")

    finalize_story(fname, citations)

    print("=== Title phase ===")
    with open(fname, "r", encoding="utf-8") as f:
        story_text = f.read()
    title_session, title_token = random.choice(
        [(session_a, token_a), (session_b, token_b)]
    )
    title = propose_title(title_token, title_session, task, language, genre, story_text)
    print(f"Story title: {title}\n")
    stories_dir, fname = apply_title(title, stories_dir, fname)
    print(f"Story renamed to: {fname}\n")

    print("=== Editor phase ===")
    edited_path = run_editor(stories_dir, fname, task, genre, mediums=medium, language=language, details=details, checklist=checklist)

    print("=== Deterministic verification ===")
    with open(fname, "r", encoding="utf-8") as f:
        original_text = f.read()
    check_source = edited_path if edited_path else fname
    with open(check_source, "r", encoding="utf-8") as f:
        check_text = f.read()
    problems = verify_task_fulfillment(original_text, check_text, medium, language)

    if problems:
        print(f"[verify] {len(problems)} problem(s) found — auto-RED, skipping moderator LLM call:")
        for p in problems:
            print(f"[verify]   - {p}")
        verdict_path = (edited_path if edited_path else fname).replace(".md", ".moderation.json")
        data = {
            "verdict": "RED",
            "reasons": "Automatic RED (deterministic check, no LLM call):\n" + "\n".join(f"- {p}" for p in problems),
            "task": task,
            "genre": genre,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with open(verdict_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    else:
        print("=== Moderator phase ===")
        run_moderator(stories_dir, fname, task, genre, editor_path=edited_path, mediums=medium, language=language, details=details, checklist=checklist)

    return transcript, session_a, session_b, fname


def save_transcript(transcript, round_number):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"conv_r{round_number}_{timestamp}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(transcript, f, indent=4)
    print(f"Saved transcript to {fname}")
    return fname


def slugify(text, max_len=60):
    slug = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", text, flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug[:max_len].strip("-") or "story"


def sanitize_title(text):
    """Extract a clean single-line title from an LLM reply, or None."""
    if not text:
        return None
    first = text.strip().strip('"\'«»“”‘’`').splitlines()[0].strip()
    first = re.sub(r"^(?:title|heading|name|header)\s*[:：]\s*", "", first, flags=re.IGNORECASE)
    first = re.sub(r"^\d+[.)]\s*", "", first)
    first = re.sub(r"\s+", " ", first).strip().rstrip(".।!")
    return first[:80].strip() or None


def propose_title(token, session_id, task, language, genre, story_text):
    """Ask one of the agents to name the story after it is completed."""
    prompt = (
        "You are naming a completed article/story that has reached its "
        "conclusion.\n"
        f"Task: {task}\n"
        f"Genre: {genre}\n"
        f"Write your reply ONLY in: {language}\n"
        "Read the completed story below, then propose ONE short, catchy, unique "
        "title (max 60 characters) that captures its conclusion and theme. "
        "Output ONLY the title text — no quotes, no numbering, no explanation, "
        "no names of people or agents.\n\n"
        "=== COMPLETED STORY ===\n\n"
        + (story_text or "")[:6000]
    )
    wait_for_user_to_leave()
    try:
        result = call_llm(token, session_id, prompt)
        return sanitize_title(result["text"]) or task
    except Exception as e:
        print(f"[title] Could not generate a title, falling back to task: {e}")
        return task


def apply_title(title, stories_dir, fname):
    """Update the story heading with the new title and rename the folder to match."""
    with open(fname, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_heading = f"# {title}\n"
    if lines and lines[0].startswith("# "):
        lines[0] = new_heading
    else:
        lines.insert(0, new_heading)
    with open(fname, "w", encoding="utf-8") as f:
        f.writelines(lines)

    m = re.search(r"_\d{8}_\d{6}\.md$", fname)
    if not m:
        return stories_dir, fname
    timestamp = m.group(0).lstrip("_").replace(".md", "")
    genre_dir = os.path.dirname(stories_dir)
    new_stories_dir = os.path.join(genre_dir, f"{slugify(title)}_{timestamp}")
    if new_stories_dir != stories_dir and os.path.isdir(stories_dir):
        os.rename(stories_dir, new_stories_dir)
        stories_dir = new_stories_dir
        fname = os.path.join(new_stories_dir, os.path.basename(fname))
    return stories_dir, fname


def start_story(round_number, task, title, mediums, language, roles=None, genre="General", path=None):
    base_dir = path or STORY_BASE_DIR
    os.makedirs(base_dir, exist_ok=True)
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    folder_name = f"{slugify(title)}_{timestamp}"
    genre_dir = os.path.join(base_dir, slugify(genre))
    os.makedirs(genre_dir, exist_ok=True)
    stories_dir = os.path.join(genre_dir, folder_name)
    os.makedirs(stories_dir, exist_ok=True)
    fname = os.path.join(stories_dir, f"story_r{round_number}_{timestamp}.md")
    roles = roles or ["free"]
    header = [
        f"# {title}\n",
        f"*Round {round_number} · Generated on {now.strftime('%Y-%m-%d %H:%M:%S')}*\n\n",
        f"**Task prompt:** {task}\n\n",
        f"**Genre:** {genre}\n\n",
        f"**For roles:** {' , '.join(roles)}\n\n",
        f"**Mediums:** {' , '.join(mediums)}  ·  **Language(s):** {language}\n\n",
        "---\n\n",
    ]
    with open(fname, "w", encoding="utf-8") as f:
        f.writelines(header)
    return stories_dir, fname


def clean_speaker_text(speaker, text):
    cleaned = re.sub(rf"^(kolpo|kaya|कल्प|কায়া):\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(rf"^{re.escape(speaker)}:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[NEXT TURN:\s*[^\]]*\]\s*", "", cleaned, flags=re.IGNORECASE)

    # Remove raw action tags automatically
    cleaned = re.sub(r"\[ACTION:\s*[^\]]+\]", "", cleaned, flags=re.IGNORECASE)

    return cleaned.replace("[END CONVERSATION]", "").strip()


def scrub_agent_names(text):
    """Deterministically remove the agents' names from story content.

    The models often address each other by name despite the naming rules, so the
    names are scrubbed here (vocative positions first, bare mentions as fallback).
    Structural markup is protected: image alt-text (`![Kaya](...)`) and the turn
    headers (`<small ...>_Round N · Kaya Turn M_</small>`) keep their names.
    """
    protected = []
    for token in re.findall(r"(?s)<small.*?</small>|!\[[^\]]*\]\([^)]*\)", text):
        if token not in protected:
            protected.append(token)

    for i, token in enumerate(protected):
        text = text.replace(token, f"\x00PROTECT{i}\x00")

    # Remove names in vocative or isolated positions across supported scripts
    text = re.sub(r"\b(?:Kaya|Kolpo)\b\s*[,،]+\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*\b(?:Kaya|Kolpo)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:কায়া|কল্প|काया|कल्प)\s*[,،]+\s*", "", text)
    text = re.sub(r",\s*(?:কায়া|কল্প|काया|कल्प)", "", text)
    text = re.sub(r"\b(?:Kaya|Kolpo)\b", "", text, flags=re.IGNORECASE)

    # Normalize inline space without merging lines across newlines
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        line = re.sub(r"[ \t]{2,}", " ", line)
        line = re.sub(r"[ \t]+([.,!?;:।])", r"\1", line)
        cleaned_lines.append(line.replace(" ,", ","))

    text = "\n".join(cleaned_lines)

    for i, token in enumerate(protected):
        text = text.replace(f"\x00PROTECT{i}\x00", token)

    return text.strip()


def normalize_markdown_lines(text):
    """Restore structural line breaks that the editor model may have flattened.

    The editor sometimes returns the revised markdown with most newlines collapsed
    to spaces. This re-inserts line breaks before every structural marker so the
    published page renders as separate blocks again. Within-turn paragraph breaks
    that were already lost cannot be recovered, but every heading, turn header,
    image, and citation line ends up on its own line.
    """
    # Clean horizontal whitespace per line while preserving existing explicit newlines
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)

    # Re-insert structural double newlines for proper block rendering
    text = re.sub(r"(?<!\n\n)<small ", "\n\n<small ", text)
    text = re.sub(r"(?<!\n\n)(#{1,6}\s)", r"\n\n\1", text)
    text = re.sub(
        r"(?<!\n\n)(\*\*(?:Task prompt|Genre|For roles|Mediums|Language\(s\)):)",
        r"\n\n\1",
        text,
    )
    text = re.sub(r"(?<!\n\n)(\d+\.\s+\[)", r"\n\n\1", text)
    text = re.sub(r"(?<!\n\n)!\[", "\n\n![", text)
    text = re.sub(r"</small>(?!\n\n)", "</small>\n\n", text)
    text = re.sub(r"\s*---\s*", "\n\n---\n\n", text)

    # Clean up excessive line padding
    text = re.sub(r"(?m)^ +", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip() + "\n"


def embed_story_image(img_url, stories_dir, round_number, speaker, idx):
    if not img_url:
        return None
    rel_name = img_url.split("/output/")[-1]
    comfy_output = os.path.expanduser("~/local-ai-files/ComfyUI/output")
    abs_src = os.path.join(comfy_output, rel_name)
    if not os.path.isfile(abs_src):
        return None
    _, ext = os.path.splitext(rel_name)
    local_name = f"img_r{round_number}_{speaker}_{idx}{ext}"
    dest = os.path.join(stories_dir, local_name)
    try:
        shutil.copy(abs_src, dest)
    except OSError as e:
        print(f"Warning: could not copy {abs_src}: {e}")
        return None
    print(f"Embedded image {local_name}")
    return local_name


def collect_citations(citations, searches):
    for s in searches or []:
        if not isinstance(s, dict):
            continue
        query = s.get("query", "")
        for r in s.get("results") or []:
            url = r.get("url", "")
            if not url or url in citations:
                continue
            citations[url] = (r.get("title") or url, query)


def strip_model_citations(text):
    """Remove any Citations & References block the model wrote into a turn.

    Only finalize_story() may emit that section, and it is built exclusively from
    web-search results. Model-authored variants (images, placeholder links, double
    hashes) are dropped so they can never leak into the published section.
    """
    text = re.sub(
        r"(?i)(?:^|\n)\s*#{1,6}\s+citations?\s*&?\s*references?.*$",
        "",
        text,
        flags=re.DOTALL
    )
    return text.strip()


def append_story_entry(entry, fname, citations, stories_dir, round_number, idx):
    speaker = entry.get("speaker", "Unknown")
    cleaned = clean_speaker_text(speaker, entry.get("text", ""))
    cleaned = scrub_agent_names(cleaned)
    cleaned = strip_model_citations(cleaned)
    turn = entry.get("message", idx)
    lines = [
        f"<small style=\"color:#888\">_Round {round_number} · {speaker} Turn {turn}_</small>\n\n",
        f"{cleaned}\n\n",
    ]

    collect_citations(citations, entry.get("searches"))

    local_img = embed_story_image(
        entry.get("image"), stories_dir, round_number, speaker, idx
    )
    if local_img:
        lines.append(f"![{speaker}]({local_img})\n\n")

    with open(fname, "a", encoding="utf-8") as f:
        f.writelines(lines)


def finalize_story(fname, citations):
    if citations:
        lines = ["\n---\n\n## Citations & References\n\n"]
        for num, (url, (title, query)) in enumerate(citations.items(), start=1):
            lines.append(
                f"{num}. [{title}]({url})"
                + (f" *(source: {query})*" if query else "")
                + "\n"
            )
        with open(fname, "a", encoding="utf-8") as f:
            f.writelines(lines)
    print(f"Saved story to {fname}")


def file_to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def story_images_in_order(stories_dir, markdown_text):
    """Return [(filename, abs_path)] of images referenced in the markdown, in order."""
    ordered = []
    seen = set()
    for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown_text):
        fname = os.path.basename(ref)
        if fname in seen:
            continue
        seen.add(fname)
        full = os.path.join(stories_dir, fname)
        if os.path.isfile(full):
            ordered.append((fname, full))
    return ordered


def extract_markdown_fence(text):
    match = re.search(
        r"```(?:markdown|md)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    return text.strip()


def run_editor(stories_dir, fname, task, genre, mediums=None, language="", details="", checklist=None):
    """Editor phase: review images + markdown, write story_rN_ts.edited.md."""
    try:
        token = login(USERNAME_EDITOR, PASSWORD)
    except Exception as e:
        print(f"[editor] Could not log in {USERNAME_EDITOR}: {e}")
        return None
    register_agent_tokens([token], [USERNAME_EDITOR])
    try:
        prompt = open(EDITOR_PROMPT_FILE, encoding="utf-8").read()
        context_tokens = {
            "%genre%": genre,
            "%mediums%": ", ".join(mediums or []),
            "%language%": language or "",
            "%details%": details or "None",
            "%checklist%": checklist_for(genre, "editor", checklist),
        }
        for placeholder, value in context_tokens.items():
            prompt = prompt.replace(placeholder, value)
    except OSError as e:
        print(f"[editor] Could not read prompt file: {e}")
        return None
    session_id = create_session(
        token,
        "Editor review",
        system_prompts=[{"name": "Editor Directive", "content": prompt}],
        context_tokens=context_tokens,
    )
    edited_path = fname.replace(".md", ".edited.md")
    try:
        with open(fname, "r", encoding="utf-8") as f:
            markdown_text = f.read()
        for img_fname, full in story_images_in_order(stories_dir, markdown_text):
            wait_for_user_to_leave()
            call_llm(
                token,
                session_id,
                f"This is the image referenced in the story as {img_fname}. "
                "Look at it carefully; it is part of the story you must edit.",
                image_b64=file_to_b64(full),
            )
        wait_for_user_to_leave()
        result = call_llm(
            token,
            session_id,
            "Here is the full story markdown:\n\n"
            + markdown_text
            + "\n\nNow return the complete revised markdown, wrapped in a "
            "single ```markdown code block. Nothing else.",
        )
        revised = extract_markdown_fence(result["text"])
        revised = scrub_agent_names(revised) if revised else revised
        revised = normalize_markdown_lines(revised) if revised else revised
        if not revised:
            print("[editor] Editor returned an empty revision; keeping original")
            return None
        with open(edited_path, "w", encoding="utf-8") as f:
            f.write(revised + "\n")
        print(f"[editor] Saved edited story to {edited_path}")
        return edited_path
    except Exception as e:
        print(f"[editor] Editor phase failed: {e}")
        return None
    finally:
        if not keep_sessions:
            delete_session(token, session_id)


def run_moderator(stories_dir, fname, task, genre, editor_path=None, mediums=None, language="", details="", checklist=None):
    """Moderator phase: GREEN/RED verdict, written to story_rN_ts.moderation.json."""
    try:
        token = login(USERNAME_MODERATOR, PASSWORD)
    except Exception as e:
        print(f"[moderator] Could not log in {USERNAME_MODERATOR}: {e}")
        return None
    register_agent_tokens([token], [USERNAME_MODERATOR])
    try:
        prompt = open(MODERATOR_PROMPT_FILE, encoding="utf-8").read()
        context_tokens = {
            "%genre%": genre,
            "%mediums%": ", ".join(mediums or []),
            "%language%": language or "",
            "%details%": details or "None",
            "%checklist%": checklist_for(genre, "moderator", checklist),
        }
        for placeholder, value in context_tokens.items():
            prompt = prompt.replace(placeholder, value)
    except OSError as e:
        print(f"[moderator] Could not read prompt file: {e}")
        return None
    session_id = create_session(
        token,
        "Moderator review",
        system_prompts=[{"name": "Moderator Directive", "content": prompt}],
        context_tokens=context_tokens,
    )
    try:
        source = editor_path if editor_path else fname
        with open(source, "r", encoding="utf-8") as f:
            markdown_text = f.read()
        for img_fname, full in story_images_in_order(stories_dir, markdown_text):
            wait_for_user_to_leave()
            call_llm(
                token,
                session_id,
                f"This is the image referenced in the story as {img_fname}.",
                image_b64=file_to_b64(full),
            )
        wait_for_user_to_leave()
        result = call_llm(
            token,
            session_id,
            "Here is the final story markdown:\n\n"
            + markdown_text
            + "\n\nGive your verdict using exactly these two lines:\n"
            "VERDICT: GREEN\nREASONS: <short reasons>",
        )
        verdict = "UNKNOWN"
        m = re.search(
            r"VERDICT\s*:\s*(GREEN|RED)", result["text"], flags=re.IGNORECASE
        )
        if m:
            verdict = m.group(1).upper()
        elif re.search(r"\bGREEN\b", result["text"]):
            verdict = "GREEN"
        elif re.search(r"\bRED\b", result["text"]):
            verdict = "RED"
        verdict_path = fname.replace(".md", ".moderation.json")
        data = {
            "verdict": verdict,
            "reasons": result["text"],
            "task": task,
            "genre": genre,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with open(verdict_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[moderator] Verdict {verdict} saved to {verdict_path}")
        return data
    except Exception as e:
        print(f"[moderator] Moderator phase failed: {e}")
        return None
    finally:
        if not keep_sessions:
            delete_session(token, session_id)


def run_forever():
    token_a = login(USERNAME_A, PASSWORD)
    token_b = login(USERNAME_B, PASSWORD)
    register_agent_tokens([token_a, token_b], [USERNAME_A, USERNAME_B])
    print("Logged In")

    round_number = 1
    task_index = 0

    try:
        while True:
            spec = TASKS[task_index % len(TASKS)]
            task = spec["task"]
            mediums = spec["mediums"]
            languages = spec["languages"]
            roles = spec.get("roles") or ["free"]
            genre = spec.get("genre") or "General"
            details = spec.get("details") or ""
            checklist = spec.get("checklist") or {}
            path = spec.get("path")
            print(roles)
            if "admin" in roles:
                path = f"{path}/admin"
            if "premium" in roles:
                path = f"{path}/premium"

            print("The stories will be generated in this directory", path)
            inactive = spec.get("inactive") or False
            if inactive:
                print(f"Task {task} is inactive, skipping the task")
                task_index += 1
                continue

            if "audio" in mediums:
                print(
                    f"[guard] Task declares 'audio', but no audio tool exists in TOOLS — "
                    f"skipping round {round_number} without running it.\n"
                )
                round_number += 1
                task_index += 1
                continue
            print(f"=== Starting round {round_number}: {task} (genre: {genre}, roles: {', '.join(roles)}) ===\n")
            start_time = time.time()
            transcript, session_a, session_b, fname = run_single_conversation(
                token_a, token_b, round_number, task, mediums, languages, roles, genre, details, checklist, path
            )
            # save_transcript(transcript, round_number)
            if not keep_sessions:
                delete_session(token_a, session_a)
                delete_session(token_b, session_b)
            round_number += 1
            task_index += 1
            elapsed = time.time() - start_time
            print(f"Total time elapsed in round {round_number} - {elapsed:.2f} seconds\n")
            print(f"Autonomous organization is in vacation for {SLEEP_BETWEEN_ROUNDS} seconds")
            time.sleep(SLEEP_BETWEEN_ROUNDS)
            print("Vacation over")
    except KeyboardInterrupt:
        print("\nManual Interruption")


TASKS, TASKS_SOURCE, TASK_CHECKLISTS = load_tasks()
if not TASKS:
    print("No tasks to run. Add tasks to a config file and restart.")
    raise SystemExit(1)

GENRE_CHECKLISTS = load_genre_checklists(TASK_CHECKLISTS)
print(f"Loaded {len(TASKS)} task(s) from {TASKS_SOURCE}")

if args.dry_run:
    run_dry_run()
    raise SystemExit(0)

user_input = input("Keep sessions {y/n} [default: n] ? ")
keep_sessions = user_input.strip().lower() == "y"


if __name__ == "__main__":
    run_forever()
