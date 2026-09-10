"""Standalone live harness.

    PYTHONPATH=. python3 -m server.features.music                 # paste a score
    PYTHONPATH=. python3 -m server.features.music --random        # random piece
    PYTHONPATH=. python3 -m server.features.music --genre jazz    # by genre
    PYTHONPATH=. python3 -m server.features.music --mood epic     # by mood
    PYTHONPATH=. python3 -m server.features.music --seed 7 --genre jazz
    PYTHONPATH=. python3 -m server.features.music --showcase      # audition ALL
"""
import json
import sys


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _fmt(res):
    print("engine :", res.get("engine"))
    print("tempo  :", res.get("tempo"), "bpm   duration:", res.get("duration_s"), "s")
    print("lanes  :")
    for lv in res.get("levels", []):
        tag = "drums" if lv.get("drum") else f"prog {lv.get('program')}"
        print(f"   {lv['name']:8} vol={lv['vol']:>3}  {lv['notes']:>3} notes  ({tag})")
    if res.get("structure"):
        print("form   :", ", ".join(f"{s['name']}({s['bars']}b,e={s['energy']})"
                                    for s in res["structure"]))
    if res.get("errors"):
        print("errors :", res["errors"][:8])
    if res.get("ok"):
        print("WAV    :", res["wav_path"])
        print("MID    :", res["mid_path"])


def main():
    from server.features.music.render import render_score
    if "--showcase" in sys.argv:
        from server.features.music import showcase
        user = _arg("--user", "palash")
        seed = int(_arg("--seed", "7"))
        if "--genres-only" in sys.argv:
            showcase.render_genres(user=user, seed=seed)
        elif "--instruments-only" in sys.argv:
            showcase.render_instruments(user=user)
        else:
            showcase.run_all(user=user, seed=seed)
        return
    mood = _arg("--mood")
    genre = _arg("--genre")
    seed = _arg("--seed")
    if "--random" in sys.argv or mood or genre or seed:
        from server.features.music.random_arrange import random_score
        text, tempo, info = random_score(seed=int(seed) if seed else None,
                                         mood=mood, genre=genre)
        tag = info.get("genre") or info.get("mood") or "random"
        print(f"--- {tag} arrangement: {info['key']} "
              f"{info['tempo']}bpm {info['bars']}bars [{info['structure']}] ---")
        print(text)
        print()
    else:
        print("--- Music Render Checker (paste a score; empty = demo) ---")
        try:
            text = input().strip()
        except EOFError:
            text = ""
        tempo = 120
        if not text:
            text = ("@tempo 96\n@section verse bars=2 energy=0.5\n"
                    "@section chorus bars=2 energy=1.0\n"
                    "[MELODY piano vol=90]\nC4! q E4 q G4! h | E4! e G4 e C5! q R q | "
                    "G4! q A4 q C5! h | B4 e C5 e G4! q R q |")
    res = json.loads(render_score(text, tempo))
    if not res.get("ok"):
        print(json.dumps(res, indent=2))
        return
    _fmt(res)


if __name__ == "__main__":
    main()
