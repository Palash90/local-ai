import os
import time
import json
import requests
import re
from datetime import datetime

BASE_URL = "http://localhost"
USERNAME_A = os.environ["SELF_CHAT_USER_A"]
USERNAME_B = os.environ["SELF_CHAT_USER_B"]
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 10.0
SLEEP_BETWEEN_TURNS = 2.0
MAX_MESSAGES_PER_AGENT = 5
CONVERGE_WINDOW = 2  # last N messages per agent are for convergence/finalization
AGENT_NAMES = {"A": "Kolpo", "B": "Kaya"}
STARTING_CONVERSATION = open("/home/palash/local-ai-files/self_chat.txt").read()

print(STARTING_CONVERSATION)


def login(username, password):
    resp = requests.post(
        f"{BASE_URL}/api/login",
        json={"username": username, "password": password},
        timeout=15,
    )

    resp.raise_for_status()
    return resp.json()["token"]


def create_session(token, name):
    resp = requests.post(
        f"{BASE_URL}/api/sessions",
        json={"name": name},
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


def build_input(speaker, message_number, incoming):
    current_agent = AGENT_NAMES[speaker]
    partner_agent = AGENT_NAMES["B" if speaker == "A" else "A"]
    remaining = MAX_MESSAGES_PER_AGENT - message_number

    lines = [
        f"You are {partner_agent}. You are conversing with {current_agent}.",
        f"[Message {message_number} of {MAX_MESSAGES_PER_AGENT} | {remaining} remaining]",
    ]

    if message_number == 1 and speaker == "A":
        lines.append("Opening prompt for the conversation:")
    else:
        lines.append(f"Latest message from {partner_agent}:")

    if remaining <= CONVERGE_WINDOW:
        if remaining == 1:
            lines.append(
                f"FINAL MESSAGE: Bring the work to a close with {partner_agent} "
                f"and include {STOP_PHRASE}."
            )
        else:
            lines.append(
                f"CONVERGENCE PHASE ({remaining} left): Work with {partner_agent} to "
                f"finalize your joint creation. End with {STOP_PHRASE} once complete."
            )
    else:
        lines.append(
            f"Exploration phase: Build on {partner_agent}'s last message."
        )

    lines.extend(["", "----------", incoming])
    return "\n".join(lines)


def run_single_conversation(token_a, token_b, round_number):
    session_a = create_session(token_a, f"{AGENT_NAMES['A']} round {round_number}")
    session_b = create_session(token_b, f"{AGENT_NAMES['B']} round {round_number}")

    transcript = []
    counts = {"A": 0, "B": 0}

    current_speaker = "A"
    incoming = STARTING_CONVERSATION

    try:
        while True:
            counts[current_speaker] += 1
            message_number = counts[current_speaker]
            token = token_a if current_speaker == "A" else token_b
            session = session_a if current_speaker == "A" else session_b

            prompt = build_input(current_speaker, message_number, incoming)
            reply = call_llm(token, session, prompt)

            transcript.append(
                {
                    "speaker": AGENT_NAMES[current_speaker],
                    "message": message_number,
                    "text": reply,
                }
            )

            if STOP_PHRASE in reply:
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
    fname = f"story_r{round_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

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
