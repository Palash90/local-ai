"""Genre catalog: culture/style presets that bundle scale, instrument palettes,
rhythm groove, tempo and cadence, plus LLM-facing descriptions and cross-genre
fusion partners.

Instrument names are the PROGRAMS aliases used in the score DSL ([LANE NAME]).
Palettes are genre-authentic, but the LLM is explicitly encouraged to borrow one
lane from another genre for tasteful fusion (the `fusion` lists are hints).
"""

# groove names resolve in rhythm.py GROOVES
GENRES = {
    "jazz": {
        "desc": "Swing/bossa jazz. Warm 7th & 9th chords, walking or brushed "
                "bass, ride/snare swing, improvisatory melody. Moods: cool, smoky.",
        "scales": ["dorian", "mixolydian", "harmonicminor", "minor"],
        "tonics": ["Bb", "Eb", "F", "G", "C"],
        "tempo": (96, 168),
        "melody": ["SAX", "FLUTE", "EPIANO", "TRUMPET", "CLARINET", "VIOLIN"],
        "harmony": ["EPIANO", "NYLON", "STRINGS", "GUITAR", "ORGAN"],
        "bass": ["CONTRABASS", "EBASS", "BASS"],
        "perc": "brush_swing",
        "cadence": "jazz",
        "seventh": True,
        "hept_only": False,
        "fusion": ["bossa", "blues", "latin", "funk", "pop"],
    },
    "pop": {
        "desc": "Modern pop: bright major, four-on-floor or backbeat, synth/piano "
                "hooks, catchy repeated motif, clean 4-bar phrasing.",
        "scales": ["major"],
        "tonics": ["C", "G", "D", "A", "E"],
        "tempo": (98, 128),
        "melody": ["SYNTH", "PIANO", "EPIANO", "GUITAR", "LEAD"],
        "harmony": ["SYNTH", "STRINGS", "EPIANO", "NYLON", "PAD"],
        "bass": ["EBASS", "SYNTH", "BASS"],
        "perc": "backbeat",
        "cadence": "pop",
        "seventh": False,
        "fusion": ["funk", "disco", "rock", "edm", "rnb"],
    },
    "rock": {
        "desc": "Rock: driving straight-8ths, distorted/electric guitars, power "
                "chords, strong backbeat, anthemic chorus lift.",
        "scales": ["mixolydian", "minor", "major", "blues"],
        "tonics": ["E", "A", "D", "G"],
        "tempo": (110, 152),
        "melody": ["EGUITAR", "GUITAR", "ORGAN", "SYNTH"],
        "harmony": ["EGUITAR", "ORGAN", "NYLON"],
        "bass": ["EBASS", "BASS", "CONTRABASS"],
        "perc": "rock_8ths",
        "cadence": "authentic",
        "seventh": False,
        "fusion": ["blues", "funk", "metal", "pop"],
    },
    "edm": {
        "desc": "Electronic dance: four-on-the-floor kick, off-beat open hats, "
                "synth bass & lead, build-ups/drops, sidechain feel.",
        "scales": ["minor", "major", "dorian"],
        "tonics": ["A", "C", "E", "G"],
        "tempo": (120, 130),
        "melody": ["LEAD", "SYNTH", "PAD", "CELESTA"],
        "harmony": ["PAD", "SYNTH", "LEAD"],
        "bass": ["SYNTH", "EBASS"],
        "perc": "four_on_floor",
        "cadence": "pop",
        "seventh": False,
        "fusion": ["pop", "disco", "synthwave", "triphop"],
    },
    "hiphop": {
        "desc": "Hip-hop/trap: half-time groove, booming kick, skittering trap "
                "hats, sub bass, moody minor samples/keys, sparse melodic hook.",
        "scales": ["minor", "harmonicminor", "phrygianlike"],
        "tonics": ["C", "F", "G", "A"],
        "tempo": (70, 96),
        "melody": ["EPIANO", "PAD", "SYNTH", "MUSICBOX", "CELESTA"],
        "harmony": ["PAD", "EPIANO", "STRINGS"],
        "bass": ["SYNTH", "EBASS"],
        "perc": "trap",
        "cadence": "minor",
        "seventh": False,
        "fusion": ["rnb", "triphop", "lofi", "jazz"],
    },
    "rnb": {
        "desc": "R&B/soul: lush 7th/9th chords, warm Rhodes, smooth groove with "
                "ghost snares, mellow melismatic melody.",
        "scales": ["dorian", "major", "mixolydian"],
        "tonics": ["Eb", "F", "Bb", "C"],
        "tempo": (72, 100),
        "melody": ["EPIANO", "SYNTH", "LEAD", "CHOIR"],
        "harmony": ["EPIANO", "PAD", "STRINGS", "ORGAN"],
        "bass": ["EBASS", "FRETLESS", "SYNTH"],
        "perc": "rnb_shufle",
        "cadence": "jazz",
        "seventh": True,
        "fusion": ["soul", "jazz", "hiphop", "pop"],
    },
    "funk": {
        "desc": "Funk: syncopated 16th groove, slap/wah bass, chikenscratched "
                "guitar stabs on the 'and', bright brass hits.",
        "scales": ["mixolydian", "dorian"],
        "tonics": ["E", "A", "Bb", "C"],
        "tempo": (96, 116),
        "melody": ["EGUITAR", "BRASS", "CLAV", "SYNTH"],
        "harmony": ["EGUITAR", "CLAV", "BRASS"],
        "bass": ["EBASS", "SLAP", "SYNTH"],
        "perc": "funk_16",
        "cadence": "pop",
        "seventh": True,
        "fusion": ["disco", "soul", "rock", "hiphop"],
    },
    "blues": {
        "desc": "Blues: 12-bar feel, shuffle eighths, bent blue notes, "
                "electric guitar/harmonica phrases, walking bass.",
        "scales": ["blues", "mixolydian"],
        "tonics": ["E", "A", "G", "D"],
        "tempo": (60, 96),
        "melody": ["EGUITAR", "NYLON", "HARP", "ORGAN"],
        "harmony": ["EGUITAR", "ORGAN", "NYLON"],
        "bass": ["EBASS", "BASS", "PIANO"],
        "perc": "shuffle",
        "cadence": "blues",
        "seventh": True,
        "fusion": ["rock", "jazz", "country", "rnb"],
    },
    "japanese": {
        "desc": "Traditional Japanese (gagaku/min'yo): pentatonic hirajoshi/yo "
                "scales, koto & shamisen plucks, shakuhachi flute, sparse taiko "
                "drums, wide expressive spacing. Meditative, melancholic.",
        "scales": ["hirajoshi", "yo", "insen", "iwato"],
        "tonics": ["D", "A", "E", "G"],
        "tempo": (52, 88),
        "melody": ["SHAKUHACHI", "KOTO", "SHAMISEN", "FIDDL", "HARP"],
        "harmony": ["KOTO", "SHAMISEN", "PAD"],
        "bass": ["CELLO", "CONTRABASS", "CELLO"],
        "perc": "taiko",
        "cadence": "drone",
        "seventh": False,
        "fusion": ["ambient", "cinematic", "korean", "lofi"],
    },
    "chinese": {
        "desc": "Chinese traditional: gong/shang pentatonic (5-tone) scales, "
                "guzheng & pipa plucks, erhu-like fiddle, dizi flute, gong/cymbal "
                "and woodblock. Flowing, ornamental.",
        "scales": ["gong", "shang", "ju"],
        "tonics": ["C", "G", "D", "A"],
        "tempo": (60, 96),
        "melody": ["GUZHENG", "DIZI", "FIDDL", "KOTO", "SITAR", "OCARINA"],
        "harmony": ["GUZHENG", "KOTO", "HARP"],
        "bass": ["CELLO", "CONTRABASS"],
        "perc": "chinese_perc",
        "cadence": "drone",
        "seventh": False,
        "fusion": ["japanese", "korean", "ambient", "cinematic"],
    },
    "indian_classical": {
        "desc": "Hindustani classical: raga-based melody (Yaman/Bhairav/Kafi/"
                "Malkauns), sitar/sarod/veena & sarangi, tanpura drone on tonic+"
                "fifth, tabla theka. Slow, meandering, deeply ornamented.",
        "scales": ["yaman", "bhairav", "kafi", "bhairavi", "malkauns", "bilawal"],
        "tonics": ["C", "D", "E", "G", "A"],
        "tempo": (54, 92),
        "melody": ["SITAR", "SARANGI", "SHANAI", "VEENA", "SANTOOR", "EKTARA"],
        "harmony": ["PAD", "TAMBRA", "SANTOOR"],
        "bass": ["TAMBRA", "CELLO", "EBASS"],
        "perc": "tabla",
        "cadence": "drone",
        "seventh": False,
        "fusion": ["ambient", "jazz", "chill", "cinematic"],
    },
    "bollywood": {
        "desc": "Bollywood/film: lush strings + sitar/tabla + dholak + Western "
                "brass/guitars fused, big catchy melody, danceable groove, shifts "
                "verse<->chorus energy. Uplifting, romantic, festive.",
        "scales": ["minor", "major", "kafi", "yaman"],
        "tonics": ["C", "G", "D", "A"],
        "tempo": (90, 140),
        "melody": ["SHANAI", "SITAR", "STRINGS", "FLUTE", "SANTOOR"],
        "harmony": ["STRINGS", "SYNTH", "BRASS", "PIANO"],
        "bass": ["EBASS", "TAMBRA"],
        "perc": "dholak",
        "cadence": "pop",
        "seventh": False,
        "fusion": ["pop", "edm", "latin", "indian_classical", "funk"],
    },
    "arabic": {
        "desc": "Arabic/oud & maqam (Hijaz/Kurd/Rast): quarter-tone-flavoured "
                "melodies, oud/qanun plucks, ney flute, darbuka/riq grooves, "
                "long drone-ish pedal, ornate melisma.",
        "scales": ["hijaz", "nahawand", "kurd", "ras"],
        "tonics": ["D", "G", "C", "A"],
        "tempo": (92, 132),
        "melody": ["SANTOOR", "SITAR", "SHEHNAI", "CITHARA", "FLUTE"],
        "harmony": ["SANTOOR", "KOTO", "PAD", "HARP"],
        "bass": ["EBASS", "TAMBRA", "CELLO"],
        "perc": "darbuka",
        "cadence": "hijaz",
        "seventh": False,
        "fusion": ["turkish", "persian", "electronic", "cinematic"],
    },
    "korean": {
        "desc": "Korean traditional (gugak): pentyongjo/gyemyeonori scales, "
                "gayageum (zither) & geomungo plucks, daegeum flute, janggu drum, "
                "rhythmic 'jinyangjo' lilt. Serene, modal.",
        "scales": ["pyongjo", "gyemyonori", "hirajoshi"],
        "tonics": ["D", "G", "A"],
        "tempo": (60, 100),
        "melody": ["KOTO", "SHAMISEN", "PANFLUTE", "OCARINA", "HARP"],
        "harmony": ["KOTO", "GUZHENG", "PAD"],
        "bass": ["CELLO", "CONTRABASS"],
        "perc": "janggu",
        "cadence": "drone",
        "seventh": False,
        "fusion": ["japanese", "chinese", "pop", "kpop"],
    },
    "latin": {
        "desc": "Latin/salsa/bossa: clave & montuno piano, congas/bongos/timbales, "
                "syncopated bass tumbao, bright horns. Danceable, percussive.",
        "scales": ["major", "mixolydian", "dorian"],
        "tonics": ["C", "F", "G", "D"],
        "tempo": (96, 180),
        "melody": ["TRUMPET", "PIANO", "NYLON", "FLUTE", "SANTOOR"],
        "harmony": ["PIANO", "NYLON", "BRASS"],
        "bass": ["EBASS", "CONTRABASS"],
        "perc": "clave",
        "cadence": "pop",
        "seventh": True,
        "fusion": ["jazz", "bossa", "funk", "pop"],
    },
    "bossa": {
        "desc": "Bossa nova: soft samba feel, brushed & fingerpicked nylon guitar, "
                "warm 7th/9th jazz chords, gentle melody, understated percussion.",
        "scales": ["major", "dorian", "mixolydian"],
        "tonics": ["F", "Bb", "G", "D"],
        "tempo": (100, 130),
        "melody": ["NYLON", "EPIANO", "FLUTE", "SAX", "KOTO"],
        "harmony": ["NYLON", "EPIANO", "STRINGS"],
        "bass": ["CONTRABASS", "EBASS"],
        "perc": "bossa",
        "cadence": "jazz",
        "seventh": True,
        "fusion": ["jazz", "latin", "chill", "pop"],
    },
    "ambient": {
        "desc": "Ambient/chillout: slow-moving pads, sparse plucked motifs, "
                "reverb-heavy, little or no percussion, calm and spacious.",
        "scales": ["major", "lydian", "minpent", "dorian"],
        "tonics": ["C", "F", "A", "D"],
        "tempo": (56, 84),
        "melody": ["KOTO", "MUSICBOX", "CELESTA", "PAD", "FLUTE", "SANTOOR", "GUZHENG"],
        "harmony": ["PAD", "SYNTHSTRINGS", "CHOIR", "ORGAN"],
        "bass": ["CELLO", "EBASS", "PAD"],
        "perc": "none",
        "cadence": "drone",
        "seventh": True,
        "fusion": ["newage", "cinematic", "japanese", "lofi"],
    },
    "cinematic": {
        "desc": "Cinematic/orchestral: swelling strings & brass, big timpani/"
                "taiko hits, evolving pads, heroic or ominous arc. Wide dynamics.",
        "scales": ["minor", "harmonicminor", "dorian", "phrygian"],
        "tonics": ["D", "G", "C", "A"],
        "tempo": (60, 100),
        "melody": ["FRENCHHORN", "VIOLIN", "OBOE", "FLUTE", "CELLO"],
        "harmony": ["STRINGS", "CHOIR", "BRASS", "PAD", "ORGAN"],
        "bass": ["CONTRABASS", "CELLO", "SYNTH"],
        "perc": "orchestral",
        "cadence": "authentic",
        "seventh": True,
        "fusion": ["epic", "ambient", "indian_classical", "japanese"],
    },
}


def resolve(name):
    """Return a genre dict, tolerating light aliases / cross-spellings."""
    if not name:
        return None
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    alias = {
        "kpop": "pop", "synthwave": "edm", "disco": "funk", "soul": "rnb",
        "metal": "rock", "country": "blues", "trip-hop": "hiphop",
        "trip hop": "hiphop", "lofi": "ambient", "new age": "ambient",
        "newage": "ambient", "epic": "cinematic", "gagaku": "japanese",
        "hindustani": "indian_classical", "sitar-raga": "indian_classical",
        "maqam": "arabic", "middleeast": "arabic", "middle_east": "arabic",
        "salsa": "latin", "samba": "bossa", "afrobeat": "latin",
        "reggaeton": "latin", "phrygian": "arabic", "phrygianlike": "hiphop",
        "slap": "EBASS", "fretless": "EBASS", "clav": "PIANO", "dizi": "FLUTE",
        "harp": "HARP", "drum": "backbeat",
    }
    key = alias.get(key, key)
    return GENRES.get(key)


def genre_names():
    return sorted(GENRES)
