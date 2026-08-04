import os
import time
import json
import requests
from datetime import datetime

BASE_URL = "http://localhost"
USERNAME_A = os.environ["SELF_CHAT_USER_A"]
USERNAME_B = os.environ["SELF_CHAT_USER_B"]
PASSWORD = os.environ["SELF_CHAT_PASSWORD"]

STOP_PHRASE = "[END CONVERSATION]"
POLL_INTERVAL_SECONDS = 10.0
SLEEP_BETWEEN_TURNS = 5.0
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


def run_single_conversation(token_a, token_b, round_number):
    session_a = create_session(token_a, f"Spudnik round {round_number}")
    session_b = create_session(token_b, f"Kaya round {round_number}")

    transcript = []

    # Track turns explicitly
    current_speaker = "A"
    next_input = STARTING_CONVERSATION

    while True:
        token = token_a if current_speaker == "A" else token_b
        session = session_a if current_speaker == "A" else session_b

        # Get response from active speaker
        reply = call_llm(token, session, next_input)

        transcript.append({"speaker": current_speaker, "text": reply})

        if STOP_PHRASE in reply:
            print(f"Round {round_number} ended by agent {current_speaker}\n")
            break

        # Pass current reply as input to next speaker
        next_input = reply
        current_speaker = "B" if current_speaker == "A" else "A"
        time.sleep(SLEEP_BETWEEN_TURNS)

    return transcript


def save_transcript(transcript, round_number):
    fname = f"conv_r{round_number}_{datetime.now()}.json"
    with open(fname, "w") as f:
        json.dump(transcript, f, indent=4)
    print(f"Saved transcript to {fname}")


def run_forever():
    token_a = login(USERNAME_A, PASSWORD)
    token_b = login(USERNAME_B, PASSWORD)
    print("Logged In")

    round_number = 1

    try:
        while True:
            print(f"=== Starting round {round_number} ===\n")
            transcript = run_single_conversation(token_a, token_b, round_number)
            save_transcript(transcript, round_number)
            round_number += 1
    except KeyboardInterrupt:
        print("\nManual Interruption")


if __name__ == "__main__":
    run_forever()
