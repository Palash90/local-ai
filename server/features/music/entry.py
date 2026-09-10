"""Chat entry for the temporary 'make music' shortcut (no LLM involved).

Replaced soon by a real LLM `generate_music` tool call; until then the
orchestration `start` branch diverts here on a strict 'make music' prefix.
"""

import json


def _render_make_music(task_id, sid, user_message, user):
    from server.features.state import M
    from server.features.music.random_arrange import random_score
    from server.features.music.render import render_score

    M.set_status(task_id, "Composing music...")
    score_text, tempo, info = random_score()
    try:
        res = json.loads(render_score(score_text, tempo, user=user))
    except Exception as e:
        M._set_task_error(task_id, f"Music render failed: {e}", sid)
        return
    if not res.get("ok"):
        M._set_task_error(task_id, f"Music render failed: {res.get('error')}", sid)
        return
    music_url = res.get("music_url", "")
    rel = music_url[len("/music/"):] if music_url.startswith("/music/") else None
    with M._data_lock:
        t = M.tasks.get(task_id)
        if t is None:
            return
        t["music_file"] = rel
        t["music_score"] = res.get("score", score_text)
        t["music_url"] = music_url
        t["music_levels"] = res.get("levels", [])
        t.setdefault("_tools_used", []).append("generate_music")
    lanes = ", ".join(info.get("lanes", []))
    reply = (
        f"Here's a **{info.get('mood', '')}** piece in "
        f"**{info.get('key', '')}** ({info.get('tempo', 0)} BPM) — "
        f"{info.get('structure', '')} · {res.get('duration_s', 0)}s across {lanes}. "
        f"Press play below!"
    )
    M._finalize_task(task_id, sid, reply, {})
