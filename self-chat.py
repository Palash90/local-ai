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
parser.add_argument(
    "--gpu",
    action="store_true",
    help="Run the self-chat agents (kolpo/kaya/editor/moderator) on the "
    "interactive GPU llama-server (8081) instead of the RAM-backed CPU "
    'server (8079). Each /api/chat request carries mode="gpu" so '
    "chat-webui routes the agent tasks to the GPU lane.",
)
args = parser.parse_args()
STORY_BASE_DIR = os.path.expanduser("~/local-ai-files/stories")

# Tiered story roots for premium/admin tasks whose spec declares no path.
# Resolved the same way markdown_hosting.py resolves its collections: from
# the OS environment (STORIES_PREMIUM_DIR / STORIES_ADMIN_DIR), falling back
# to the shared free stories dir when unset.
PREMIUM_STORIES_DIR = os.getenv("STORIES_PREMIUM_DIR")
ADMIN_STORIES_DIR = os.getenv("STORIES_ADMIN_DIR")

BASE_URL = "http://localhost:3001"
USERNAME_A = "kolpo"
USERNAME_B = "kaya"
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 2.0
SLEEP_BETWEEN_TURNS = 1.0
MAX_MESSAGES_PER_AGENT = 10
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
SELF_CHAT_PROMPT_FILE = "/home/palash/local-ai-files/self_chat.txt"
STARTING_CONVERSATION = open(SELF_CHAT_PROMPT_FILE).read()

SLEEP_BETWEEN_ROUNDS = 10

USERNAME_EDITOR = "editor"
USERNAME_MODERATOR = "moderator"
EDITOR_PROMPT_FILE = "/home/palash/local-ai-files/contexts/editor.txt"
MODERATOR_PROMPT_FILE = "/home/palash/local-ai-files/contexts/moderator.txt"

DEFAULT_TASKS_FILE = os.path.expanduser("~/local-ai-files/tasks.json")

# All participants of the self-chat window (kolpo, kaya, editor, moderator)
# share one theme scope, so the theme is coordinated between the users of the
# window while regular per-user chats stay isolated in their own scopes.
SELF_CHAT_THEME_SCOPE = "self-chat"
SELF_CHAT_THEME_LIMIT = 30


GENRE_CHECKLISTS_FILE = os.path.expanduser("~/local-ai-files/genre_checklists.json")


GENRE_PERSONA_MAP_FILE = os.path.expanduser(
    "~/local-ai-files/contexts/genre_persona_map.json"
)
PERSONA_POOL_FILE = os.path.expanduser("~/local-ai-files/contexts/persona_pool.json")

_persona_cycles = {}


def load_json_file(filepath, fallback):
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[persona] Could not load {filepath}: {e} — using fallback")
        return fallback


GENRE_PERSONA_MAP = load_json_file(GENRE_PERSONA_MAP_FILE, {})
PERSONA_POOL = load_json_file(PERSONA_POOL_FILE, {})
MASTER_DETAILS = load_json_file(os.path.expanduser("~/local-ai-files/contexts/master_details.json"), {})

def pick_persona_round_robin(pool, genre, genre_map, task_roles = None):
    global _persona_cycles
    allowed = genre_map.get(genre) or genre_map.get("default")

    # Hard safety rule: exclude Parent & Child from Adventure & Horror
    if genre == "Adventure & Horror" and allowed:
        allowed = [r for r in allowed if r != "Parent & Child"]

    task_roles = task_roles or []

    # Flatten pool: (relationship, mood, details_dict)
    candidates = []
    for rel, moods in pool.items():
        if allowed and rel not in allowed:
            continue
        for mood, details in moods.items():
            req_role = details.get("required_role")

            # If profile requires premium/admin, skip if current task roles don't match
            if req_role:
                is_premium_or_admin = any(r in task_roles for r in ["premium", "admin"])
                if req_role == "premium" and not is_premium_or_admin:
                    continue
                elif req_role == "admin" and "admin" not in task_roles:
                    continue
            candidates.append((rel, mood, details))

    if not candidates:
        # Fallback default
        fallback_details = {
            "Kaya": {
                "role": "Colleague",
                "persona": "Creative and energetic problem solver",
            },
            "Kolpo": {
                "role": "Colleague",
                "persona": "Methodical and structured partner",
            },
        }
        return "Colleagues", "Focused Collaboration", fallback_details

    if genre not in _persona_cycles or _persona_cycles[genre]["idx"] >= len(
        _persona_cycles[genre]["pairs"]
    ):
        shuffled = list(candidates)
        random.shuffle(shuffled)
        _persona_cycles[genre] = {"pairs": shuffled, "idx": 0}

    state = _persona_cycles[genre]
    choice = state["pairs"][state["idx"]]
    state["idx"] += 1
    return choice  # Returns (relationship, mood, details_dict)


def deep_merge(target, source):
    """Recursively merge dictionary source into target."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def load_genre_checklists(extra=None):
    """Base checklists from genre_checklists.json, overridden per genre by any
    'genre_checklists' carried in a task config file."""
    checklists = {}
    try:
        with open(GENRE_CHECKLISTS_FILE, encoding="utf-8") as f:
            checklists = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(
            f"[checklist] Could not load {GENRE_CHECKLISTS_FILE}: {e} — using empty checklists"
        )
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
        details = item.get("details") or ""
        if isinstance(details, str):
            details = details.strip()
        checklist = item.get("checklist") or {}
        path = (item.get("path") or "").strip() or None
        inactive = item.get("inactive") or False
        context = item.get("context") or None
        turns = MAX_MESSAGES_PER_AGENT
        raw_turns = item.get("turns")
        if raw_turns is not None:
            try:
                turns = int(raw_turns)
            except (TypeError, ValueError):
                turns = MAX_MESSAGES_PER_AGENT
            if turns < 2:
                turns = MAX_MESSAGES_PER_AGENT
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
                "inactive": inactive,
                "context": context,
                "turns": turns,
            }
        )
    return tasks


_detail_cycles = {}


def _fmt_detail_value(value):
    """Render a resolved detail value as prompt text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _join_values(values, separator=None):
    """Join multi-selected values naturally: 'a', 'a and b', 'a, b and c'."""
    if separator:
        return separator.join(values)
    if len(values) <= 1:
        return values[0] if values else ""
    return ", ".join(values[:-1]) + " and " + values[-1]


def _resolve_count(task, name, spec):
    """Resolve how many values a multi selector should pick.

    ``count`` may be a plain integer/string or its own selector spec
    (e.g. ``{"selector": "random", "values": [2, 3]}``), resolved through
    the same selector machinery so it can vary per round.
    """
    count = spec.get("count")
    if count is None:
        return 1
    count_spec = count if isinstance(count, dict) else {"value": count}
    resolved = _pick_detail_value(task, f"{name}::count", count_spec)
    if isinstance(resolved, (list, tuple)):
        return len(resolved)
    try:
        return max(0, int(resolved))
    except (TypeError, ValueError):
        return 1


def _pick_detail_value(task, name, spec):
    """Pick value(s) for one detail field according to its selector.

    Supported selectors:
      - random:           pick one value at random (fresh each round)
      - roundrobin:       cycle through values one at a time across rounds
      - random_multi:     pick ``count`` distinct values at random
      - roundrobin_multi: slide a window of ``count`` values across rounds
      - absent / static / first: use ``value`` if given, else ``values[0]``
    """
    if "value" in spec:
        return spec["value"]

    values = spec.get("values") or []
    selector = str(spec.get("selector") or "").strip().lower()

    if not values:
        return None

    if selector == "random":
        return random.choice(values)

    if selector == "roundrobin":
        key = (task, name)
        idx = _detail_cycles.get(key, 0) % len(values)
        _detail_cycles[key] = idx + 1
        return values[idx]

    if selector == "random_multi":
        count = _resolve_count(task, name, spec)
        return random.sample(values, min(count, len(values)))

    if selector == "roundrobin_multi":
        count = _resolve_count(task, name, spec)
        key = (task, name)
        n = len(values)
        start = _detail_cycles.get(key, 0) % n
        _detail_cycles[key] = start + count
        return [values[(start + i) % n] for i in range(min(count, n))]

    return values[0]


def _resolve_field_value(spec, task, master):
    """Resolve a single detail field spec into ``(name, value)``."""
    if not isinstance(spec, dict):
        return "", str(spec)
    
    ref = spec.get("ref")
    if ref and master and isinstance(master.get(ref), dict):
        # Start with master definition, then override with spec
        merged = dict(master[ref])
        merged.update(spec)
        spec = merged

    # If name wasn't explicitly provided in spec, fallback to master's name or ref key
    name = str(spec.get("name") or ref or "").strip()
    value = _pick_detail_value(task, name, spec)
    return name, value


def resolve_details(details, task, master=None):
    """Resolve a task's ``details`` spec into a prompt-ready string.

    Accepts:
      - a plain string (legacy format): returned unchanged
      - a list of ``{name, selector, values}`` field specs
      - a dict keyed by field name (``{name: {selector, values}}``)

    A field may reference a shared definition from ``master`` (the
    ``master_details`` block in a task config, or the ``detail_fields.json``
    file) via ``{"name": ..., "ref": "pool_name"}``; local keys override the
    master definition.

    Fields resolve to an inline comma list, e.g.
    ``"animal: horse, time: evening"``. Multi-select fields render as
    ``"animals: horse, elephant"``. Resolution happens once per round.
    """
    if master is None:
        master = MASTER_DETAILS

    if isinstance(details, str):
        return details

    if isinstance(details, list):
        specs = details
    elif isinstance(details, dict):
        specs = [
            {"name": name, **(spec if isinstance(spec, dict) else {"value": spec})}
            for name, spec in details.items()
        ]
    else:
        return str(details)

    parts = []
    for spec in specs:
        if not isinstance(spec, dict):
            parts.append(str(spec))
            continue
        name, value = _resolve_field_value(spec, task, master)
        if not name or value is None:
            continue
        if isinstance(value, (list, tuple)):
            formatted = [_fmt_detail_value(v) for v in value]
            if not formatted:
                continue
            rendered = _join_values(formatted, spec.get("separator"))
        else:
            rendered = _fmt_detail_value(value)
        if not rendered:
            continue
        parts.append(f"{name}: {rendered}")
    return ", ".join(parts)


def resolve_details_fields(details, task, master=None):
    """Resolve a task's ``details`` spec into ``{field: value}`` pairs.

    Like :func:`resolve_details` but returns the raw resolved values (lists
    stay lists) keyed by field name, so the exact combination can be hashed by
    the theme tracker. A plain string returns ``{}`` (nothing to track).
    """
    if master is None:
        master = MASTER_DETAILS

    if isinstance(details, str):
        return {}

    if isinstance(details, list):
        specs = details
    elif isinstance(details, dict):
        specs = [
            {"name": name, **(spec if isinstance(spec, dict) else {"value": spec})}
            for name, spec in details.items()
        ]
    else:
        return {}

    fields = {}
    for spec in specs:
        name, value = _resolve_field_value(spec, task, master)
        if not name or value is None:
            continue
        if isinstance(value, (list, tuple)):
            formatted = [_fmt_detail_value(v) for v in value]
            value = formatted if formatted else None
        else:
            value = _fmt_detail_value(value)
        if value:
            fields[name] = value
    return fields


def build_combo_dict(genre, mood, persona_details, details_fields):
    """Assemble the tracked combination: detail fields + mood + genre + role + persona."""
    kaya = (persona_details or {}).get("Kaya", {}) or {}
    kolpo = (persona_details or {}).get("Kolpo", {}) or {}
    return {
        "genre": genre or "",
        "mood": mood or "",
        "role": " / ".join(x for x in [kaya.get("role"), kolpo.get("role")] if x),
        "persona": " / ".join(
            x for x in [kaya.get("persona"), kolpo.get("persona")] if x
        ),
        "details": details_fields or {},
    }


def build_theme_slug(task, mood, detail_fields, max_len=80):
    """Build a short, readable theme slug purely from already-resolved data —
    no LLM call needed. detail_fields already carries real variety via its
    roundrobin/random selectors (hero, setting, festival, sweet, mystery,
    trope, etc.); combining the most 'subject-like' ones with the mood gives
    a distinct, human-readable premise for free. combo_hash (the actual
    dedup mechanism used by check_combo_used) never reads this field — it
    only makes the 'Already Produced Themes' prompt block readable for the
    agents, so there is nothing here that needs an LLM to invent.
    """
    preferred_keys = [
        "hero", "mystery", "trope", "sweet", "festival", "animals",
        "domain", "topic", "target", "setting",
    ]
    parts = []
    for key in preferred_keys:
        val = detail_fields.get(key)
        if not val:
            continue
        parts.append(val if isinstance(val, str) else ", ".join(val))
        if len(parts) >= 2:
            break
    if not parts:
        for v in detail_fields.values():
            parts.append(v if isinstance(v, str) else ", ".join(v))
            if len(parts) >= 2:
                break
    if mood:
        parts.append(mood)
    slug = " · ".join(p for p in parts if p) or task
    return slug[:max_len]


def theme_api(action, token, **payload):
    """Talk to the server's theme tracker (/api/themes). Returns parsed JSON."""
    headers = {"X-Auth-Token": token}
    try:
        if action == "list":
            r = requests.get(
                f"{BASE_URL}/api/themes", params=payload, headers=headers, timeout=15
            )
            r.raise_for_status()
        else:
            r = requests.post(
                f"{BASE_URL}/api/themes", json=payload, headers=headers, timeout=15
            )
            r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[theme] {action} failed: {e}")
        return {"ok": False, "error": str(e)}


def fetch_used_themes(token, scope=SELF_CHAT_THEME_SCOPE):
    """Return the list of already-logged theme records for the window scope."""
    data = theme_api("list", token, scope=scope, limit=SELF_CHAT_THEME_LIMIT)
    return data.get("themes", []) if data.get("ok") else []


def check_combo_used(token, combo, scope=SELF_CHAT_THEME_SCOPE):
    data = theme_api("check", token, operation="check", scope=scope, **combo)
    return bool(data.get("used")) if data.get("ok") else False


def format_theme_block(records):
    """Render the shared 'Already Produced Themes' block for the prompt."""
    if not records:
        return "None yet — everything is available."
    lines = []
    for r in records:
        bits = []
        if r.get("genre"):
            bits.append(f"genre: {r['genre']}")
        if r.get("mood"):
            bits.append(f"mood: {r['mood']}")
        if r.get("role"):
            bits.append(f"role: {r['role']}")
        if r.get("persona"):
            bits.append(f"persona: {r['persona']}")
        if r.get("details") and r.get("details") != "{}":
            try:
                det = json.loads(r["details"])
            except (TypeError, ValueError):
                det = {}
            if det:
                bits.append("details: " + ", ".join(f"{k}={v}" for k, v in det.items()))
        if r.get("theme"):
            bits.append(f"theme: {r['theme']}")
        if not bits:
            continue
        status = r.get("status") or ""
        lines.append(f"  - ({status}) " + " | ".join(bits))
    return "\n".join(lines) if lines else "None yet — everything is available."


def load_config_file(tasks_file):
    if not os.path.isfile(tasks_file):
        print(f"Tasks file not found: {tasks_file}")
        return [], {}, {}, {}
    with open(tasks_file, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[config] Invalid JSON in {tasks_file}: {e.msg}")
            raise SystemExit(1) from e

    if isinstance(data, dict):
        tasks = _parse_tasks(data.get("tasks") or [])
        checklists = data.get("genre_checklists") or {}
        persona_map = data.get("genre_persona_map") or {}
        persona_pool = data.get("persona_pool") or {}
        master = data.get("master_details") or {}
        if master:
            MASTER_DETAILS.update(master)
        return tasks, checklists, persona_map, persona_pool

    return _parse_tasks(data), {}, {}, {}


def load_tasks():
    checklists = {}

    if args.config:
        persona_map = {}
        persona_pool = {}

        tasks, cfg_checklists, cfg_persona_map, cfg_persona_pool = load_config_file(
            args.config
        )
        source = args.config
        checklists.update(cfg_checklists)

        # Merge config persona overrides
        deep_merge(persona_map, cfg_persona_map)
        deep_merge(persona_pool, cfg_persona_pool)

        if args.defaults:
            defaults, def_checklists, def_pmap, def_ppool = load_config_file(
                DEFAULT_TASKS_FILE
            )
            existing = {t["task"] for t in tasks}
            tasks.extend(t for t in defaults if t["task"] not in existing)
            checklists.update(def_checklists)
            source = f"{args.config} + defaults"
    else:
        persona_map = load_json_file(GENRE_PERSONA_MAP_FILE, {})
        persona_pool = load_json_file(PERSONA_POOL_FILE, {})

        tasks, def_checklists, def_pmap, def_ppool = load_config_file(
            DEFAULT_TASKS_FILE
        )
        checklists.update(def_checklists)
        source = DEFAULT_TASKS_FILE

    return tasks, source, checklists, persona_map, persona_pool


def checklist_for(genre, role, task_checklist=None):
    """role is 'editor' or 'moderator'. A task's own checklist wins, then the
    genre's entry, then the 'default' entry, then nothing."""
    items = (task_checklist or {}).get(role)
    if not items:
        entry = GENRE_CHECKLISTS.get(genre) or {}
        items = entry.get(role)
    if not items:
        items = GENRE_CHECKLISTS.get("default", {}).get(role) or []
    items.append(
        "Remove any out-of-character planning or meta-discussion between the collaborators (e.g. 'let's write about X', 'I'll cover Y, you do Z') that is not part of the narrative/content itself — the final piece must read as continuous, in-universe content only."
    )
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
        inactive = spec.get("inactive") or False

        print(f"=== Task {idx}: {task}")
        print(f"  genre:       {genre}   (checklist source: {source})")
        print(f"  path:        {resolve_story_path(spec, roles)}")
        print(f"  inactive:        {inactive}")
        print(f"  languages:   {', '.join(languages)}")
        for lang in languages:
            if (lang or "").strip().lower() in _SCRIPT_RANGES:
                print(
                    f"               - '{lang}' -> script enforcement active (bengali/hindi)"
                )
            else:
                print(f"               - '{lang}' -> no script check (unmapped)")
        for medium in mediums:
            flag = ""
            if medium.strip().lower() == "audio":
                flag = "   WARNING: no audio tool exists — round will be skipped by the guard"
            print(f"  mediums:     {medium}{flag}")
        print(f"  roles:       {', '.join(roles)}")
        print(f"  turns:       {spec.get('turns') or MAX_MESSAGES_PER_AGENT} per agent")
        if isinstance(details, list):
            names = [
                d.get("name", "?") if isinstance(d, dict) else "?" for d in details
            ]
            print(
                f"  details:     {len(details)} structured field(s) -> {', '.join(str(n) for n in names)}"
            )
        elif isinstance(details, dict):
            print(
                f"  details:     {len(details)} structured field(s) -> {', '.join(details)}"
            )
        else:
            print(
                f"  details:     {'present (' + str(len(details)) + ' chars)' if details else 'EMPTY'}"
            )
        print("  editor checklist (resolved):")
        print(indent(checklist_for(genre, "editor", checklist)))
        print("  moderator checklist (resolved):")
        print(indent(checklist_for(genre, "moderator", checklist)))
        print("ENVIRONMENT")
        print(f"  tasks source:        {TASKS_SOURCE}")
        print(
            f"  persona map:         {len(GENRE_PERSONA_MAP)} genre mapping(s) active"
        )
        print(
            f"  persona pool:        {len(PERSONA_POOL)} relationship category(ies) active"
        )
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
        SELF_CHAT_PROMPT_FILE: {
            "%task%",
            "%mediums%",
            "%_lang%",
            "%details%",
            "%themes%",
            "%relationship%",
            "%mood%",
            "%kaya_role%",
            "%kaya_persona%",
            "%kolpo_role%",
            "%kolpo_persona%",
        },
        EDITOR_PROMPT_FILE: {
            "%genre%",
            "%mediums%",
            "%language%",
            "%details%",
            "%checklist%",
        },
        MODERATOR_PROMPT_FILE: {
            "%genre%",
            "%mediums%",
            "%language%",
            "%details%",
            "%checklist%",
        },
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
            print(
                f"  prompt file {name}: ok, but UNHANDLED placeholders {sorted(unhandled)}"
            )
        else:
            print(
                f"  prompt file {name}: ok ({len(found)} placeholder(s) replaced by code)"
            )
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
        problems.append(
            "Medium 'image' was declared but no image is embedded in the final story."
        )

    for field in ["Task prompt", "Genre", "Mediums", "Language(s)"]:
        if f"**{field}:**" in original_text and f"**{field}:**" not in check_text:
            problems.append(f"Editor dropped the '{field}' header field.")

    if (
        "## Citations & References" in original_text
        and "## Citations & References" not in check_text
    ):
        problems.append("Editor dropped the Citations & References section.")

    if not check_language_script(check_text, language):
        problems.append(
            f"Story does not appear to be predominantly written in the declared language '{language}'."
        )

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


"""
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
"""


def call_llm(token, session_id, message, image_b64=None):
    headers = {"X-Auth-Token": token}

    payload = {
        "session_id": session_id,
        "message": message,
        "client_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if args.gpu:
        payload["mode"] = "gpu"
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
        time.sleep(POLL_INTERVAL_SECONDS)


def build_input(
    speaker, message_number, incoming, lang, task, context=None, turns=None
):
    current_agent = AGENT_NAMES[speaker]
    partner_agent = AGENT_NAMES["B" if speaker == "A" else "A"]

    if turns is None:
        turns = MAX_MESSAGES_PER_AGENT

    lines = [
        f"[SYSTEM DIRECTIVE: You are responding as {current_agent}. Your partner is {partner_agent}.]\n",
        f"[Turn {message_number}/{turns}]\n",
    ]

    if context is not None:
        print("Extra context", context)
        lines.append(context)

    if message_number <= 2:
        lines.append(
            f"[PHASE 1: DECISION & PLANNING] Analyze options, debate trade-offs with {partner_agent}, and DECIDE on"
            f" a concrete creative direction and role division for this task: {task}."
        )
    elif message_number >= turns - 2:
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


def run_single_conversation(
    token_a,
    token_b,
    round_number,
    task,
    mediums,
    languages,
    roles=None,
    genre="General",
    details="",
    checklist=None,
    path=None,
    context=None,
    persona=None,
    themes_context="",
    turns=None,
    task_roles = None
):
    medium = random.sample(mediums, 2 if len(mediums) > 1 else 1)
    language = random.choice(languages)

    # Pick persona tuple. run_forever may pass a pre-picked one so the theme
    # tracker sees the exact same combination the conversation will use.
    if persona is None:
        relationship, mood, persona_details = pick_persona_round_robin(
            PERSONA_POOL, genre, GENRE_PERSONA_MAP, task_roles
        )
    else:
        relationship, mood, persona_details = persona

    persona_info = {
        "relationship": relationship,
        "mood": mood,
        "details": persona_details,
    }

    kaya_info = persona_details.get("Kaya", {})
    kolpo_info = persona_details.get("Kolpo", {})

    print(f"[persona] Genre: {genre} | Dynamic: {relationship} ({mood})")
    print(f"[persona] Kaya: {kaya_info.get('role')} — {kaya_info.get('persona')}")
    print(f"[persona] Kolpo: {kolpo_info.get('role')} — {kolpo_info.get('persona')}")

    s = STARTING_CONVERSATION.replace("%task%", task)
    s = s.replace("%mediums%", " , ".join(medium))
    s = s.replace("%_lang%", language)
    s = s.replace("%details%", details or "None")
    s = s.replace("%themes%", themes_context or "None yet — everything is available.")
    s = s.replace("%relationship%", relationship)
    s = s.replace("%mood%", mood)
    s = s.replace("%kaya_role%", kaya_info.get("role", "Partner"))
    s = s.replace("%kaya_persona%", kaya_info.get("persona", "Creative"))
    s = s.replace("%kolpo_role%", kolpo_info.get("role", "Partner"))
    s = s.replace("%kolpo_persona%", kolpo_info.get("persona", "Methodical"))

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

    if turns is None:
        turns = MAX_MESSAGES_PER_AGENT

    current_speaker = "A"

    incoming = ""
    shared_image_b64 = None

    stories_dir, fname = start_story(
        round_number, task, task, medium, language, roles, genre, path, persona_info
    )
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
            context,
            turns,
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
        if counts[current_speaker] >= turns:
            print(
                f"Round {round_number} ended: {AGENT_NAMES[current_speaker]} "
                f"reached the {turns}-message cap\n"
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
    edited_path = run_editor(
        stories_dir,
        fname,
        task,
        genre,
        mediums=medium,
        language=language,
        details=details,
        checklist=checklist,
    )

    print("=== Deterministic verification ===")
    with open(fname, "r", encoding="utf-8") as f:
        original_text = f.read()
    check_source = edited_path if edited_path else fname
    with open(check_source, "r", encoding="utf-8") as f:
        check_text = f.read()
    problems = verify_task_fulfillment(original_text, check_text, medium, language)

    if problems:
        print(
            f"[verify] {len(problems)} problem(s) found — auto-RED, skipping moderator LLM call:"
        )
        for p in problems:
            print(f"[verify]   - {p}")
        verdict_path = (edited_path if edited_path else fname).replace(
            ".md", ".moderation.json"
        )
        data = {
            "verdict": "RED",
            "reasons": "Automatic RED (deterministic check, no LLM call):\n"
            + "\n".join(f"- {p}" for p in problems),
            "task": task,
            "genre": genre,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with open(verdict_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    else:
        print("=== Moderator phase ===")
        print("Moderator Phase Skipped, not much value add")
        # run_moderator(stories_dir, fname, task, genre, editor_path=edited_path, mediums=medium, language=language, details=details, checklist=checklist)

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
    first = text.strip().strip("\"'«»“”‘’`").splitlines()[0].strip()
    first = re.sub(
        r"^(?:title|heading|name|header)\s*[:：]\s*", "", first, flags=re.IGNORECASE
    )
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
        "=== COMPLETED STORY ===\n\n" + (story_text or "")[:6000]
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


def resolve_story_path(spec, roles):
    path = spec.get("path")
    if path:
        if "admin" in roles:
            path = f"{path}/admin"
        if "premium" in roles:
            path = f"{path}/premium"
        return path
        
    if "admin" in roles:
        if not ADMIN_STORIES_DIR:
            raise ValueError("STORIES_ADMIN_DIR environment variable is not set!")
        return ADMIN_STORIES_DIR
        
    if "premium" in roles:
        if not PREMIUM_STORIES_DIR:
            raise ValueError("STORIES_PREMIUM_DIR environment variable is not set!")
        return PREMIUM_STORIES_DIR
        
    return STORY_BASE_DIR


def start_story(
    round_number,
    task,
    title,
    mediums,
    language,
    roles=None,
    genre="General",
    path=None,
    persona_info=None,
):
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
    rel = persona_info.get("relationship", "N/A") if persona_info else "N/A"
    mood = persona_info.get("mood", "N/A") if persona_info else "N/A"

    header = [
        f"# {title}\n",
        f"*Round {round_number} · Generated on {now.strftime('%Y-%m-%d %H:%M:%S')}*\n\n",
        f"**Task prompt:** {task}\n\n",
        f"**Genre:** {genre}  ·  **Dynamic:** {rel} ({mood})\n\n",
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
        flags=re.DOTALL,
    )
    return text.strip()


def extract_tagged_content(text):
    """Return only the text inside [CONTENT]...[/CONTENT] blocks, discarding
    everything else (planning talk, meta-commentary). Returns None if no
    [CONTENT] block is present at all — the caller treats that as a
    planning-only turn with nothing to publish. Multiple blocks are
    concatenated in order."""
    blocks = re.findall(
        r"\[CONTENT\](.*?)\[/CONTENT\]", text, flags=re.DOTALL | re.IGNORECASE
    )
    if not blocks:
        return None
    return "\n\n".join(b.strip() for b in blocks if b.strip())


def append_story_entry(entry, fname, citations, stories_dir, round_number, idx):
    speaker = entry.get("speaker", "Unknown")
    raw_text = entry.get("text", "")
    turn = entry.get("message", idx)

    content = extract_tagged_content(raw_text)
    if content is None:
        # No [CONTENT] block — Phase 1 planning turn (or a turn that only
        # ran tools). Still capture any citations, but write nothing to the
        # story file.
        collect_citations(citations, entry.get("searches"))
        print(
            f"[content] No [CONTENT] block in {speaker} turn {turn} — skipping (planning-only)"
        )
        return

    cleaned = clean_speaker_text(speaker, content)
    cleaned = scrub_agent_names(cleaned)
    cleaned = strip_model_citations(cleaned)
    lines = [
        f'<small style="color:#888">_Round {round_number} · {speaker} Turn {turn}_</small>\n\n',
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


def run_editor(
    stories_dir,
    fname,
    task,
    genre,
    mediums=None,
    language="",
    details="",
    checklist=None,
):
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
                "Look at it carefully; it is part of the story. Decide the quality of image."
                "If it does not match the task expectation, flag it."
                "But never add new image or edit existing one",
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


def run_moderator(
    stories_dir,
    fname,
    task,
    genre,
    editor_path=None,
    mediums=None,
    language="",
    details="",
    checklist=None,
    
):
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
        m = re.search(r"VERDICT\s*:\s*(GREEN|RED)", result["text"], flags=re.IGNORECASE)
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
            details_spec = spec.get("details") or ""
            details = resolve_details(details_spec, task, MASTER_DETAILS)
            checklist = spec.get("checklist") or {}
            path = resolve_story_path(spec, roles)
            context = spec.get("context") or None
            print(roles)
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

            # Deterministic variety: resolve the combination (detail fields +
            # mood + genre + role + persona) and re-roll until it has not
            # already been produced in this self-chat window.
            combo = {}
            for attempt in range(4):
                relationship, mood, persona_details = pick_persona_round_robin(
                    PERSONA_POOL, genre, GENRE_PERSONA_MAP, task_roles=spec.get("roles")
                )
                detail_fields = resolve_details_fields(details_spec, task, MASTER_DETAILS)
                combo = build_combo_dict(genre, mood, persona_details, detail_fields)
                if not check_combo_used(token_a, combo):
                    break
                print(
                    f"[theme] Combination already used (attempt {attempt + 1}); "
                    f"re-rolling details/persona for variety"
                )
            else:
                print(
                    "[theme] Exhausted re-roll attempts; proceeding with the last combination"
                )
            persona = (relationship, mood, persona_details)

            # Share what has already been worked on with the agents BEFORE the
            # task starts, so they coordinate through the tracker.
            themes_block = format_theme_block(
                fetch_used_themes(token_a, scope=SELF_CHAT_THEME_SCOPE)
            )

            # Reserve this combination in the tracker before the round runs, so
            # no later round ever repeats it (even if this one fails). The
            # theme slug is built deterministically from the already-resolved
            # detail fields + mood — no LLM call needed, and combo_hash (the
            # actual dedup key) never reads this field anyway.
            theme_slug = build_theme_slug(task, mood, combo.get("details") or {})
            logged = theme_api(
                "log",
                token_a,
                operation="log",
                scope=SELF_CHAT_THEME_SCOPE,
                theme=theme_slug,
                **combo,
            )
            theme_id = (
                (logged.get("theme") or {}).get("id") if logged.get("ok") else None
            )
            if theme_id:
                print(
                    f"[theme] Reserved combination {theme_id} for round {round_number}"
                )

            print(
                f"=== Starting round {round_number}: {task} (genre: {genre}, roles: {', '.join(roles)}) ===\n"
            )
            start_time = time.time()
            transcript, session_a, session_b, fname = run_single_conversation(
                token_a,
                token_b,
                round_number,
                task,
                mediums,
                languages,
                roles,
                genre,
                details,
                checklist,
                path,
                context,
                persona=persona,
                themes_context=themes_block,
                turns=spec.get("turns"),
            )
            if theme_id:
                done = theme_api(
                    "complete",
                    token_a,
                    operation="complete",
                    theme_id=theme_id,
                )
                if done.get("ok"):
                    print(f"[theme] Marked {theme_id} completed")
                else:
                    print(
                        f"[theme] Could not mark {theme_id} completed: {done.get('error')}"
                    )
            # save_transcript(transcript, round_number)
            if not keep_sessions:
                delete_session(token_a, session_a)
                delete_session(token_b, session_b)
            round_number += 1
            task_index += 1
            elapsed = time.time() - start_time
            print(
                f"Total time elapsed in round {round_number} - {elapsed:.2f} seconds\n"
            )
            print(
                f"Autonomous organization is in vacation for {SLEEP_BETWEEN_ROUNDS} seconds"
            )
            time.sleep(SLEEP_BETWEEN_ROUNDS)
            print("Vacation over")
    except KeyboardInterrupt:
        print("\nManual Interruption")


TASKS, TASKS_SOURCE, TASK_CHECKLISTS, GENRE_PERSONA_MAP, PERSONA_POOL = load_tasks()
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