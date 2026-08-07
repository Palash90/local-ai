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
    "--dir",
    default="/home/palash/local-ai-files/stories",
    help="Base directory where generated stories are saved (default: ~/local-ai-files/stories)",
)
args = parser.parse_args()
STORY_BASE_DIR = args.dir

BASE_URL = "http://localhost"
USERNAME_A = "kolpo"
USERNAME_B = "kaya"
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 10.0
SLEEP_BETWEEN_TURNS = 1.0
MAX_MESSAGES_PER_AGENT = 50
CONVERGE_WINDOW = 3  # last N messages per agent are for convergence/finalization
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
STARTING_CONVERSATION = open("/home/palash/local-ai-files/self_chat.txt").read()

TASKS_FILE = os.path.expanduser("~/local-ai-files/tasks.json")


def load_tasks():
    if not os.path.isfile(TASKS_FILE):
        print(f"Tasks file not found: {TASKS_FILE}")
        return []
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    tasks = []
    for item in data:
        task = (item.get("task") or "").strip()
        if not task:
            continue
        languages = item.get("languages") or ["English"]
        if isinstance(languages, str):
            languages = [l.strip() for l in languages.split(",")]
        mediums = item.get("mediums") or ["image", "text"]
        if isinstance(mediums, str):
            mediums = [m.strip() for m in mediums.split(",")]
        tasks.append(
            {"task": task, "languages": languages, "mediums": mediums}
        )
    return tasks


TASKS = load_tasks()
if not TASKS:
    print("No tasks to run. Add tasks to tasks.json and restart.")
    raise SystemExit(1)

user_input = input("Keep sessions {y/n} [default: n] ? ")
keep_sessions = user_input.strip().lower() == "y"

print(f"Loaded {len(TASKS)} task(s) from {TASKS_FILE}")


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


def create_session(token, name, system_prompts=None):
    body = {"name": name}
    if system_prompts:
        body["system_prompts"] = system_prompts
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


def register_agent_tokens(tokens):
    try:
        requests.post(
            f"{BASE_URL}/api/register-agent",
            json={"tokens": tokens, "usernames": [USERNAME_A, USERNAME_B]},
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
    while True:
        real = active_real_users()
        if not real:
            return
        print(
            f"[wait] Real user(s) active ({', '.join(real)}) "
            f"— pausing self-chat until they log out..."
        )
        time.sleep(POLL_INTERVAL_SECONDS)


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

        time.sleep(POLL_INTERVAL_SECONDS)


def build_input(speaker, message_number, incoming, lang, task):
    current_agent = AGENT_NAMES[speaker]
    partner_agent = AGENT_NAMES["B" if speaker == "A" else "A"]

    progress = message_number / MAX_MESSAGES_PER_AGENT

    lines = [
        f"[SYSTEM DIRECTIVE: You are responding as {current_agent}. Your partner is {partner_agent}.]\n",
        f"[Progress: {int(progress * 100)}% | Turn {message_number}/{MAX_MESSAGES_PER_AGENT}]\n",
    ]

    # Phase 1: Planning & Alignment (0% - 10%)
    if progress <= 0.10:
        lines.append(
            f"[PHASE 1: ALIGNMENT (First 10%)] Agree on the plan/approach immediately with {partner_agent} for this task: {task}."
        )
    # Phase 3: Consolidation & Wrap-up (90% - 100%)
    elif progress >= 0.90:
        if message_number == MAX_MESSAGES_PER_AGENT:
            lines.append(
                f"[PHASE 3: FINAL STEP] Consolidate output, finalize deliverables for {task}, and append {STOP_PHRASE}."
            )
        else:
            lines.append(
                f"[PHASE 3: CONVERGENCE (Final 10%)] Wrap up remaining elements of {task} with {partner_agent}. Prepare final output and append {STOP_PHRASE}."
            )
    # Phase 2: Direct Execution (10% - 90%)
    else:
        lines.append(
            f"[PHASE 2: DIRECT EXECUTION] DO NOT send meta-talk like 'waiting for data' or 'over to you'. "
            f"Every single turn MUST add new factual story content, outline details, or execute a real tool call. "
            f"Speak in {lang}."
        )

    if incoming:
        lines.extend(["", "----------", incoming])
    return "\n".join(lines)


def run_single_conversation(token_a, token_b, round_number, task, mediums, languages):
    medium = random.sample(mediums, 2 if len(mediums) > 1 else 1)
    language = random.choice(languages)

    s = STARTING_CONVERSATION.replace("%task%", task)
    s = s.replace("%mediums%", " , ".join(medium))
    s = s.replace("%_lang%", random.choice(languages))

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

    stories_dir, fname = start_story(round_number, task, mediums, languages)
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
        time.sleep(SLEEP_BETWEEN_TURNS)

    finalize_story(fname, citations)

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


def start_story(round_number, task, mediums, languages):
    base_dir = STORY_BASE_DIR
    os.makedirs(base_dir, exist_ok=True)
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    folder_name = f"{slugify(task)}_{timestamp}"
    stories_dir = os.path.join(base_dir, folder_name)
    os.makedirs(stories_dir, exist_ok=True)
    fname = os.path.join(stories_dir, f"story_r{round_number}_{timestamp}.md")
    header = [
        f"# {task}\n",
        f"*Round {round_number} · Generated on {now.strftime('%Y-%m-%d %H:%M:%S')}*\n\n",
        f"**Task prompt:** {task}\n\n",
        f"**Mediums:** {' , '.join(mediums)}  ·  **Language(s):** {' , '.join(languages)}\n\n",
        "---\n\n",
    ]
    with open(fname, "w", encoding="utf-8") as f:
        f.writelines(header)
    return stories_dir, fname


def clean_speaker_text(speaker, text):
    cleaned = re.sub(
        rf"^(kolpo|kaya|কল্প|কায়া):\s*", "", text, flags=re.IGNORECASE
    )
    cleaned = re.sub(rf"^{re.escape(speaker)}:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[NEXT TURN:\s*[^\]]*\]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.replace("[END CONVERSATION]", "").strip()


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


def append_story_entry(entry, fname, citations, stories_dir, round_number, idx):
    speaker = entry.get("speaker", "Unknown")
    cleaned = clean_speaker_text(speaker, entry.get("text", ""))
    turn = entry.get("message", idx)
    lines = [
        f"### {speaker}\n\n",
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


def run_forever():
    token_a = login(USERNAME_A, PASSWORD)
    token_b = login(USERNAME_B, PASSWORD)
    register_agent_tokens([token_a, token_b])
    print("Logged In")

    round_number = 1
    task_index = 0

    try:
        while True:
            spec = TASKS[task_index % len(TASKS)]
            task = spec["task"]
            mediums = spec["mediums"]
            languages = spec["languages"]
            print(f"=== Starting round {round_number}: {task} ===\n")
            transcript, session_a, session_b, fname = run_single_conversation(
                token_a, token_b, round_number, task, mediums, languages
            )
            # save_transcript(transcript, round_number)
            if not keep_sessions:
                delete_session(token_a, session_a)
                delete_session(token_b, session_b)
            round_number += 1
            task_index += 1
    except KeyboardInterrupt:
        print("\nManual Interruption")


if __name__ == "__main__":
    run_forever()
