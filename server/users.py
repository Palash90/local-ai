"""User account and per-user context-file helpers.

These take an already-loaded `users` dict as a parameter rather than reading
config paths or managing the load cache themselves. chat-webui.py keeps
ownership of the users-file cache (it's small, stateful, and already
monkeypatched directly by the test suite) and passes the result of its
load_users() into these on every call.
"""
import os
from datetime import datetime


def get_user_password(users, username):
    u = users.get(username)
    return u.get("password", "") if u else ""


def get_user_context_path(users, username):
    u = users.get(username)
    if u and u.get("context_file"):
        return os.path.join(u["context_file"])
    return ""


def read_user_context(users, username):
    path = get_user_context_path(users, username)
    print("Context path", path, "for", username)
    if path and os.path.exists(path):
        try:
            print("Reading", path)
            with open(path) as f:
                context = f.read()
                print(context)
                return context
        except OSError:
            return ""
    return ""


def write_user_context(users, username, content):
    path = get_user_context_path(users, username)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = read_user_context(users, username)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] {content}"
        new_content = (existing.strip() + "\n\n" + entry) if existing.strip() else entry
        with open(path, "w") as f:
            f.write(new_content)