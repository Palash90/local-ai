import os
import time
import json
import requests
from datetime import datetime

BASE_URL = "http://localhost"
USERNAME = os.environ["SELF_CHAT_USER"]
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 10.0
SLEEP_BETWEEN_TURNS = 5.0
STARTING_CONVERSATION = (
    "Pick any topic you find interesting and start a conversation about it."
)


def login():
    resp = requests.post(
        f"{BASE_URL}/api/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=15,
    )

    resp.raise_for_status()
    return resp.json()["token"]


def create_session(token, name):
    resp = requests.post(
        f"{BASE_URL}/api/sessions",
        json={"name": name},
        headers={"X-Auth-Token": token},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["session_id"]

def call_llm(token, session_id, message):
    headers={"X-Auth-Token": token}

    submit_respo = requests.post(
        f"{BASE_URL}/api/chat",
        json={
            "session_id": session_id,
            "message": message,
            "client_timestamp": datetime.now().astimezone().isoformat(timespec="seconds")
        },
        headers=headers,
        timeout=30
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
            return RuntimeError(f"Task failed {data}:")

        time.sleep(POLL_INTERVAL_SECONDS)

def run_single_conversation(token, round_number):
    session_a = create_session(token, f"Agent A round {round_number}")
    session_b = create_session(token, f"Agent B round {round_number}")

    print(session_a)
    print(session_b)

    transcript = []

    opening_reply = call_llm(token, session_a, STARTING_CONVERSATION)
    transcript.append({"speaker": "A", "text": opening_reply})

    current_speaker = "B"

    while True:
        last_mesage = transcript[-1]["text"]
        other_session = session_b if current_speaker == "B" else session_a
        reply = call_llm(token, other_session, last_mesage)

        print(f"{current_speaker}: {reply}\n")
        transcript.append({"speaker": current_speaker, "text": reply})

        if STOP_PHRASE in reply:
            print(f"Round {round_number} ended by agent\n")
            break

        current_speaker = "B" if current_speaker == "A" else "A"
        time.sleep(SLEEP_BETWEEN_TURNS)

    return transcript

def save_transcript(transcript, round_number):
    fname=f"conv_r{round_number}_{datetime.now()}.json"
    with open(fname, "w") as f:
        json.dump(transcript, f, indent=4)
    print(f"Saved transcript to {fname}")

def run_forever():
    token = login()
    print("Logged In")

    round_number = 1

    try:
        while True:
            print(f"=== Starting round {round_number} ===\n")
            transcript = run_single_conversation(token, round_number)
            save_transcript(transcript, round_number)
            round_number += 1
    except KeyboardInterrupt:
        print("\nManual Interruption")

if __name__ == "__main__":
    run_forever()