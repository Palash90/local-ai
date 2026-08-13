import json
import os

FILE_NAME = "nested_schema_template.json"

def load_existing_schema():
    if os.path.exists(FILE_NAME):
        try:
            with open(FILE_NAME, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {
        "tasks_entry": [],
        "genre_persona_map_entry": {},
        "persona_pool_entry": {},
        "genre_checklists_entry": {}
    }

def generate_nested_schema():
    data = load_existing_schema()

    print("=== Interactive Deep-Merging Schema Builder ===")

    while True:
        genre = input("\nEnter Genre (e.g., Regional Fashion): ").strip()
        if not genre:
            break

        # 1. Append Task if not already present
        task_exists = any(t.get("genre") == genre for t in data["tasks_entry"])
        if not task_exists:
            data["tasks_entry"].append({
                "task": f"Sample Task for {genre}",
                "details": "Provide detailed task instructions here.",
                "genre": genre,
                "roles": ["free"],
                "languages": ["English", "bengali"],
                "mediums": ["text", "image"]
            })

        # 2. Init Genre-Persona Map & Checklist
        if genre not in data["genre_persona_map_entry"]:
            data["genre_persona_map_entry"][genre] = []

        if genre not in data["genre_checklists_entry"]:
            data["genre_checklists_entry"][genre] = {
                "editor": ["Rule 1...", "Rule 2..."],
                "moderator": ["RED if rule 1 fails.", "RED if rule 2 fails."]
            }

        # 3. Add Personas for this Genre
        while True:
            print(f"\n--- Adding Persona for Genre: '{genre}' ---")
            relationship = input("Enter Relationship Category (e.g., Artisans): ").strip()
            mood = input("Enter Mood Dynamic (e.g., Subtle Admirers): ").strip()

            # Merge Relationship to Genre Map
            if relationship not in data["genre_persona_map_entry"][genre]:
                data["genre_persona_map_entry"][genre].append(relationship)

            # Deep Merge Persona Pool
            if relationship not in data["persona_pool_entry"]:
                data["persona_pool_entry"][relationship] = {}

            data["persona_pool_entry"][relationship][mood] = {
                "Kaya": {
                    "role": "Role A",
                    "persona": "Description of Kaya's persona"
                },
                "Kolpo": {
                    "role": "Role B",
                    "persona": "Description of Kolpo's persona"
                }
            }

            another_persona = input(f"Add another persona for '{genre}'? (y/n): ").strip().lower()
            if another_persona != 'y':
                break

        another_genre = input("\nAdd another Genre? (y/n): ").strip().lower()
        if another_genre != 'y':
            break

    # Save merged data
    with open(FILE_NAME, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"\nSuccessfully merged and saved into: {FILE_NAME}")

if __name__ == "__main__":
    generate_nested_schema()