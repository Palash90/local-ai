import os
import time
import json
import requests
import re
from datetime import datetime
import random


BASE_URL = "http://localhost"
USERNAME_A = os.environ["SELF_CHAT_USER_A"]
USERNAME_B = os.environ["SELF_CHAT_USER_B"]
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 30.0
SLEEP_BETWEEN_TURNS = 2.0
MAX_MESSAGES_PER_AGENT = 6
CONVERGE_WINDOW = 3  # last N messages per agent are for convergence/finalization
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
STARTING_CONVERSATION = open("/home/palash/local-ai-files/self_chat.txt").read()    

user_input = input("Enter Detailed task: ")
task = user_input

user_input = input("Enter comma-separated mediums: ")
mediums = user_input.split(',')

user_input = input("In which language Kaya and Kolpo should have conversation (comma separated values):")
languages = user_input.split(',')

user_input = input("Keep sessions {y/n} ?")
keep_sessions = user_input.strip() == 'y'

print(task, mediums, languages)


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
        print(f"Warning: could not delete session {session_id} (HTTP {resp.status_code})")
        return False
    print(f"Deleted session {session_id}")
    return True


def call_llm(token, session_id, message):
    headers = {"X-Auth-Token": token}

    submit_respo = requests.post(
        f"{BASE_URL}/api/chat",
        json={
            "session_id": session_id,
            "message": message,
            "client_timestamp": datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
        },
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
            return data["response"]
        if status == "error":
            raise RuntimeError(f"Task failed: {data}")

        time.sleep(POLL_INTERVAL_SECONDS)


def build_input(speaker, message_number, incoming, lang):
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
            f"[PHASE 1: ALIGNMENT (First 10%)] Agree on the plan/approach immediately with {partner_agent} based on %task%."
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
            f"[PHASE 2: EXECUTION] Work on {task} directly without meta-talk. "
            f"Communicate in {lang} if required by the task."
        )

    if incoming:
        lines.extend(["", "----------", incoming])
    return "\n".join(lines)


def run_single_conversation(token_a, token_b, round_number):
    medium = random.sample(mediums, 2 if len(mediums) > 1 else 1)
    language = random.choice(languages)
                           
    s = STARTING_CONVERSATION.replace("%task%", task)
    s = s.replace(
        "%mediums%",
        " , ".join(medium))
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

    try:
        while True:
            counts[current_speaker] += 1
            message_number = counts[current_speaker]
            token = token_a if current_speaker == "A" else token_b
            session = session_a if current_speaker == "A" else session_b

            prompt = build_input(
                current_speaker, message_number, "" if not transcript else incoming, language
            )
            reply = call_llm(token, session, prompt)

            transcript.append(
                {
                    "speaker": AGENT_NAMES[current_speaker],
                    "message": message_number,
                    "text": reply,
                }
            )

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
            current_speaker = "B" if current_speaker == "A" else "A"
            time.sleep(SLEEP_BETWEEN_TURNS)
    finally:
        if keep_sessions:
            pass
        else:
            delete_session(token_a, session_a)
            delete_session(token_b, session_b)

    return transcript


def save_transcript(transcript, round_number):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"conv_r{round_number}_{timestamp}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(transcript, f, indent=4)
    print(f"Saved transcript to {fname}")
    return fname


def save_markdown_story(transcript, round_number):
    fname = f"/home/palash/local-ai-files/stories/story_r{round_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    markdown_lines = [
        f"# Collaborative Story — Round {round_number}\n",
        f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n",
        "---\n\n",
    ]

    for entry in transcript:
        speaker = entry.get("speaker", "Unknown")
        text = entry.get("text", "")

        # Clean leading speaker prefixes and ending tokens
        cleaned_text = re.sub(
            rf"^{re.escape(speaker)}:\s*", "", text, flags=re.IGNORECASE
        )
        cleaned_text = cleaned_text.replace("[END CONVERSATION]", "").strip()

        markdown_lines.append(f"### {speaker}\n\n{cleaned_text}\n\n---\n\n")

    with open(fname, "w", encoding="utf-8") as f:
        f.writelines(markdown_lines)

    print(f"Saved story to {fname}")


def run_forever():
    token_a = login(USERNAME_A, PASSWORD)
    token_b = login(USERNAME_B, PASSWORD)
    print("Logged In")

    round_number = 1

    try:
        while True:
            print(f"=== Starting round {round_number} ===\n")
            transcript = run_single_conversation(token_a, token_b, round_number)
            # save_transcript(transcript, round_number)
            save_markdown_story(transcript, round_number)
            round_number += 1
    except KeyboardInterrupt:
        print("\nManual Interruption")


if __name__ == "__main__":
    run_forever()
