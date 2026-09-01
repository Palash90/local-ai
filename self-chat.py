import os
import time
import json
import base64
import argparse
import requests
import re
import shutil
import threading
import traceback
from difflib import SequenceMatcher
from datetime import datetime
from urllib.parse import urlparse
import random

from server.dotenv import load_dotenv

load_dotenv()

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
# Expand ~/ so .env-style values resolve against $HOME instead of CWD
# (markdown_hosting.py applies expanduser to the same variables).
PREMIUM_STORIES_DIR = os.path.expanduser(os.getenv("STORIES_PREMIUM_DIR", ""))
ADMIN_STORIES_DIR = os.path.expanduser(os.getenv("STORIES_ADMIN_DIR", ""))

# Agents normally run on this machine next to chat-webui; override with
# SELF_CHAT_BASE_URL only when pointing them somewhere else.
BASE_URL = os.environ.get(
    "SELF_CHAT_BASE_URL", "http://localhost:3001"
).rstrip("/")
USERNAME_A = "kolpo"
USERNAME_B = "kaya"
# Each agent is a real Authentik user with its own password. Use the shared
# SELF_CHAT_PASSWORD as a fallback when the per-agent override is unset.
_SHARED_PASSWORD = os.environ.get("SELF_CHAT_PASSWORD", "")
PASSWORD_A = os.environ.get("SELF_CHAT_A_PASSWORD", _SHARED_PASSWORD)
PASSWORD_B = os.environ.get("SELF_CHAT_B_PASSWORD", _SHARED_PASSWORD)

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 5.0
SLEEP_BETWEEN_TURNS = 30.0
MAX_MESSAGES_PER_AGENT = 10
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
SELF_CHAT_PROMPT_FILE = "/home/palash/local-ai-files/self_chat.txt"
STARTING_CONVERSATION = open(SELF_CHAT_PROMPT_FILE).read()

SLEEP_BETWEEN_ROUNDS = 900

USERNAME_EDITOR = "editor"
USERNAME_MODERATOR = "moderator"
PASSWORD_EDITOR = os.environ.get("SELF_CHAT_EDITOR_PASSWORD", _SHARED_PASSWORD)
PASSWORD_MODERATOR = os.environ.get("SELF_CHAT_MODERATOR_PASSWORD", _SHARED_PASSWORD)
EDITOR_PROMPT_FILE = "/home/palash/local-ai-files/contexts/editor.txt"
MODERATOR_PROMPT_FILE = "/home/palash/local-ai-files/contexts/moderator.txt"
CRITIQUE_PROMPT_FILE = "/home/palash/local-ai-files/contexts/critique.txt"

# Kaya↔Kolpo cross-critique: at most this many retries on the same failing spot
# before giving up and letting the deterministic gate auto-RED the story.
MAX_CRITIQUE_RETRIES = 2

# Editor gate: how many times a FLAGGED story is discarded wholesale and the
# conversation restarted from scratch (0 = judge once, never restart), plus the
# default confidence threshold below which the cross-critique revision round
# fires (overridable per task via the "editor_min_confidence" config key).
MAX_EDITOR_RESTARTS = int(os.environ.get("SELF_CHAT_EDITOR_RESTARTS", "2"))
EDITOR_DEFAULT_MIN_CONFIDENCE = int(
    os.environ.get("SELF_CHAT_EDITOR_MIN_CONFIDENCE", "70")
)

# Creative alignment handshake: before the first turn, one agent reviews the
# resolved topic/tone pack (and may adjust the tone/angle), the partner
# cross-checks with veto power, and the agreed decision + both reasons are
# injected into every turn prompt. Disable with SELF_CHAT_ALIGNMENT=0.
CREATIVE_ALIGNMENT = os.environ.get("SELF_CHAT_ALIGNMENT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

EDITOR_CONTRACT = """

───────────────────────────────────────────────────────────────────────────────
FINAL GATE INSTRUCTIONS — reply with exactly this shape (nothing else):

VERDICT: CLEAN or FLAGGED
CONFIDENCE: NN/100
FLAGS:
- <the exact checklist item violated, plus where in the story>

- VERDICT is FLAGGED when ANY checklist item above is violated by the story.
- CONFIDENCE is 0-100: how confident you are that the story fully delivers the
  task, mediums and language as graded by the checklist. 100 = every item
  satisfied end-to-end; deduct for partial or shallow delivery.
- FLAGS lists each violated item (omit the FLAGS section entirely when the
  story is CLEAN).
───────────────────────────────────────────────────────────────────────────────
"""

DEFAULT_TASKS_FILE = os.path.expanduser("~/local-ai-files/tasks.json")

# All participants of the self-chat window (kolpo, kaya, editor, moderator)
# share one theme scope, so the theme is coordinated between the users of the
# window while regular per-user chats stay isolated in their own scopes.
SELF_CHAT_THEME_SCOPE = "self-chat"
SELF_CHAT_THEME_LIMIT = 30
# How many times a per-turn detail combination may be re-rolled if the theme
# tracker reports it was already used.
MAX_THEME_REROLL = 4


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
MASTER_DETAILS = load_json_file(
    os.path.expanduser("~/local-ai-files/contexts/master_details.json"), {}
)


def pick_persona_round_robin(pool, genre, genre_map, task_roles=None):
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
        research = bool(item.get("research"))
        research_turns = int(item.get("research_turns") or 1)
        if research_turns < 1:
            research_turns = 1
        editor_min_confidence = EDITOR_DEFAULT_MIN_CONFIDENCE
        if item.get("editor_min_confidence") is not None:
            try:
                editor_min_confidence = max(
                    0, min(100, int(item["editor_min_confidence"]))
                )
            except (TypeError, ValueError):
                print(
                    f"[config] Ignoring bad editor_min_confidence for task: "
                    f"{item.get('editor_min_confidence')!r}"
                )
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
                "research": research,
                "research_turns": research_turns,
                "editor_min_confidence": editor_min_confidence,
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

    count = _resolve_count(task, name, spec) if spec.get("count") is not None else None
    if count is not None and count > 1:
        if selector in ("roundrobin", "roundrobin_multi"):
            key = (task, name)
            n = len(values)
            start = _detail_cycles.get(key, 0) % n
            _detail_cycles[key] = start + count
            return [values[(start + i) % n] for i in range(min(count, n))]
        return random.sample(values, min(count, len(values)))

    if selector == "date":
        return datetime.today().strftime('%Y-%m-%d')
    
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


def _merge_value_defs(spec, master, _seen=None):
    """Resolve ``ref`` / ``refs`` against the master dictionary.

    The ``values`` across every referenced master definition are unioned
    (order-preserving and deduplicated) so a single field can draw from several
    value pools at once. Resolution is recursive: a referenced definition may
    itself carry ``ref`` / ``refs``, allowing one value set to be composed from
    others. ``_seen`` guards against reference cycles.

    As before, an explicit inline ``values`` list on the spec wins outright over
    the merged pool (mirroring the old single-``ref`` override behaviour). The
    returned dict is ready to hand to :func:`_pick_detail_value`.
    """
    if not isinstance(spec, dict):
        return spec
    _seen = set() if _seen is None else _seen

    ref = spec.get("ref")
    refs = spec.get("refs")
    if refs is not None:
        if isinstance(refs, (str, bytes)):
            refs = [refs]
        else:
            refs = list(refs)
        if ref is not None and ref not in refs:
            refs.insert(0, ref)
    elif ref is not None:
        refs = [ref]
    else:
        refs = []

    merged = dict(spec)
    if not refs:
        return merged

    values = list(spec.get("values") or [])
    selector = merged.get("selector")
    name = merged.get("name")
    count = merged.get("count")
    separator = merged.get("separator")

    for key in refs:
        if key in _seen:
            continue
        entry = master.get(key) if isinstance(master, dict) else None
        if not isinstance(entry, dict):
            continue
        sub = _merge_value_defs(entry, master, _seen | {key})
        for v in sub.get("values") or []:
            if isinstance(v, (list, tuple, dict)) or v in values:
                continue
            values.append(v)
        if selector is None:
            selector = sub.get("selector")
        if name is None:
            name = sub.get("name")
        if count is None:
            count = sub.get("count")
        if separator is None:
            separator = sub.get("separator")

    if spec.get("values") is not None:
        values = list(spec["values"])

    if spec.get("name") is not None:
        name = spec["name"]

    if values:
        merged["values"] = values
    else:
        merged.pop("values", None)
    if selector is not None:
        merged["selector"] = selector
    if name is not None:
        merged["name"] = name
    if count is not None:
        merged["count"] = count
    if separator is not None:
        merged["separator"] = separator
    merged.pop("ref", None)
    merged.pop("refs", None)
    return merged


def _pick_when_branch(table, value):
    """Select a ``when`` branch by exact match on a resolved value.

    ``*`` acts as a fallback when no exact branch matches. If the trigger value
    is a list (multi-select), any one of its elements may match.
    """
    if not isinstance(table, dict):
        return None
    if isinstance(value, (list, tuple)):
        candidates = [v for v in value if v is not None]
    elif value is not None:
        candidates = [value]
    else:
        candidates = []
    for candidate in candidates:
        if str(candidate) in table:
            return table[str(candidate)]
    if "*" in table:
        return table["*"]
    return None


def _resolve_when_spec(spec, task, master, trigger_values):
    """Resolve a spec's ``when`` conditions into a value-definition dict.

    Each ``when`` entry maps an already-resolved field name to a branch table
    (``{value: def}``). One branch is chosen per trigger field; a field with
    multiple triggers ANDs its branches together by unioning their pools. A
    missing trigger (or unmatched value without a ``*`` fallback) skips the
    field entirely by returning ``None``.
    """
    when = spec.get("when")
    if not isinstance(when, dict) or not when:
        return None

    branches = []
    for trigger, table in when.items():
        if not isinstance(table, dict):
            continue
        branch = _pick_when_branch(table, trigger_values.get(trigger))
        if branch is None:
            return None
        branches.append(branch)

    if not branches:
        return None
    if len(branches) == 1:
        return dict(branches[0]) if isinstance(branches[0], dict) else branches[0]

    refs, values = [], []
    seen_values = set()
    selector = name = count = separator = None
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        branch_refs = branch.get("refs")
        if branch_refs is None and branch.get("ref") is not None:
            branch_refs = [branch["ref"]]
        if isinstance(branch_refs, (str, bytes)):
            branch_refs = [branch_refs]
        for r in branch_refs or []:
            if r not in refs:
                refs.append(r)
        for v in branch.get("values") or []:
            if isinstance(v, (list, tuple, dict)):
                continue
            if v not in seen_values:
                seen_values.add(v)
                values.append(v)
        if selector is None:
            selector = branch.get("selector")
        if name is None:
            name = branch.get("name")
        if count is None and branch.get("count") is not None:
            count = branch.get("count")
        if separator is None and branch.get("separator") is not None:
            separator = branch.get("separator")

    merged = {}
    if refs:
        merged["refs"] = refs
    if values:
        merged["values"] = values
    for key, val in (
        ("selector", selector),
        ("name", name),
        ("count", count),
        ("separator", separator),
    ):
        if val is not None:
            merged[key] = val
    return merged or None


def _details_specs(details):
    """Normalize a ``details`` value into a list of field spec dicts."""
    if isinstance(details, list):
        return details
    if isinstance(details, dict):
        return [
            {"name": name, **(spec if isinstance(spec, dict) else {"value": spec})}
            for name, spec in details.items()
        ]
    return []


def _spec_in_filter(spec, freq_filter):
    """Whether a field spec belongs to the given change-frequency pass.

    Fields without an explicit ``change_freq`` default to ``"Per Round"``;
    only genuinely ``"Per Turn"`` fields join the per-turn re-resolution.
    """
    if not isinstance(spec, dict):
        return False
    if freq_filter is None:
        return True
    eff = str(spec.get("change_freq") or "Per Round")
    if freq_filter == "Per Round":
        return eff == "Per Round"
    if freq_filter == "Per Turn":
        return eff == "Per Turn"
    return eff == freq_filter


def _has_per_turn_details(details):
    """True if a task spec has any genuinely per-turn field."""
    return any(_spec_in_filter(s, "Per Turn") for s in _details_specs(details))


def _character_fields(details_spec):
    """Field specs flagged ``character: true`` — the story's named cast."""
    return [
        s
        for s in _details_specs(details_spec)
        if isinstance(s, dict) and s.get("character")
    ]


def _pick_character_name(task, field_name, names, skip=()):
    """Pick a character name for the round (round-robins across rounds).

    ``skip`` is a set of names already taken this round, so multi-member cast
    slots (``count > 1``) never reuse a name within the same round.
    """
    values = [
        n for n in (names or []) if isinstance(n, str) and n.strip() and n not in skip
    ]
    if not values:
        return ""
    key = (task, field_name, "<name>")
    idx = _detail_cycles.get(key, 0) % len(values)
    _detail_cycles[key] = idx + 1
    return values[idx]


def build_cast(task, details_spec, round_fields):
    """Decide and NAME the story's characters for this round.

    Returns a list of ``(label, species, name)`` triples: one per field flagged
    ``character: true``, expanded into ``count`` members when the field carries a
    count. Species come from the already-resolved per-round value pool (a list
    means a multi-member slot); names are assigned deterministically from the
    field's ``names`` list (rotating across rounds, never reused within a round),
    so the cast — including its size — is fixed and repeatable before a single
    word of story is written.
    """
    cast = []
    fields = round_fields or {}
    for spec in _character_fields(details_spec):
        label = str(spec.get("name") or "").strip()
        value = fields.get(label, spec.get("value"))
        species_list = value if isinstance(value, (list, tuple)) else [value]
        used = set()
        for species in species_list:
            species = str(species or "").strip()
            if not species:
                continue
            name = _pick_character_name(task, label, spec.get("names"), skip=used)
            if name:
                used.add(name)
            cast.append((label, species, name or species))
    return cast


def format_cast_block(cast):
    """Render the decided-and-named cast as an immutable per-turn directive."""
    if not cast:
        return ""
    lines = [
        "## Characters (already decided and named for this story — immutable)",
    ]
    for label, species, name in cast:
        lines.append(f"- {name} — the {label}, {species}")
    lines.append(
        "HARD RULE: These are the ONLY characters in this story. Never create, "
        "name, or depict any additional character in the text or in any image; "
        "every generated image must show only these named characters."
    )
    return "\n".join(lines)


_ALIGNMENT_PROPOSER_PROMPT = """
[CREATIVE ALIGNMENT — proposal phase]
The production system resolved this round's creative pack from the curated
pools. You are the first reviewer. You may keep it, or sharpen the tone and
angle — the topic itself stays as resolved.

Task: %task%
Genre / Dynamic / Tone: %genre% / %relationship% / %mood%
Resolved attributes:
%round_summary%
%cast%

Reply in EXACTLY this shape (nothing else, plain text):
DECISION: ACCEPT or ADJUST
ADJUSTED_TONE: <one line, only when ADJUST — a sharper tone/angle for the SAME topic>
REASON: <one line why>
"""

_ALIGNMENT_CHECKER_PROMPT = """
[CREATIVE ALIGNMENT — cross-check phase]
Your partner reviewed this round's creative pack:

Task: %task%
Genre / Dynamic / Tone: %genre% / %relationship% / %mood%
Resolved attributes:
%round_summary%
%cast%

Partner (%proposer%) decision: %decision%
Partner's reason: %reason%
Proposed tone adjustment: %adjusted%

Cross-check their decision. Reply in EXACTLY this shape (nothing else, plain
text):
VERDICT: AGREE or VETO
ALTERNATIVE_TONE: <one line, only when VETO — your alternative tone/angle>
REASON: <one line why>
"""


def _parse_alignment_reply(text):
    """Tolerant parse of an alignment reply (bold markers tolerated)."""
    text = (text or "").strip()
    out = {}
    m = re.search(
        r"DECISION\s*\*{0,2}\s*:\s*\*{0,2}\s*(ACCEPT|ADJUST|KEEP|CHANGE)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        word = m.group(1).upper()
        out["decision"] = "ADJUST" if word in ("ADJUST", "CHANGE") else "ACCEPT"
    m = re.search(
        r"ADJUSTED_TONE\s*\*{0,2}\s*:\s*\*{0,2}\s*(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        out["adjusted"] = m.group(1).strip()[:200]
    m = re.search(
        r"ALTERNATIVE_TONE\s*\*{0,2}\s*:\s*\*{0,2}\s*(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        out["alternative"] = m.group(1).strip()[:200]
    m = re.search(
        r"REASON\s*\*{0,2}\s*:\s*\*{0,2}\s*(.+)", text, flags=re.IGNORECASE
    )
    if m:
        out["reason"] = m.group(1).strip()[:200]
    m = re.search(
        r"VERDICT\s*\*{0,2}\s*:\s*\*{0,2}\s*(AGREE|VETO|APPROVE|DISAGREE)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        word = m.group(1).upper()
        out["verdict"] = "AGREE" if word in ("AGREE", "APPROVE") else "VETO"
    return out


def run_creative_alignment(
    proposer_name,
    proposer_token,
    proposer_session,
    checker_name,
    checker_token,
    checker_session,
    task,
    genre,
    relationship,
    mood,
    round_fields,
    cast_block,
):
    """One-round topic/tone cross-check between the two agents.

    The proposer reviews the resolved creative pack and may adjust the
    tone/angle; the partner cross-checks with veto power (a veto without an
    alternative keeps the resolved pack). Fail-open: any login/LLM/parse
    failure returns no override and no note, matching the historical behavior.
    """
    if round_fields:
        round_summary = "\n".join(
            f"- {k}: {str(v)[:120]}" for k, v in list(round_fields.items())[:12]
        )
    else:
        round_summary = "- (no round-scoped attributes resolved)"

    def _fill(template, extra=None):
        s = template.replace("%task%", task)
        s = s.replace("%genre%", genre)
        s = s.replace("%relationship%", relationship)
        s = s.replace("%mood%", mood)
        s = s.replace("%round_summary%", round_summary)
        s = s.replace("%cast%", cast_block or "")
        for k, v in (extra or {}).items():
            s = s.replace(k, str(v)[:200])
        return s

    try:
        proposal = _parse_alignment_reply(
            call_llm(
                proposer_token,
                proposer_session,
                _fill(_ALIGNMENT_PROPOSER_PROMPT),
                no_tools=True,
            )["text"]
        )
        proposal.setdefault("decision", "ACCEPT")
        decision = proposal["decision"]
        adjusted = proposal.get("adjusted", "")
        reason = proposal.get("reason", "")

        check = _parse_alignment_reply(
            call_llm(
                checker_token,
                checker_session,
                _fill(
                    _ALIGNMENT_CHECKER_PROMPT,
                    {
                        "%proposer%": proposer_name,
                        "%decision%": decision,
                        "%reason%": reason or "(none given)",
                        "%adjusted%": adjusted or "(none)",
                    },
                ),
                no_tools=True,
            )["text"]
        )
        verdict = check.get("verdict", "AGREE")

        tone_override = None
        if decision == "ADJUST" and verdict == "AGREE" and adjusted:
            tone_override = adjusted
        elif verdict == "VETO":
            tone_override = check.get("alternative") or None
        note_lines = [
            f"- {proposer_name}: {decision}"
            + (f" — {reason}" if reason else ""),
            f"- {checker_name}: {verdict}"
            + (f" — {check.get('reason', '')}" if check.get("reason") else ""),
        ]
        print(
            f"[alignment] {proposer_name}: {decision}"
            + (f" ({adjusted[:60]})" if adjusted else "")
            + f" | {checker_name}: {verdict}"
        )
        return {
            "tone_override": tone_override,
            "note": "\n".join(note_lines),
        }
    except Exception as e:
        print(f"[alignment] handshake failed (fail-open): {e}")
        return {"tone_override": None, "note": ""}


def _resolve_field_value(spec, task, master):
    """Resolve a single detail field spec into ``(name, value)``."""
    if not isinstance(spec, dict):
        return "", str(spec)

    refs = spec.get("refs")
    merged = _merge_value_defs(spec, master)

    # If name wasn't explicitly provided in spec, fall back to master's name,
    # then to the first referenced pool key.
    name = str(merged.get("name") or spec.get("name") or "").strip()
    if not name:
        first_ref = (
            refs[0] if isinstance(refs, list) and refs else spec.get("ref") or ""
        )
        name = str(first_ref or "").strip()

    value = _pick_detail_value(task, name, merged)
    return name, value


def resolve_details(
    details, task, master=None, freq_filter="Per Round", preferred=None
):
    """Resolve field specs matching a specific change frequency.

    Fields carrying a ``when`` block are resolved in a second pass, once the
    plain fields they depend on have been resolved (their values form the
    trigger map). A trigger field is resolved exactly once even though it lives
    in both passes (its result is cached).

    ``preferred`` may carry already-resolved ``{name: value}`` pairs (e.g. from
    :func:`resolve_details_fields`) that should be reused verbatim instead of
    being resolved again, so the rendered prompt text always matches the values
    that were tracked. When ``preferred`` is given the when-trigger pass is
    skipped, since every value is already final.
    """
    if master is None:
        master = MASTER_DETAILS

    if isinstance(details, str):
        return details if freq_filter == "Per Round" else ""

    specs = _details_specs(details)
    if not specs:
        rendered = "" if isinstance(details, (list, dict)) else str(details)
        return rendered if freq_filter == "Per Round" else ""

    needed_triggers = set()
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if not _spec_in_filter(spec, freq_filter):
            continue
        when = spec.get("when")
        if isinstance(when, dict) and when:
            needed_triggers.update(k for k, t in when.items() if isinstance(t, dict))

    cached = {}
    trigger_values = {}
    if preferred is None:
        for i, spec in enumerate(specs):
            if not needed_triggers:
                break
            if not isinstance(spec, dict) or spec.get("when"):
                continue
            if not _spec_in_filter(spec, freq_filter):
                continue
            name, value = _resolve_field_value(spec, task, master)
            cached[i] = (name, value)
            key = str(spec.get("name") or name)
            if key in needed_triggers:
                trigger_values[key] = value

    parts = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            if freq_filter == "Per Round":
                parts.append(str(spec))
            continue

        if not _spec_in_filter(spec, freq_filter):
            continue

        key_name = str(spec.get("name") or "").strip()
        if preferred is not None and key_name in preferred:
            name = key_name
            value = preferred[name]
            sep = spec.get("separator")
        elif spec.get("when"):
            branch = _resolve_when_spec(spec, task, master, trigger_values)
            if branch is None:
                continue
            outer_name = spec.get("name")
            eff = dict(branch) if isinstance(branch, dict) else {"value": branch}
            if outer_name:
                eff["name"] = outer_name
            name, value = _resolve_field_value(eff, task, master)
            sep = eff.get("separator")
        elif i in cached:
            name, value = cached[i]
            sep = spec.get("separator")
        else:
            name, value = _resolve_field_value(spec, task, master)
            sep = spec.get("separator")
        if not name or value is None:
            continue
        if isinstance(value, (list, tuple)):
            formatted = [_fmt_detail_value(v) for v in value]
            if not formatted:
                continue
            rendered = _join_values(formatted, sep)
        else:
            rendered = _fmt_detail_value(value)
        if not rendered:
            continue
        parts.append(f"{name}: {rendered}")

    return ", ".join(parts)


def resolve_details_fields(details, task, master=None, freq_filter=None):
    """Resolve a task's ``details`` spec into ``{field: value}`` pairs.

    Like :func:`resolve_details` but returns the raw resolved values (lists
    stay lists) keyed by field name, so the exact combination can be hashed by
    the theme tracker. A plain string returns ``{}`` (nothing to track).

    ``freq_filter`` restricts to one change-frequency pass (``"Per Round"`` /
    ``"Per Turn"``); ``None`` resolves every field (default).
    """
    if master is None:
        master = MASTER_DETAILS

    if isinstance(details, str):
        return {}

    specs = _details_specs(details)
    if not specs:
        return {}

    needed_triggers = set()
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if not _spec_in_filter(spec, freq_filter):
            continue
        when = spec.get("when")
        if isinstance(when, dict) and when:
            needed_triggers.update(k for k, t in when.items() if isinstance(t, dict))

    cached = {}
    trigger_values = {}
    for i, spec in enumerate(specs):
        if not needed_triggers:
            break
        if not isinstance(spec, dict) or spec.get("when"):
            continue
        if not _spec_in_filter(spec, freq_filter):
            continue
        name, value = _resolve_field_value(spec, task, master)
        cached[i] = (name, value)
        key = str(spec.get("name") or name)
        if key in needed_triggers:
            trigger_values[key] = value

    fields = {}

    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        if not _spec_in_filter(spec, freq_filter):
            continue
        if spec.get("when"):
            branch = _resolve_when_spec(spec, task, master, trigger_values)
            if branch is None:
                continue
            outer_name = spec.get("name")
            eff = dict(branch) if isinstance(branch, dict) else {"value": branch}
            if outer_name:
                eff["name"] = outer_name
            name, value = _resolve_field_value(eff, task, master)
        elif i in cached:
            name, value = cached[i]
        else:
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
        "hero",
        "mystery",
        "trope",
        "sweet",
        "festival",
        "animals",
        "domain",
        "topic",
        "target",
        "setting",
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
    headers = auth_headers(token)
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


def check_combo_used(token, combo, scope=SELF_CHAT_THEME_SCOPE, level="round"):
    data = theme_api(
        "check", token, operation="check", scope=scope, level=level, **combo
    )
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
        if spec.get("research"):
            print(
                f"  research:    YES — first {spec.get('research_turns') or 1} turn(s) of each agent are research-only"
            )
        else:
            print("  research:    no — direct content turns from the start")
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
        CRITIQUE_PROMPT_FILE: {
            "%genre%",
            "%mediums%",
            "%language%",
            "%details%",
            "%checklist%",
            "%cast%",
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


def _is_placeholder_query(query):
    """Detect a generic search query that grounds a citation in nothing real.

    Suitable-for/recent/kids/lighthearted phrasing (or a very short query) means
    the model searched for "something" rather than a concrete reported event, so
    the resulting citation is decorative, not grounding.
    """
    q = (query or "").strip()
    if not q or len(q) < 12:
        return True
    generic = [
        r"\brecent\b[^.,]*\bsuitable\s+for\b",
        r"\bsuitable\s+for\b",
        r"\bfor\s+(kids|children)\b",
        r"(latest|top|interesting|random|lighthearted)\s+(news|story|article|event)",
        r"\bnews\b[^.,]*\bfor\b",
    ]
    return any(re.search(p, q, re.I) for p in generic)


def _story_body_lines(text):
    """Return the story's narrative lines — everything that is neither the
    generated header/metadata block, a turn marker, an image, a horizontal
    rule, a heading, nor the citations section. An empty result means the
    story shipped with no published content at all (the empty-body failure
    mode where every turn stayed planning/research-only)."""
    body = re.sub(
        r"(?is)(?:^|\n)\s*#{1,6}\s+citations?\s*&?\s*references?\b.*$", "", text
    )
    body = re.sub(r"(?s)<small.*?</small>", "", body)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    lines = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith(("#", "**", "<!--")):
            continue
        if re.match(r"^\*[^*\s].*\*[:.,!]?\s*$", s):
            continue  # *Round N · Generated on ...* italic metadata line
        if set(s) <= set("-—=*_·|~ "):
            continue  # horizontal rules and separators
        lines.append(s)
    return lines


def verify_task_fulfillment(
    original_text, check_text, mediums, language, retrieved_citations=None
):
    """Deterministic (no-LLM) checks that catch the failure classes an editor/
    moderator LLM keeps missing: declared medium never delivered, header fields
    dropped during editing, citations dropped or fabricated, ungrounded
    citations, wrong script/language, and agent-name leaks. Returns a list of
    problem strings (empty = all good)."""
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

    if re.search(
        r"#+\s+Citations?\s*&?\s*References?", original_text
    ) and not re.search(r"#+\s+Citations?\s*&?\s*References?", check_text):
        problems.append("Editor dropped the Citations & References section.")

    if (not mediums or "text" in mediums) and not _story_body_lines(check_text):
        problems.append(
            "Story body is empty — only the header/metadata and citations were "
            "published; no narrative or deliverable content ever reached the "
            "story file."
        )

    if retrieved_citations is not None:
        published = re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", check_text)
        backed = set(retrieved_citations)
        unbacked = [u for u in published if u not in backed]
        if unbacked:
            problems.append(
                f"{len(unbacked)} citation URL(s) in the story were never retrieved by a web search."
            )
        if published and backed:
            queries = {q for _, q in retrieved_citations.values()}
            if queries and all(_is_placeholder_query(q) for q in queries):
                problems.append(
                    "Citations are ungrounded: every search used a generic placeholder "
                    "query instead of sourcing the story from a real reported event."
                )

    # Inline citations must point at the specific article that backs the
    # claim — never at a site root or section page. Checked on the story body
    # only (the auto-appended citations section may legitimately keep landing
    # pages when a search returned nothing deeper).
    story_body = strip_model_citations(check_text)
    cited_urls = set(re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", story_body))
    cited_urls |= set(re.findall(r"\[(https?://[^\]\s)]+)\]", story_body))
    landing_cites = sorted(u for u in cited_urls if _is_landing_url(u))
    if landing_cites:
        problems.append(
            f"{len(landing_cites)} inline citation URL(s) are homepage/section "
            f"pages (e.g. {landing_cites[0]}) instead of specific article links — "
            "replace each with the exact article URL that supports the claim, "
            "or remove the citation."
        )

    # One URL cannot back a dozen separate claims — that is a search-results
    # dump, not sourcing (mirrors the server critic's per-URL budget).
    max_cites = 3
    cite_counts = {}
    for u in re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", story_body):
        cite_counts[u] = cite_counts.get(u, 0) + 1
    for u in re.findall(r"\[(https?://[^\]\s)]+)\]", story_body):
        cite_counts[u] = cite_counts.get(u, 0) + 1
    over = {u: n for u, n in cite_counts.items() if n > max_cites}
    if over:
        worst = max(over.items(), key=lambda kv: kv[1])
        problems.append(
            f"{worst[1]} separate claims cite the same URL ({worst[0]}) — "
            "each claim needs its own specific article URL; search for the "
            "individual articles and cite those."
        )

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


# Authentik access tokens are short-lived (currently 5 minutes), but agents
# log in once per run and keep working for hours. The cache below keeps ONE
# current token per agent USERNAME: auth_headers() may be handed any stale
# alias of that agent and always resolves it to the latest token, refreshing
# at most once per expiry window. Callers keep their original "fixed" token
# variables for the whole run — healing happens transparently here.
#
# (Keying by token instead — as an earlier revision did — minted a fresh
# password grant on EVERY poll: the healed token was used for one request and
# discarded, so the next poll saw the stale token again and re-authenticated,
# looping indefinitely.)
_TOKEN_META = {}   # username -> {"password", "token", "exp", "granted_at"}
_TOKEN_OWNER = {}  # every issued token -> owning username (stale aliases included)
_TOKEN_LOCK = threading.RLock()
TOKEN_REFRESH_MARGIN = 60


def _decode_exp(token):
    """Expiry timestamp of a JWT access token, or 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data.get("exp", 0))
    except Exception:
        return 0


def login(username, password):
    from server.auth import oidc_password_grant

    try:
        token = oidc_password_grant(username, password)
    except Exception as e:
        print(f"[login] Authentik password grant failed for {username}: {e}")
        raise
    with _TOKEN_LOCK:
        _TOKEN_META[username] = {
            "password": password,
            "token": token,
            "exp": _decode_exp(token),
            "granted_at": time.time(),
        }
        _TOKEN_OWNER[token] = username
    return token


def _fresh_token(token):
    """Return the CURRENT token owned by whoever issued ``token``.

    Re-grants only when that token is at ``TOKEN_REFRESH_MARGIN`` seconds from
    expiry (or when its expiry is unknown and it is older than the margin).
    Unknown tokens pass through untouched.
    """
    with _TOKEN_LOCK:
        username = _TOKEN_OWNER.get(token)
        if username is None:
            # Token we did not issue ourselves — nothing to heal with.
            return token
        entry = _TOKEN_META[username]
        now = time.time()
        exp = entry["exp"]
        expired = (
            (exp and now >= exp - TOKEN_REFRESH_MARGIN)
            or (not exp and now - entry["granted_at"] >= TOKEN_REFRESH_MARGIN)
        )
        if not expired:
            # Resolve stale aliases to the agent's current token without
            # re-authenticating — this is what breaks the reauth loop.
            return entry["token"]
        print(f"[auth] {username}'s access token expired — re-authenticating")
        return login(username, entry["password"])


def auth_headers(token):
    """Headers carrying the Authentik access token as a Bearer credential."""
    with _TOKEN_LOCK:
        token = _fresh_token(token)
    return {"Authorization": f"Bearer {token}"}


def create_session(
    token, name, system_prompts=None, context_tokens=None, system_prompt=None
):
    body = {"name": name}
    if system_prompts:
        body["system_prompts"] = system_prompts
    if context_tokens:
        body["context_tokens"] = context_tokens
    if system_prompt:
        body["system_prompt"] = system_prompt
    resp = requests.post(
        f"{BASE_URL}/api/sessions",
        json=body,
        headers=auth_headers(token),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def delete_session(token, session_id):
    resp = requests.delete(
        f"{BASE_URL}/api/sessions/{session_id}",
        headers=auth_headers(token),
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


def call_llm(
    token, session_id, message, image_b64=None, no_tools=False, research=False
):
    headers = auth_headers(token)

    payload = {
        "session_id": session_id,
        "message": message,
        "client_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if args.gpu:
        payload["mode"] = "gpu"
    if image_b64:
        payload["image"] = image_b64
    if no_tools:
        payload["no_tools"] = True
    if research:
        payload["research"] = True

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
        # Re-resolve headers every poll: a long CPU generation can outlive
        # the token, and each poll must carry a still-valid bearer.
        status_resp = requests.get(
            status_url, headers=auth_headers(token), timeout=40
        )
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
    speaker,
    message_number,
    incoming,
    lang,
    task,
    context=None,
    turns=None,
    per_turn_details="",
    cast=None,
    mode="content",
    research_turns=0,
):
    current_agent = AGENT_NAMES[speaker]
    partner_agent = AGENT_NAMES["B" if speaker == "A" else "A"]

    if turns is None:
        turns = MAX_MESSAGES_PER_AGENT

    lines = [
        f"[SYSTEM DIRECTIVE: You are responding as {current_agent}. Your partner is {partner_agent}.]\n",
        f"[Turn {message_number}/{turns}]\n",
        "[SYSTEM DIRECTIVE: Place ALL meta-analysis, praise, and planning OUTSIDE the [CONTENT] tags. ",
        "The [CONTENT] block must ONLY contain clean narrative/visual deliverable text.]",
    ]

    if cast:
        lines.append(cast)

    if context is not None:
        lines.append(context)

    if mode == "research":
        # Research phase: gather and share sourced material only. Content turns
        # (which follow once BOTH agents have contributed research) write the
        # actual deliverable inside [CONTENT] using the shared materials.
        lines.append(
            "[RESEARCH MODE: This turn is for research ONLY. Perform web searches "
            "and fetch pages to gather sourced facts, figures, and material needed "
            f"for the task. Search with specific, article-targeting queries and "
            "share only article-level (deep-link) URLs — never a homepage or "
            "section page. Do NOT write any final story or deliverable content "
            "yet, and do NOT open a [CONTENT] block this turn. Write your "
            "findings and their sources in plain text so your partner can read "
            "them, then end with "
            f"[NEXT TURN: {partner_agent}]."
        )
    elif message_number <= 2:
        lines.append(
            f"Immediately establish your role and provide the first creative deliverable inside [CONTENT] tags."
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

    if mode == "content" and research_turns and message_number == research_turns + 1:
        lines.append(
            "[CONTENT MODE: Research is complete and all gathered materials are "
            "shared above. Now write the actual deliverable story using those "
            "materials, wrapping every publishable part in [CONTENT]...[/CONTENT]."
        )

    if per_turn_details:
        lines.append(f"[DYNAMIC TURN ATTRIBUTES: {per_turn_details}]")

    if incoming:
        lines.extend(["", "----------", incoming])
    return "\n".join(lines)


def _write_moderation(
    source_fname, verdict, reasons, task, genre, confidence=None,
    low_confidence=False,
):
    """Write <story>.moderation.json next to the checked story file."""
    verdict_path = source_fname.replace(".md", ".moderation.json")
    data = {
        "verdict": verdict,
        "reasons": reasons,
        "confidence": confidence,
        "task": task,
        "genre": genre,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if low_confidence:
        data["low_confidence"] = True
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[editor-gate] Moderation {verdict} saved to {verdict_path}")
    return data


def run_editor_review(
    stories_dir,
    fname,
    task,
    genre,
    mediums=None,
    language="",
    details="",
    checklist=None,
    cast="",
):
    """Editor gate: the editor agent grades the finished story against the
    task/genre checklist and returns a structured verdict.

    Returns ``{verdict, confidence, flags, reasons}`` — verdict "CLEAN" or
    "FLAGGED", confidence an int 0-100 (None when unparseable), flags the
    violated checklist items. Best-effort and fail-open: any login, prompt or
    parse failure yields a CLEAN verdict with confidence None so the
    deterministic gate remains the only hard gate. Text-only review — image
    delivery is enforced by the deterministic gate.
    """
    try:
        token = login(USERNAME_EDITOR, PASSWORD_EDITOR)
    except Exception as e:
        print(f"[editor-gate] Could not log in {USERNAME_EDITOR}: {e}")
        return {
            "verdict": "CLEAN", "confidence": None, "flags": [],
            "reasons": f"editor unavailable: {e}",
        }
    register_agent_tokens([token], [USERNAME_EDITOR])
    session_id = None
    try:
        prompt = open(EDITOR_PROMPT_FILE, encoding="utf-8").read()
        context_tokens = {
            "%genre%": genre,
            "%mediums%": ", ".join(mediums or []),
            "%language%": language or "",
            "%details%": details or "None",
            "%cast%": cast or "None",
            "%checklist%": checklist_for(genre, "editor", checklist),
        }
        for placeholder, value in context_tokens.items():
            prompt = prompt.replace(placeholder, value)
    except OSError as e:
        print(f"[editor-gate] Could not read prompt file: {e}")
        return {
            "verdict": "CLEAN", "confidence": None, "flags": [],
            "reasons": f"editor prompt unavailable: {e}",
        }
    try:
        session_id = create_session(
            token,
            "Editor gate review",
            system_prompt=prompt,
            context_tokens=context_tokens,
        )
        with open(fname, "r", encoding="utf-8") as f:
            markdown_text = f.read()
        markdown_text = sanitize_story_images(markdown_text, stories_dir)
        wait_for_user_to_leave()
        result = call_llm(
            token,
            session_id,
            "Here is the complete story markdown:\n\n"
            + markdown_text
            + "\n\nGrade this story against every checklist item."
            + EDITOR_CONTRACT,
            no_tools=True,
        )
        text = result["text"] or ""
        verdict = "CLEAN"
        # Tolerant parsing: models bold the labels ("**VERDICT**: CLEAN") and
        # sometimes drop the "/100" suffix or add "Score" — both used to fall
        # through and leave confidence None (moderation "n/a/100").
        m = re.search(
            r"VERDICT\s*\*{0,2}\s*:\s*\*{0,2}\s*(CLEAN|FLAGGED)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            verdict = m.group(1).upper()
        elif re.search(r"\bFLAGGED\b", text, flags=re.IGNORECASE):
            verdict = "FLAGGED"
        confidence = None
        m = re.search(
            r"CONFIDENCE(?:\s+SCORE|\s+LEVEL)?\s*\*{0,2}\s*:\s*\*{0,2}\s*"
            r"(\d{1,3})(?:\s*(?:/\s*100|out of\s*100|%|100))?",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            confidence = max(0, min(100, int(m.group(1))))
        else:
            print(
                "[editor-gate] no parseable CONFIDENCE line in editor reply — "
                "recorded as n/a"
            )
        flags = []
        m = re.search(
            r"FLAGS?\s*:\s*\n((?:\s*[-*•].*(?:\n|$))+)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            flags = [
                ln.strip().lstrip("-*• ").strip()
                for ln in m.group(1).strip().splitlines()
                if ln.strip().lstrip("-*• ").strip()
            ]
            flags = [
                f for f in flags
                if f.lower() not in ("none", "no flags", "n/a", "-", "—")
            ]
        if verdict == "FLAGGED" and not flags:
            flags = ["Editor flagged the story without naming the violated item"]
        print(
            f"[editor-gate] VERDICT: {verdict} | CONFIDENCE: "
            f"{confidence if confidence is not None else 'n/a'} | flags: {len(flags)}"
        )
        for fl in flags:
            print(f"[editor-gate]   - {fl}")
        return {
            "verdict": verdict,
            "confidence": confidence,
            "flags": flags,
            "reasons": text[:2000],
        }
    except Exception as e:
        print(f"[editor-gate] Editor gate failed (fail-open): {e}")
        return {
            "verdict": "CLEAN", "confidence": None, "flags": [],
            "reasons": f"editor gate error: {e}",
        }
    finally:
        if session_id and not keep_sessions:
            delete_session(token, session_id)


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
    details_spec=None,
    checklist=None,
    path=None,
    context=None,
    persona=None,
    themes_context="",
    turns=None,
    task_roles=None,
    round_fields=None,
    per_turn_task=False,
    research=False,
    research_turns=1,
    editor_min_confidence=None,
):
    """Driver: run the conversation, then apply the editor gate.

    - FLAGGED stories are discarded wholesale (sessions deleted, story files
      removed) and the conversation restarts from scratch — up to
      ``MAX_EDITOR_RESTARTS`` restarts, after which the story ships RED with
      the editor flags as reasons.
    - A CLEAN story whose confidence is below the task's threshold gets ONE
      cross-critique revision round (editor concerns passed as extra problems)
      followed by a single editor re-review; pre/post confidence is recorded.
    - Deterministic-gate violations keep the existing auto-RED semantics.
    - The editor gate itself is fail-open (editor down = CLEAN, confidence
      None), so an outage can never wedge the pipeline.

    Returns ``(transcript, session_a, session_b, fname)`` of the surviving
    attempt, matching the historical call contract.
    """
    threshold = (
        EDITOR_DEFAULT_MIN_CONFIDENCE
        if editor_min_confidence is None
        else editor_min_confidence
    )
    for attempt in range(1, MAX_EDITOR_RESTARTS + 2):
        result = _conversation_attempt(
            token_a,
            token_b,
            round_number,
            task,
            mediums,
            languages,
            roles=roles,
            genre=genre,
            details=details,
            details_spec=details_spec,
            checklist=checklist,
            path=path,
            context=context,
            persona=persona,
            themes_context=themes_context,
            turns=turns,
            task_roles=task_roles,
            round_fields=round_fields,
            per_turn_task=per_turn_task,
            research=research,
            research_turns=research_turns,
        )
        if result["red"]:
            return (
                result["transcript"],
                result["session_a"],
                result["session_b"],
                result["fname"],
            )

        check_source = result["edited_path"] or result["fname"]
        verdict = run_editor_review(
            result["stories_dir"],
            check_source,
            task,
            genre,
            mediums=result["medium"],
            language=result["language"],
            details=details,
            checklist=checklist,
        )
        confidence = verdict.get("confidence")

        if verdict["verdict"] == "FLAGGED":
            if attempt <= MAX_EDITOR_RESTARTS:
                print(
                    f"[editor-gate] FLAGGED with {len(verdict['flags'])} flag(s) — "
                    f"discarding the session and starting fresh "
                    f"(restart {attempt}/{MAX_EDITOR_RESTARTS})"
                )
                if not keep_sessions:
                    delete_session(token_a, result["session_a"])
                    delete_session(token_b, result["session_b"])
                for stale in (result["fname"], result["edited_path"]):
                    if stale:
                        try:
                            os.remove(stale)
                            print(f"[editor-gate] Removed discarded story file {stale}")
                        except OSError:
                            pass
                continue
            _write_moderation(
                check_source,
                "RED",
                "Editor gate flagged the story after "
                f"{MAX_EDITOR_RESTARTS} fresh restart(s):\n"
                + "\n".join(f"- {f}" for f in verdict["flags"]),
                task,
                genre,
                confidence=confidence,
            )
            return (
                result["transcript"],
                result["session_a"],
                result["session_b"],
                result["fname"],
            )

        # CLEAN so far — apply the confidence threshold.
        if confidence is not None and confidence < threshold:
            print(
                f"[editor-gate] CLEAN but confidence {confidence}/100 < "
                f"{threshold} — triggering the cross-critique revision round"
            )
            revised_path = run_cross_critique(
                result["stories_dir"],
                result["fname"],
                task,
                genre,
                token_a,
                result["session_a"],
                token_b,
                result["session_b"],
                mediums=result["medium"],
                language=result["language"],
                details=details,
                checklist=checklist,
                citations=result["citations"],
                extra_problems=[
                    f"Editor confidence {confidence}/100 is below the required "
                    f"{threshold}. Strengthen the weakest checklist areas without "
                    "changing anything that already complies."
                ],
            )
            final_source = revised_path or check_source
            re_verdict = run_editor_review(
                result["stories_dir"],
                final_source,
                task,
                genre,
                mediums=result["medium"],
                language=result["language"],
                details=details,
                checklist=checklist,
            )
            final_confidence = re_verdict.get("confidence")
            note = (
                f"Editor gate: CLEAN. Confidence after revision: "
                f"{pre if (pre := confidence) is not None else 'n/a'}/100 → "
                f"{final_confidence if final_confidence is not None else 'n/a'}"
                f"/100 (threshold {threshold})."
            )
            if re_verdict["verdict"] == "FLAGGED":
                _write_moderation(
                    final_source,
                    "RED",
                    "Editor re-review flagged the story after the revision "
                    "round:\n"
                    + "\n".join(f"- {f}" for f in re_verdict["flags"]),
                    task,
                    genre,
                    confidence=final_confidence,
                )
            else:
                _write_moderation(
                    final_source,
                    "GREEN",
                    note,
                    task,
                    genre,
                    confidence=final_confidence,
                    low_confidence=(
                        final_confidence is None or final_confidence < threshold
                    ),
                )
            return (
                result["transcript"],
                result["session_a"],
                result["session_b"],
                result["fname"],
            )

        # CLEAN and confident (or confidence unknown — fail-open).
        _write_moderation(
            check_source,
            "GREEN",
            f"Editor gate: CLEAN with confidence "
            f"{confidence if confidence is not None else 'n/a'}/100 "
            f"(threshold {threshold}).",
            task,
            genre,
            confidence=confidence,
        )
        return (
            result["transcript"],
            result["session_a"],
            result["session_b"],
            result["fname"],
        )
    # Defensive: the loop always returns; keep a sane fallback anyway.
    raise RuntimeError("editor gate loop exited without a verdict")


def _conversation_attempt(
    token_a,
    token_b,
    round_number,
    task,
    mediums,
    languages,
    roles=None,
    genre="General",
    details="",
    details_spec=None,
    checklist=None,
    path=None,
    context=None,
    persona=None,
    themes_context="",
    turns=None,
    task_roles=None,
    round_fields=None,
    per_turn_task=False,
    research=False,
    research_turns=1,
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

    # Decide and name the story's characters once per round, before any turn.
    cast_block = format_cast_block(build_cast(task, details_spec, round_fields))
    if cast_block:
        print(f"[cast] {cast_block.splitlines()[1]} ...")

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

    session_a = create_session(
        token_a,
        f"{AGENT_NAMES['A']} round {round_number}",
        system_prompt=s,
    )
    session_b = create_session(
        token_b,
        f"{AGENT_NAMES['B']} round {round_number}",
        system_prompt=s,
    )

    # Creative alignment handshake: one agent reviews the resolved topic/tone
    # pack (may adjust the tone/angle), the partner cross-checks with veto
    # power. The agreed decision + both reasons are injected into every turn
    # prompt. Fail-open; proposer alternates by round parity.
    if CREATIVE_ALIGNMENT:
        proposer_is_a = round_number % 2 == 0
        handshake = run_creative_alignment(
            AGENT_NAMES["A"] if proposer_is_a else AGENT_NAMES["B"],
            token_a if proposer_is_a else token_b,
            session_a if proposer_is_a else session_b,
            AGENT_NAMES["B"] if proposer_is_a else AGENT_NAMES["A"],
            token_b if proposer_is_a else token_a,
            session_b if proposer_is_a else session_a,
            task,
            genre,
            relationship,
            mood,
            round_fields,
            cast_block,
        )
        if handshake.get("tone_override") or handshake.get("note"):
            lines = ["[AGREED CREATIVE DIRECTION — jointly cross-checked before turn 1]"]
            if handshake.get("tone_override"):
                lines.append(
                    f"TONE DIRECTIVE (overrides the default tone): {handshake['tone_override']}"
                )
            lines.append(handshake["note"])
            alignment_block = "\n".join(lines)
            cast_block = (
                f"{cast_block}\n\n{alignment_block}" if cast_block else alignment_block
            )
            print(f"[alignment] {alignment_block.splitlines()[0]} injected into turn prompts")

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

        per_turn_str = ""
        turn_theme_id = None
        if per_turn_task:
            # Resolve this turn's per-turn detail fields, re-rolling until the
            # FULL combination (round scope fields + per-turn fields + mood +
            # persona) has not already been produced. Round scope fields were
            # resolved once for the whole round, so the character never changes
            # mid-story.
            turn_fields = {}
            for attempt in range(MAX_THEME_REROLL):
                turn_fields = resolve_details_fields(
                    details_spec, task, MASTER_DETAILS, freq_filter="Per Turn"
                )
                combo = build_combo_dict(
                    genre,
                    mood,
                    persona_details,
                    {**(round_fields or {}), **turn_fields},
                )
                if not check_combo_used(token_a, combo, level="turn"):
                    break
                print(
                    f"[theme] Turn combination already used (attempt {attempt + 1}); "
                    f"re-rolling per-turn details"
                )
            else:
                print(
                    "[theme] Exhausted per-turn re-roll attempts; proceeding with the last combination"
                )
            per_turn_str = resolve_details(
                details_spec,
                task,
                MASTER_DETAILS,
                freq_filter="Per Turn",
                preferred=turn_fields,
            )
            turn_slug = build_theme_slug(
                task, mood, {**(round_fields or {}), **turn_fields}
            )
            logged = theme_api(
                "log",
                token_a,
                operation="log",
                scope=SELF_CHAT_THEME_SCOPE,
                level="turn",
                theme=turn_slug,
                **combo,
            )
            if logged.get("ok"):
                turn_theme_id = (logged.get("theme") or {}).get("id")
                print(f"[theme] Reserved turn combination {turn_theme_id}")
        # Two-phase flow for research tasks: the first research_turns of EACH
        # agent are research-only (gather + share sourced material, no
        # [CONTENT] block), then the agents switch to content mode and write
        # the deliverable using every piece of research that was shared.
        in_research_phase = research and message_number <= research_turns
        mode = "research" if in_research_phase else "content"
        eff_research_turns = research_turns if research else 0
        prompt = build_input(
            current_speaker,
            message_number,
            "" if not transcript else incoming,
            language,
            task,
            context,
            turns,
            per_turn_details=per_turn_str,
            cast=cast_block,
            mode=mode,
            research_turns=eff_research_turns,
        )

        wait_for_user_to_leave()

        result = call_llm(
            token,
            session,
            prompt,
            image_b64=shared_image_b64,
            research=in_research_phase,
        )
        reply = result["text"]
        if not reply.strip():
            prompt += "\n[SYSTEM ERROR: Your previous output was empty. Generate real story content now.]"
            result = call_llm(
                token,
                session,
                prompt,
                image_b64=shared_image_b64,
                research=in_research_phase,
            )
            reply = result["text"]
            if not reply.strip():
                if turn_theme_id:
                    theme_api(
                        "complete",
                        token_a,
                        operation="complete",
                        theme_id=turn_theme_id,
                    )
                    print(f"[theme] Marked turn {turn_theme_id} completed")
                print(
                    f"Round {round_number} ended: {AGENT_NAMES[current_speaker]} "
                    f"returned no content after a retry\n"
                )
                break
        if is_duplicate(reply, incoming):
            # Re-prompt agent to generate new content instead of repeating
            prompt += "\n[SYSTEM ERROR: Your previous output was identical to your partner's. Generate unique content now.]"
            result = call_llm(
                token,
                session,
                prompt,
                image_b64=shared_image_b64,
                research=in_research_phase,
            )
            reply = result["text"]

        if in_research_phase:
            # A research turn that ends without a single article-level URL
            # degenerates into homepage citations for every claim in the
            # deliverable. Re-prompt for targeted searches (bounded) before
            # accepting the turn.
            for _ in range(2):
                if _turn_has_deep_source(result.get("searches")):
                    break
                print(
                    f"[research] {AGENT_NAMES[current_speaker]} turn "
                    f"{message_number}: no article-level URLs in search "
                    "results — re-prompting for targeted searches"
                )
                result = call_llm(
                    token,
                    session,
                    prompt + "\n" + _DEEP_SOURCE_PROMPT,
                    image_b64=shared_image_b64,
                    research=True,
                )
                reply = result["text"]
                if not reply.strip():
                    break

        if turn_theme_id:
            theme_api("complete", token_a, operation="complete", theme_id=turn_theme_id)
            print(f"[theme] Marked turn {turn_theme_id} completed")

        entry = {
            "speaker": AGENT_NAMES[current_speaker],
            "message": message_number,
            "text": reply,
            "image": result.get("image"),
            "searches": result.get("searches"),
            "publish": not in_research_phase,
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
            # Mirror collect_citations(): when the reports contain article
            # links, don't hand the partner homepage/section URLs to cite.
            deep_shared = _turn_has_deep_source(shared_searches)
            for s in shared_searches:
                if not isinstance(s, dict):
                    continue
                query = s.get("query", "")
                block.append(f"- Query: {query}")
                for r in s.get("results") or []:
                    title = r.get("title") or r.get("url") or ""
                    url = r.get("url", "")
                    if deep_shared and _is_landing_url(url):
                        continue
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

    finalize_story(fname, stories_dir, citations)

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

    print("=== Cross-critique phase (Kaya↔Kolpo self-verify) ===")
    edited_path = run_cross_critique(
        stories_dir,
        fname,
        task,
        genre,
        token_a,
        session_a,
        token_b,
        session_b,
        mediums=medium,
        language=language,
        details=details,
        checklist=checklist,
        cast=cast_block,
        citations=citations,
    )

    print("=== Deterministic verification ===")
    with open(fname, "r", encoding="utf-8") as f:
        original_text = f.read()
    check_source = edited_path if edited_path else fname
    with open(check_source, "r", encoding="utf-8") as f:
        check_text = f.read()
    problems = verify_task_fulfillment(
        original_text, check_text, medium, language, citations
    )

    red = bool(problems)
    if problems:
        print(
            f"[verify] {len(problems)} problem(s) found — auto-RED, skipping editor gate:"
        )
        for p in problems:
            print(f"[verify]   - {p}")
        _write_moderation(
            check_source,
            "RED",
            "Automatic RED (deterministic check, no LLM call):\n"
            + "\n".join(f"- {p}" for p in problems),
            task,
            genre,
        )

    return {
        "transcript": transcript,
        "session_a": session_a,
        "session_b": session_b,
        "fname": fname,
        "stories_dir": stories_dir,
        "medium": medium,
        "language": language,
        "citations": citations,
        "edited_path": edited_path,
        "check_source": check_source,
        "problems": problems,
        "red": red,
    }


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
        return os.path.expanduser(path)

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
    # Date bucket layer <genre>/<YYYY-MM-DD>/<story>/ so the hosting index can
    # group stories chronologically (e.g. "August 2026").
    bucket_dir = os.path.join(genre_dir, now.strftime("%Y-%m-%d"))
    os.makedirs(bucket_dir, exist_ok=True)
    stories_dir = os.path.join(bucket_dir, folder_name)
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


def strip_image_markers(text):
    """Drop model-authored image placeholder/reference marker lines.

    The agents occasionally write standalone notes such as ``**(Image
    Reference: the squid from the last turn)**`` or ``**(Image Placeholder:
    /output/...png)**`` instead of (or alongside) a real ``![...](...)``
    embed. These add nothing to the published story, so the whole line is
    removed. Only lines that ARE the marker are stripped — real image embeds
    (`![alt](file)`) and surrounding prose are left untouched.
    """
    pattern = re.compile(
        r"(?im)^\s*\*{0,2}\s*[\[\(]\s*image\s+(?:reference|placeholder|ref|ph)"
        r"\s*:[^\]\)]*[\]\)]\s*\*{0,2}\s*$"
    )
    stripped = pattern.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


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


_LANDING_SEGMENT_RE = re.compile(
    r"^(section|sections|category|categories|topics?|tags?|archive|index)$"
)

# URL path segments that mark a site-internal search/category/listing page —
# never an individual article ("flipkart.com/q/fashion-tops",
# "nykaafashion.com/women/tops/c/4497", "meesho.com/tops-ladies/pl/3ja").
_STRUCTURAL_PATH_SEGMENTS = {
    "q", "s", "c", "pl", "pr", "p", "products", "product", "category",
    "categories", "collection", "collections", "shop", "store", "search",
    "browse", "listings", "listing", "tagged",
}


def _is_landing_url(url):
    """True for site roots and section/landing pages rather than single articles.

    Search engines answer generic "top news" queries with outlet homepages and
    section pages (``/``, ``/technology/``, ``/section/technology``). Such a URL
    can never back a specific claim, so it is only useful as a citation when
    the same search returned nothing better (see ``collect_citations``).
    """
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
    except (ValueError, AttributeError):
        return False
    path = path.rstrip("/")
    if not path:
        return True
    segs = [s for s in path.split("/") if s]
    if not segs:
        return True
    if any(seg.lower() in _STRUCTURAL_PATH_SEGMENTS for seg in segs):
        return True
    if len(segs) == 1:
        seg = segs[0].lower()
        # One clean segment with no file extension and no digits ("/technology")
        # is a section page; real article slugs almost always carry a date, an
        # ID, or a hyphenated multi-part path.
        if "." not in seg and not re.search(r"\d", seg) and len(seg) <= 40:
            return True
    if _LANDING_SEGMENT_RE.match(segs[0].lower()):
        return True
    return False


def collect_citations(citations, searches):
    for s in searches or []:
        if not isinstance(s, dict):
            continue
        query = s.get("query", "")
        results = [r for r in (s.get("results") or []) if isinstance(r, dict)]
        found = [(r.get("url", ""), r.get("title") or "") for r in results]
        # Prefer article-level links: when one search returned both deep
        # article URLs and landing pages, only the deep links can source a
        # claim — the landing pages are dropped from that search's citations.
        has_deep = any(url and not _is_landing_url(url) for url, _ in found)
        for url, title in found:
            if not url or url in citations:
                continue
            if has_deep and _is_landing_url(url):
                print(f"[citations] Dropping landing-page result: {url}")
                continue
            citations[url] = (title or url, query)


def strip_model_citations(text):
    """Remove any Citations & References block the model wrote into a turn.

    Only finalize_story() may emit that section, and it is built exclusively from
    web-search results. Model-authored variants (images, placeholder links, double
    hashes, bare "## References" headings, localized headings like
    "## संदर्भ (References)") are dropped so they can never leak into the
    published section.
    """
    text = re.sub(
        r"(?i)(?:^|\n)\s*#{1,6}\s+(?:citations?\s*(?:&|and)?\s*)?references?\b.*$",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?i)(?:^|\n)\s*#{1,6}\s+sources?\s*(?:&|and)?\s*(?:references?)?\b.*$",
        "",
        text,
        flags=re.DOTALL,
    )
    # Localized heading with the English word in parentheses:
    # "## संदर्भ (References)" / "## रेफरेंस (Citations)"
    text = re.sub(
        r"(?i)(?:^|\n)\s*#{1,6}\s+[^(\n]{0,40}\(\s*(?:citations?|references?|sources?)\s*\).*$",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


_VERIFICATION_CHROME_RE = re.compile(
    r"(?s)<details\s+class=\"source-verification\".*?</details>\s*"
)


def strip_verification_chrome(text):
    """Remove the server-appended source-verification <details> block from a
    reply. It is UI chrome for the chat transcript — never story content — but
    an unclosed [CONTENT] capture would otherwise sweep it into the story."""
    return _VERIFICATION_CHROME_RE.sub("", text or "")


def extract_tagged_content(text):
    """Return only the text inside [CONTENT] blocks.

    The closing tag may appear as ``[/CONTENT]`` (as instructed in the system
    prompt) or as the ``[END CONTENT]`` variant the models actually emit; both
    are accepted, so a turn's narrative is never mistaken for planning chatter.
    Returns None if no [CONTENT] block is present at all — the caller treats
    that as a planning-only turn with nothing to publish. Multiple blocks are
    concatenated in order.

    As a defensive fallback, a message that OPENS with ``[CONTENT]`` but is
    truncated or never closes the tag (the research mode used to provoke this)
    still yields its narrative: everything from the ``[CONTENT]`` marker up to
    the next structural tag (``[NEXT TURN:`` / ``[END CONVERSATION]`` /
    ``[IMAGE GENERATION CALL:]``) or the end of the message."""
    blocks = re.findall(
        r"\[CONTENT\](.*?)(?:\[/CONTENT\]|\[END CONTENT\]|\[END\]|$)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not blocks:
        # Fallback for an unclosed [CONTENT] block. Only trigger when the
        # message clearly starts with the marker, so a 0-block Phase-1 planning
        # turn (no [CONTENT] at all) is still treated as nothing to publish.
        m = re.match(r"(?is)\s*\[CONTENT\]\s*(.*)$", text)
        if m:
            rest = m.group(1)
            rest = re.split(
                r"(?is)\s*\[(?:NEXT TURN\s*:|END CONVERSATION\]|IMAGE GENERATION CALL\s*:|THEME LOGGED\s*:|IMAGE SHARED\s*:)",
                rest,
                maxsplit=1,
            )[0]
            rest = rest.strip()
            blocks = [rest] if rest else []
    if not blocks:
        return None
    return "\n\n".join(b.strip() for b in blocks if b.strip())


def _turn_has_deep_source(searches):
    """True if a research turn's tool trail contains at least one
    article-level (deep-link) URL — the kind a specific claim can cite."""
    for s in searches or []:
        if not isinstance(s, dict):
            continue
        if s.get("tool") == "fetch_page":
            url = s.get("url", "")
            if url and not _is_landing_url(url):
                return True
            continue
        for r in s.get("results") or []:
            if isinstance(r, dict):
                url = r.get("url") or r.get("link") or ""
                if url and not _is_landing_url(url):
                    return True
    return False


_DEEP_SOURCE_PROMPT = (
    "[SYSTEM ERROR: Your searches returned no article-level source URLs "
    "(empty result sets, homepages, or section pages only). Do NOT hand off "
    "yet. Run new web_search calls with specific, article-targeting queries "
    "(exact event/story names, or site + topic) until your results include "
    "2-3 article URLs (deep links, not homepages), then share your findings "
    "with those exact URLs.]"
)


def beautify_inline_citations(text):
    """Convert chat-lane citation syntax into proper markdown links.

    Agents write research-style inline citations — ``(Author, Venue, Year)
    [url]`` — and occasionally doubled ``[url](url)`` variants. In the story
    markdown that renders as raw bracketed text or a link whose label is the
    URL itself. Rewrite every variant to ``[Author, Venue, Year](url)`` so the
    published page shows a clean, clickable citation.
    """
    # (meta) [label](url)  ->  [meta](url)   (covers the doubled [url](url))
    text = re.sub(
        r"\(([^()]{2,80})\)\s*\[[^\]]*\]\((https?://[^)\s]+)\)",
        r"[\1](\2)",
        text,
    )
    # (meta) [url]  ->  [meta](url)
    text = re.sub(
        r"\(([^()]{2,80})\)\s*\[(https?://[^\]\s)]+)\]",
        r"[\1](\2)",
        text,
    )

    # Bare [url] with no metadata  ->  [host](url)
    def _bare(m):
        url = m.group(1)
        host = urlparse(url).netloc or url
        return f"[{host}]({url})"

    text = re.sub(r"\[(https?://[^\]\s)]+)\]", _bare, text)
    return text


def append_story_entry(entry, fname, citations, stories_dir, round_number, idx):
    speaker = entry.get("speaker", "Unknown")
    raw_text = strip_verification_chrome(entry.get("text", ""))
    turn = entry.get("message", idx)

    content = extract_tagged_content(raw_text)

    # Research-phase turns gather and share material; they must never publish
    # narrative or images, but their web-search results still feed citations.
    # Exception: if the model wrote a [CONTENT] block anyway (observed when
    # judge retries keep a turn in the research phase and the model crams the
    # whole deliverable into it), publishing nothing would silently lose the
    # entire story body — so the explicit [CONTENT] block always wins.
    if entry.get("publish") is False and content is None:
        collect_citations(citations, entry.get("searches"))
        print(
            f"[content] {speaker} turn {turn} — research phase, citations captured only"
        )
        return
    if entry.get("publish") is False and content:
        print(
            f"[content] {speaker} turn {turn} — research phase opened a "
            "[CONTENT] block; publishing it to avoid losing the story body"
        )

    if content is None:
        # No [CONTENT] block — Phase 1 planning turn (or a turn that only
        # ran tools). Still capture any citations. If the turn generated an
        # image, embed it anyway so a generated image is never lost from the
        # story just because the model skipped the [CONTENT] wrapper.
        collect_citations(citations, entry.get("searches"))
        local_img = embed_story_image(
            entry.get("image"), stories_dir, round_number, speaker, idx
        )
        if not local_img:
            print(
                f"[content] No [CONTENT] block in {speaker} turn {turn} — skipping (planning-only)"
            )
            return
        print(
            f"[content] No [CONTENT] block in {speaker} turn {turn} — embedding generated image only"
        )
        lines = [
            f'<small style="color:#888">_Round {round_number} · {speaker} Turn {turn}_</small>\n\n',
            f"![{speaker}]({local_img})\n\n",
        ]
        with open(fname, "a", encoding="utf-8") as f:
            f.writelines(lines)
        return

    cleaned = clean_speaker_text(speaker, content)
    cleaned = scrub_agent_names(cleaned)
    cleaned = strip_model_citations(cleaned)
    cleaned = beautify_inline_citations(cleaned)
    cleaned = strip_image_markers(cleaned)
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


def finalize_story(fname, stories_dir, citations):
    sanitize_story_file(fname, stories_dir)
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


def sanitize_story_images(text, stories_dir):
    """Drop markdown image references whose target file does not exist.

    The story agents and the editor sometimes emit ``![...](path)`` lines that
    point at hallucinated filenames or ComfyUI ``/output/`` URLs that were never
    copied into the story folder (real embedded copies always use the
    ``img_rN_Speaker_idx.ext`` scheme). Removing those references keeps the
    published markdown free of dead/broken image tags.
    """

    def _fix(match):
        ref = match.group(1)
        fname = os.path.basename(ref.split("?")[0].split("#")[0])
        if not fname:
            return match.group(0)
        if os.path.isfile(os.path.join(stories_dir, fname)):
            return match.group(0)
        print(f"[images] Dropping broken image reference: {ref}")
        return ""

    return re.sub(r"!\[[^\]]*\]\(([^)]*)\)", _fix, text)


_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")


def _image_anchors(markdown_text):
    """Map each image filename to the narrative paragraph right before it.

    Story files store one turn per entry as ``<small> label, paragraph(s),
    image``; the paragraph adjacent to an image is the scene that image
    illustrates, which is what re-anchoring matches against when the editor
    regroups image references.
    """
    anchors = {}
    pending = []
    for line in markdown_text.splitlines():
        if _IMAGE_LINE_RE.match(line):
            anchor = ""
            for block in reversed(pending):
                if "".join(block).strip():
                    anchor = "\n".join(block)
                    break
            for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", line):
                fn = os.path.basename(ref)
                anchors[fn] = anchor
            pending = []
            continue
        if line.strip().startswith("<small") and line.strip().endswith("</small>"):
            pending = []
            continue
        if not line.strip():
            pending.append([])
            continue
        if pending:
            pending[-1].append(line)
        else:
            pending.append([line])
    return anchors


def _paragraph_overlap_score(a, b):
    """Token Jaccard overlap between two text blocks (0..1)."""

    def _toks(s):
        return set(re.findall(r"[a-z0-9]+", s.lower()))

    left, right = _toks(a), _toks(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def reanchor_story_images(revised, original, stories_dir):
    """Keep every image embedded inline, right after the narrative it illustrates.

    The editor is free to polish prose but sometimes regroups all image
    references at the top or bottom of the story. This re-anchors each image (in
    the order it appeared in the original story) to the revised paragraph whose
    wording most resembles the turn text the image originally followed, so
    images reliably stay embedded inside the story flow.
    """
    ordered = [fn for fn, _ in story_images_in_order(stories_dir, original)]
    if not ordered:
        return revised
    anchors = _image_anchors(original)
    if not anchors:
        return revised

    lines = revised.splitlines()
    ref_by_fn = {}
    for line in lines:
        if _IMAGE_LINE_RE.match(line):
            ref = re.search(r"!\[[^\]]*\]\(([^)]+)\)", line).group(1)
            ref_by_fn.setdefault(
                os.path.basename(ref).split("?")[0].split("#")[0], line
            )
    missing = [fn for fn in ordered if fn not in ref_by_fn]
    if missing:
        print(
            f"[images] Re-anchor: {len(missing)} reference(s) missing from revision: {missing}"
        )
        return revised

    blocks = []
    cur = []
    for line in lines:
        if _IMAGE_LINE_RE.match(line) or not line.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append(cur)
    block_texts = ["\n".join(b).strip() for b in blocks]

    attach = []
    prev = 0
    for fn in ordered:
        anchor = anchors.get(fn, "")
        best, score = -1, -1.0
        for i in range(prev, len(block_texts)):
            s = _paragraph_overlap_score(block_texts[i], anchor)
            if s > score:
                score, best = s, i
        if best < 0:
            best = min(prev, len(block_texts) - 1)
        attach.append(best)
        prev = best + 1

    out = []
    for i, block in enumerate(blocks):
        out.extend(block)
        for k, fn in enumerate(ordered):
            if attach[k] == i:
                out.append("")
                out.append(ref_by_fn[fn])
        out.append("")
    return "\n".join(out).strip() + "\n"


def sanitize_story_file(fname, stories_dir):
    """Rewrite a story markdown file in place, dropping broken image references.

    Used defensively after the round and after the editor phase so a dead image
    tag can never reach the hosted page."""
    try:
        with open(fname, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"[images] Could not read {fname}: {e}")
        return
    cleaned = sanitize_story_images(text, stories_dir)
    if cleaned != text:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(cleaned)
        print(f"[images] sanitized {os.path.basename(fname)}")


def extract_markdown_fence(text):
    match = re.search(
        r"```(?:markdown|md)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    return text.strip()


def run_cross_critique(
    stories_dir,
    fname,
    task,
    genre,
    token_a,
    session_a,
    token_b,
    session_b,
    mediums=None,
    language="",
    details="",
    checklist=None,
    cast="",
    citations=None,
    retries=MAX_CRITIQUE_RETRIES,
    extra_problems=None,
):
    """Kaya↔Kolpo cross-critique of the finished story (research-style self-verify).

    The deterministic gate (verify_task_fulfillment) runs on the authored story
    first — the cheap, no-LLM check. When it is clean, the slow LLM re-write is
    skipped entirely and the story the two agents wrote is kept as-is. When
    violations exist, the two agents become the verifiers: each retry has one of
    them (rotating Kaya/Kolpo) review their partner's copy, name the exact spot
    of every violation, and return a corrected markdown where ONLY those spots
    changed. The gate re-runs after every attempt; residual problems after
    ``retries`` attempts surface as an auto-RED (never a silently shipped story).
    ``extra_problems`` merges caller-supplied concerns (e.g. a low editor
    confidence) into the gate's list so a clean-deterministic story can still be
    revised.

    Returns the path to the ``.edited.md`` file, or ``None`` to keep the original.
    """
    try:
        with open(fname, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"[critique] Could not read story {fname}: {e}")
        return None

    def _gate(check_text):
        return verify_task_fulfillment(
            text, check_text, mediums, language, retrieved_citations=citations
        )

    problems = _gate(text)
    for extra in extra_problems or []:
        if extra not in problems:
            problems.append(extra)
    if not problems:
        print("[critique] PASS — no deterministic violations, skipping LLM rewrite")
        return None

    try:
        prompt = open(CRITIQUE_PROMPT_FILE, encoding="utf-8").read()
        context_tokens = {
            "%genre%": genre,
            "%mediums%": ", ".join(mediums or []),
            "%language%": language or "",
            "%details%": details or "None",
            "%checklist%": checklist_for(genre, "editor", checklist),
            "%cast%": cast or "None",
        }
        for placeholder, value in context_tokens.items():
            prompt = prompt.replace(placeholder, value)
    except OSError as e:
        print(f"[critique] Could not read prompt file: {e}")
        return None

    edited_path = fname.replace(".md", ".edited.md")
    partners = [
        (AGENT_NAMES["A"], token_a),
        (AGENT_NAMES["B"], token_b),
    ]
    for attempt in range(max(1, retries)):
        name, token = partners[attempt % len(partners)]
        print(
            f"[critique] Attempt {attempt + 1}/{max(1, retries)} by {name}: "
            f"{len(problems)} residual violation(s)"
        )
        for p in problems:
            print(f"[critique]   - {p}")
        try:
            session_id = create_session(
                token, f"Cross-critique attempt {attempt + 1}", system_prompt=prompt
            )
        except Exception as e:
            print(f"[critique] {name} could not start a critique session: {e}")
            break
        try:
            wait_for_user_to_leave()
            result = call_llm(
                token,
                session_id,
                "Here is the complete story markdown:\n\n"
                + text
                + "\n\nVerification violations to resolve:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\nFollow your WORK MODE exactly: quote each violation's spot, "
                "then return your CRITIQUE comment and the complete corrected "
                "markdown in a single ```markdown code block, changing only the "
                "flagged spots.",
                no_tools=True,
            )
            revised = extract_markdown_fence(result["text"])
            if revised:
                revised = scrub_agent_names(revised)
                revised = beautify_inline_citations(revised)
                revised = normalize_markdown_lines(revised)
                revised = sanitize_story_images(revised, stories_dir)
                revised = strip_image_markers(revised)
                revised = reanchor_story_images(revised, text, stories_dir)
            if not revised:
                print(f"[critique] {name} returned no markdown; keeping original")
                continue
            residual = _gate(revised)
            if not residual:
                with open(edited_path, "w", encoding="utf-8") as f:
                    f.write(revised + "\n")
                print(f"[critique] PASS after {name}'s retry — saved {edited_path}")
                return edited_path
            text = revised
            problems = residual
        except Exception as e:
            print(f"[critique] Attempt {attempt + 1} failed: {e}")
        finally:
            if not keep_sessions:
                delete_session(token, session_id)

    print(f"[critique] FAIL after retries — {len(problems)} residual violation(s):")
    for p in problems:
        print(f"[critique]   - {p}")
    return None


def run_editor(
    stories_dir,
    fname,
    task,
    genre,
    mediums=None,
    language="",
    details="",
    checklist=None,
    cast="",
):
    """Editor phase: review images + markdown, write story_rN_ts.edited.md."""
    try:
        token = login(USERNAME_EDITOR, PASSWORD_EDITOR)
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
            "%cast%": cast or "None",
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
        system_prompt=prompt,
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
        revised = sanitize_story_images(revised, stories_dir)
        revised = strip_image_markers(revised) if revised else revised
        revised = reanchor_story_images(revised, markdown_text, stories_dir)
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
    cast="",
):
    """Moderator phase: GREEN/RED verdict, written to story_rN_ts.moderation.json."""
    try:
        token = login(USERNAME_MODERATOR, PASSWORD_MODERATOR)
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
            "%cast%": cast or "None",
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
        system_prompt=prompt,
        context_tokens=context_tokens,
    )
    try:
        source = editor_path if editor_path else fname
        with open(source, "r", encoding="utf-8") as f:
            markdown_text = f.read()
        markdown_text = sanitize_story_images(markdown_text, stories_dir)
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
    token_a = login(USERNAME_A, PASSWORD_A)
    token_b = login(USERNAME_B, PASSWORD_B)
    register_agent_tokens([token_a, token_b], [USERNAME_A, USERNAME_B])
    print("Logged In")

    round_number = 1
    task_index = 0

    try:
        while True:
            spec = TASKS[task_index % len(TASKS)]
            # Re-register on every task: a chat-webui restart wipes the
            # in-memory agent registry, and without this the pipeline would
            # silently fall back to the GPU lane until the next self-chat
            # restart.
            register_agent_tokens([token_a, token_b], [USERNAME_A, USERNAME_B])
            task = spec["task"]
            mediums = spec["mediums"]
            languages = spec["languages"]
            roles = spec.get("roles") or ["free"]
            genre = spec.get("genre") or "General"
            details_spec = spec.get("details") or ""
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

            # Round-scoped fields (Per Round, the default when change_freq is
            # absent) resolve once here and stay fixed for every turn; only
            # genuinely per-turn fields re-resolve each turn inside the story.
            round_fields = resolve_details_fields(
                details_spec, task, MASTER_DETAILS, freq_filter="Per Round"
            )
            details = resolve_details(
                details_spec,
                task,
                MASTER_DETAILS,
                freq_filter="Per Round",
                preferred=round_fields,
            )
            per_turn_task = _has_per_turn_details(details_spec)

            # Deterministic variety: for round-scoped tasks, resolve the
            # combination (round detail fields + mood + genre + role + persona)
            # and re-roll the persona until it has not already been produced in
            # this self-chat window. Tasks with genuine per-turn details skip
            # round-level reservation — variety is enforced turn-by-turn inside
            # run_single_conversation, which also pins the identity fields.
            combo = {}
            for attempt in range(4):
                relationship, mood, persona_details = pick_persona_round_robin(
                    PERSONA_POOL, genre, GENRE_PERSONA_MAP, task_roles=spec.get("roles")
                )
                if per_turn_task:
                    break
                combo = build_combo_dict(genre, mood, persona_details, round_fields)
                if not check_combo_used(token_a, combo):
                    break
                print(
                    f"[theme] Combination already used (attempt {attempt + 1}); "
                    f"re-rolling persona for variety"
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
            theme_id = None
            if not per_turn_task:
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
            try:
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
                    details_spec,
                    checklist,
                    path,
                    context,
                    persona=persona,
                    themes_context=themes_block,
                    turns=spec.get("turns"),
                    round_fields=round_fields,
                    per_turn_task=per_turn_task,
                    research=spec.get("research"),
                    research_turns=spec.get("research_turns") or 1,
                    editor_min_confidence=spec.get("editor_min_confidence"),
                )
            except Exception as e:
                traceback.print_exc()
                print(
                    f"[error] Round {round_number} failed for task '{task}': {e}\n"
                    f"        Skipping to the next task so the flow keeps running."
                )
            else:
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
