"""
Conjugation data layer for Étozi.   [BUILD rev 2026-06-13av]

rev r: negation & reflexive flags in sentence specs — student must produce
"ne...pas" and the reflexive pronoun themselves (hint shows "négatif"/"réfléchi").
Removed written negation/explétif "ne" from frames. Answer subject now agrees
with the sentence (il↔elle). Accepts full + no-subject; "ne" can't be dropped.
rev p: prompt shows "je/j'"; tense/pronoun selection from widget keys.
rev n: verbecc 2.0 returns 9 forms (je tu il elle on nous vous ils elles).

Strategy (in order):
  1. Look in the SQLite cache (table `verb_cache`) — instant, offline.
  2. Call the hosted verbe.cc REST API.
  3. If the API is unreachable/errors, fall back to the local `verbecc`
     library (pip install verbecc) — same data, same model.
  4. Whatever produced the result, cache the full conjugation JSON so the
     verb is only ever fetched/generated once.

The cached unit is the ENTIRE conjugation table for one verb (all moods,
tenses, pronouns) — one row per verb. That matches how both the API and the
library return data and keeps lookups to a single query.

Public functions:
  conjugate_verb(verb)            -> full dict {mood: {tense: [6 forms]}} or None
  get_form(verb, mood, tense, i)  -> single conjugated string (e.g. "tu manges")
  is_irregular(verb)              -> True/False best-effort
  PRONOUNS                        -> the 6 subject pronouns in order
"""

import json
import sqlite3
import urllib.request
import urllib.parse

REV = "av"  # build revision — shown in the app sidebar to verify file sync
DB_PATH = "frenchflow.db"
API_BASE = "http://verbe.cc/vcfr/conjugate/fr/"
API_TIMEOUT = 3  # seconds; short so a dead server fails fast instead of hanging

# Order in which conjugation sources are tried for an uncached verb.
# Library-first because the public verbe.cc demo currently 404s; this avoids
# a per-verb network timeout. Switch to ("api", "library") if you host your
# own verbecc-svc and point API_BASE at it.
SOURCE_ORDER = ("library", "api")

# The 6 standard subject pronouns, in the order verbecc returns them.
PRONOUNS = ["je", "tu", "il/elle", "nous", "vous", "ils/elles"]

# ── Map Étozi's MOODS display names → verbecc keys ───────────────────────
# verbecc moods are lowercase; tenses are lowercase, hyphenated, accented.
MOOD_KEY = {
    "Indicatif": "indicatif",
    "Subjonctif": "subjonctif",
    "Conditionnel": "conditionnel",
    "Impératif": "imperatif",
}

# (mood, Étozi tense) -> verbecc tense key
TENSE_KEY = {
    ("Indicatif", "Présent"):           "présent",
    ("Indicatif", "Passé composé"):     "passé-composé",
    ("Indicatif", "Imparfait"):         "imparfait",
    ("Indicatif", "Plus-que-parfait"):  "plus-que-parfait",
    ("Indicatif", "Passé simple"):      "passé-simple",
    ("Indicatif", "Passé antérieur"):   "passé-antérieur",
    ("Indicatif", "Futur simple"):      "futur-simple",
    ("Indicatif", "Futur antérieur"):   "futur-antérieur",
    ("Subjonctif", "Présent"):          "présent",
    ("Subjonctif", "Passé"):            "passé",
    ("Subjonctif", "Imparfait"):        "imparfait",
    ("Subjonctif", "Plus-que-parfait"): "plus-que-parfait",
    ("Conditionnel", "Présent"):                "présent",
    ("Conditionnel", "Passé première forme"):   "passé",
    # verbecc exposes a single conditional "passé"; the rare 2nd form reuses it
    ("Conditionnel", "Passé deuxième forme"):   "passé",
    # Impératif has only 3 forms (tu, nous, vous) — handled specially
    ("Impératif", "Présent"):  "imperatif-présent",
    ("Impératif", "Passé"):    "imperatif-passé",
}


# ── Cache table ──────────────────────────────────────────────────────────
def ensure_cache_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verb_cache (
            verb        TEXT PRIMARY KEY,
            data        TEXT NOT NULL,   -- JSON: {mood: {tense: [forms]}}
            source      TEXT,            -- 'api' | 'library'
            irregular   INTEGER DEFAULT 0,
            cached_at   TEXT
        )
    """)


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    ensure_cache_table(conn)
    return conn


# ── Source 1: hosted API ──────────────────────────────────────────────────
def _fetch_from_api(verb):
    """Return verbecc's `moods` dict for `verb`, or None on any failure."""
    url = API_BASE + urllib.parse.quote(verb.strip())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Etozi/1.0"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["value"]["moods"]
    except Exception:
        return None


# ── Source 2: local library fallback ───────────────────────────────────────
# verbecc 2.0 was a backwards-incompatible rewrite. The return shape and the
# mood/tense key language changed between versions, so we try several access
# patterns and normalise to French-keyed {mood: {tense: [6 forms]}}.

# Map verbecc 2.0 English mood/tense keys → the French keys the rest of the
# app uses (matching the old API / pre-2.0 shape).
_EN_MOOD = {
    "indicative": "indicatif", "subjunctive": "subjonctif",
    "conditional": "conditionnel", "imperative": "imperatif",
    "infinitive": "infinitif", "participle": "participe",
}
_EN_TENSE = {
    "present": "présent", "imperfect": "imparfait",
    "future": "futur-simple", "simple-past": "passé-simple",
    "perfect": "passé-composé", "pluperfect": "plus-que-parfait",
    "future-perfect": "futur-antérieur", "anterior-past": "passé-antérieur",
    "past": "passé",
    "imperative-present": "imperatif-présent",
    "imperative-past": "imperatif-passé",
}

def _frenchify_keys(moods):
    """Translate English mood/tense keys to French if needed; pass through
    if already French."""
    out = {}
    for mood, tenses in moods.items():
        fmood = _EN_MOOD.get(mood, mood)
        out[fmood] = {}
        for tense, forms in tenses.items():
            ftense = _EN_TENSE.get(tense, tense)
            out[fmood][ftense] = forms
    return out

_lib_conjugator = None
_lib_wraps_moods = True
_lib_error = None

def _make_conjugator():
    """Instantiate whatever conjugator class this verbecc version exposes.
    verbecc 2.0+ uses CompleteConjugator; older versions use Conjugator.
    Returns (callable_conjugate, wraps_in_moods_bool) or raises ImportError."""
    import verbecc
    # 2.0+ : CompleteConjugator, returns the moods dict DIRECTLY (no 'moods' key)
    if hasattr(verbecc, "CompleteConjugator"):
        cc = verbecc.CompleteConjugator("fr")
        return cc.conjugate, False
    # older : Conjugator, returns {'verb':..., 'moods': {...}}
    if hasattr(verbecc, "Conjugator"):
        cg = verbecc.Conjugator(lang="fr")
        return cg.conjugate, True
    # very old : verbecc.conjugator.Conjugator
    if hasattr(verbecc, "conjugator") and hasattr(verbecc.conjugator, "Conjugator"):
        cg = verbecc.conjugator.Conjugator(lang="fr")
        return cg.conjugate, True
    raise ImportError("no usable conjugator class found in verbecc")


def _extract_form(entry):
    """Pull the plain conjugated string out of one verbecc entry (str, list, or
    ConjugationData dict/obj where the form lives under 'c'). "" if not found."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        c = entry.get("c")
        if isinstance(c, (list, tuple)):
            return c[0] if c else ""
        if isinstance(c, str):
            return c
    for attr in ("conjugation", "c"):
        if hasattr(entry, attr):
            v = getattr(entry, attr)
            if isinstance(v, (list, tuple)):
                return v[0] if v else ""
            if isinstance(v, str):
                return v
    try:
        c = entry["c"]
        if isinstance(c, (list, tuple)):
            return c[0] if c else ""
        if isinstance(c, str):
            return c
    except Exception:
        pass
    try:
        first = entry[0]
        if isinstance(first, str) and len(first) > 1:
            return first
        if isinstance(first, (list, tuple)) and first:
            return str(first[0])
    except Exception:
        pass
    return ""


def _all_french_mood_tense_keys():
    pairs, seen = [], set()
    for (mood_disp, tense_disp), tkey in TENSE_KEY.items():
        mkey = MOOD_KEY.get(mood_disp)
        if mkey and (mkey, tkey) not in seen:
            seen.add((mkey, tkey))
            pairs.append((mkey, tkey))
    return pairs


def _extract_moods_by_subscript(result):
    """verbecc 2.0 CompleteConjugation object → {mood:{tense:[forms]}}.
    Positions preserved (empty → "") so pronoun indices never shift."""
    moods, any_ok = {}, False
    for mkey, tkey in _all_french_mood_tense_keys():
        try:
            cell = result[mkey][tkey]
        except Exception:
            continue
        if not cell:
            continue
        forms = [_extract_form(c) for c in cell]
        if any(f for f in forms):
            moods.setdefault(mkey, {})[tkey] = forms
            any_ok = True
    return moods if any_ok else None


def _fetch_from_library(verb):
    """French-keyed moods dict via local verbecc, or None. Handles verbecc 2.0+
    (CompleteConjugation object, ConjugationData entries) and older dicts."""
    global _lib_conjugator, _lib_wraps_moods, _lib_error
    verb = verb.strip()
    try:
        if _lib_conjugator is None:
            _lib_conjugator, _lib_wraps_moods = _make_conjugator()
        result = _lib_conjugator(verb)

        moods = None
        if isinstance(result, dict):
            moods = result.get("moods", result)
        elif hasattr(result, "moods"):
            try:
                moods = dict(result.moods)
            except Exception:
                moods = None

        if isinstance(moods, dict) and moods:
            try:
                clean = {}
                for m, tn in moods.items():
                    clean[str(m)] = {}
                    for t, forms in dict(tn).items():
                        clean[str(m)][str(t)] = [_extract_form(c) for c in forms]
                return _frenchify_keys(clean)
            except Exception:
                pass

        built = _extract_moods_by_subscript(result)
        if built:
            return built

        _lib_error = f"unexpected verbecc result shape: {type(result)}"
        return None
    except Exception as e:  # noqa
        _lib_error = f"{type(e).__name__}: {e}"
        return None


def library_diagnostic(verb="manger"):
    """Run the library on one verb and return a human-readable status string.
    Used by the settings screen to surface WHY conjugation failed."""
    global _lib_error
    _lib_error = None
    moods = _fetch_from_library(verb)
    if moods is None:
        try:
            import verbecc
            ver = getattr(verbecc, "__version__", "unknown")
        except Exception:
            ver = "not installed"
        return f"verbecc échec (version {ver}) — {_lib_error or 'aucune donnée'}"
    # success: report a sample form (flattened)
    try:
        sample = moods.get("indicatif", {}).get("présent", ["?"])[0]
        if isinstance(sample, (list, tuple)):
            sample = sample[0] if sample else "?"
    except Exception:
        sample = "?"
    return f"verbecc OK — exemple: {sample}"


# ── Normalisation ──────────────────────────────────────────────────────────
def _normalise(moods_raw):
    """Both sources give {mood: {tense: [list]}}. Some library versions wrap
    each form as [form, ...]; flatten those to plain strings."""
    clean = {}
    for mood, tenses in moods_raw.items():
        clean[mood] = {}
        for tense, forms in tenses.items():
            flat = []
            for f in forms:
                if isinstance(f, (list, tuple)):
                    flat.append(f[0] if f else "")
                else:
                    flat.append(f)
            clean[mood][tense] = flat
    return clean


# ── Public API ──────────────────────────────────────────────────────────────
def conjugate_verb(verb, conn=None):
    """Full conjugation dict for `verb` (verbecc shape), cached. None if the
    verb can't be conjugated by either source."""
    verb = (verb or "").strip().lower()
    if not verb:
        return None

    own_conn = conn is None
    conn = conn or _get_conn()
    try:
        row = conn.execute("SELECT data FROM verb_cache WHERE verb=?",
                           (verb,)).fetchone()
        if row:
            return json.loads(row["data"])

        # Source order. The public verbe.cc demo endpoint currently returns
        # 404, so we default to the local `verbecc` library first (fast,
        # offline) and fall back to the API. Flip SOURCE_ORDER to ("api",
        # "library") if you self-host the verbecc-svc Docker image and set
        # API_BASE to your own URL.
        moods, source = None, None
        for src in SOURCE_ORDER:
            if src == "api":
                moods = _fetch_from_api(verb)
            else:
                moods = _fetch_from_library(verb)
            if moods is not None:
                source = src
                break
        if moods is None:
            return None  # no source could conjugate it

        moods = _normalise(moods)
        irregular = 1 if _looks_irregular(verb, moods) else 0
        from datetime import datetime
        conn.execute(
            "INSERT OR REPLACE INTO verb_cache (verb, data, source, irregular, cached_at) "
            "VALUES (?,?,?,?,?)",
            (verb, json.dumps(moods, ensure_ascii=False), source, irregular,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return moods
    finally:
        if own_conn:
            conn.close()


def get_form(verb, mood, tense, pronoun_index, conn=None):
    """One conjugated form, e.g. get_form('manger','Indicatif','Présent',1)
    -> 'tu manges'. Returns None if unavailable.
    mood/tense are Étozi display names; pronoun_index is our 0..5 scheme
    (0=je 1=tu 2=il/elle 3=nous 4=vous 5=ils/elles).

    verbecc returns a variable number of forms (6 or 9) in varying orders
    depending on the verb/tense, so instead of trusting positions we IDENTIFY
    each form by the pronoun it starts with. This guarantees the form returned
    matches the pronoun shown in the prompt."""
    moods = conjugate_verb(verb, conn=conn)
    if not moods:
        return None
    mkey = MOOD_KEY.get(mood)
    tkey = TENSE_KEY.get((mood, tense))
    if not mkey or not tkey:
        return None
    tense_forms = moods.get(mkey, {}).get(tkey)
    if not tense_forms:
        return None
    forms = [f for f in tense_forms if f]  # drop blanks for matching

    # Impératif has no subject pronoun (just "mange", "mangeons", "mangez").
    # Map by position: tu→0, nous→1, vous→2.
    if mkey == "imperatif":
        imp_map = {1: 0, 3: 1, 4: 2}
        idx = imp_map.get(pronoun_index)
        if idx is None or idx >= len(forms):
            return None
        return forms[idx]

    # Which leading pronoun(s) identify the form we want for each slot.
    # Subjonctif forms are prefixed with que/qu', which we look past.
    want = {
        0: ("je", "j'"),
        1: ("tu",),
        2: ("il", "elle", "on"),
        3: ("nous",),
        4: ("vous",),
        5: ("ils", "elles"),
    }.get(pronoun_index)
    if not want:
        return None

    def lead_pronoun(form):
        s = form.strip()
        low = s.lower()
        # strip subjonctif conjunction
        if low.startswith("que "):
            s = s[4:]; low = s.lower()
        elif low.startswith("qu'"):
            s = s[3:]; low = s.lower()
        if low.startswith("j'"):
            return "j'"
        first = s.split(" ", 1)[0].lower()
        return first

    # Prefer an exact pronoun match (in priority order: il before elle/on,
    # ils before elles), so we pick the canonical form.
    for target in want:
        for f in forms:
            if lead_pronoun(f) == target:
                return f
    # Fallback: positional map if no pronoun match (e.g. pronoun-less output)
    n = len(forms)
    pos = ({0:0,1:1,2:2,3:5,4:6,5:7} if n >= 9 else
           {0:0,1:1,2:2,3:3,4:4,5:5})
    idx = pos.get(pronoun_index)
    if idx is not None and idx < n:
        return forms[idx]
    return None


def _looks_irregular(verb, moods):
    """Best-effort irregular flag: -er verbs that aren't 'aller' and follow
    the regular present pattern are treated as regular; everything else
    (-ir/-re, aller, être, avoir, spelling-change) is flagged irregular.
    This is a heuristic for difficulty weighting, not a linguistic claim."""
    v = verb.lower()
    if v in ("aller", "être", "avoir", "faire", "dire", "venir", "prendre",
             "pouvoir", "vouloir", "devoir", "savoir", "voir", "tenir"):
        return True
    if v.endswith("er"):
        # Regular -er: present 'je' form = stem + 'e'
        try:
            je_form = moods["indicatif"]["présent"][0]
            # strip leading "je " / "j'"
            form = je_form.split(" ", 1)[-1] if " " in je_form else je_form
            form = form.replace("j'", "")
            return not form.endswith("e")
        except Exception:
            return False
    return True  # -ir, -re, etc. → treat as irregular for difficulty


def is_irregular(verb, conn=None):
    verb = (verb or "").strip().lower()
    own_conn = conn is None
    conn = conn or _get_conn()
    try:
        row = conn.execute("SELECT irregular FROM verb_cache WHERE verb=?",
                           (verb,)).fetchone()
        if row is not None:
            return bool(row["irregular"])
        # not cached yet → conjugate (which computes + stores the flag)
        conjugate_verb(verb, conn=conn)
        row = conn.execute("SELECT irregular FROM verb_cache WHERE verb=?",
                           (verb,)).fetchone()
        return bool(row["irregular"]) if row else False
    finally:
        if own_conn:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# QUESTION GENERATOR
# ══════════════════════════════════════════════════════════════════════════
import random

# Default verb pool per difficulty level. Level 1 leans regular; higher
# levels mix in more irregular / advanced verbs. Users can override these
# via their own selection (stored in the DB, see mainapp).
DEFAULT_VERBS = {
    1: ["parler", "manger", "aimer", "regarder", "écouter", "donner",
        "trouver", "penser", "demander", "habiter", "jouer", "chanter"],
    2: ["parler", "manger", "finir", "choisir", "vendre", "attendre",
        "aimer", "perdre", "réussir", "répondre", "grandir", "entendre"],
    3: ["faire", "aller", "venir", "prendre", "voir", "vouloir", "pouvoir",
        "devoir", "savoir", "mettre", "dire", "partir", "sortir", "lire"],
    4: ["falloir", "valoir", "acquérir", "résoudre", "craindre", "peindre",
        "rejoindre", "vivre", "suivre", "conduire", "écrire", "boire",
        "croire", "recevoir", "apercevoir", "s'asseoir"],
}

# Pronoun index → display label (matches PRONOUNS order)
PRONOUN_LABELS = ["je", "tu", "il/elle", "nous", "vous", "ils/elles"]
# Labels shown in the QUESTION prompt. "je/j'" avoids leaking the elided "j'"
# that some answers use (e.g. "j'aime"), which would give away the form.
PROMPT_PRONOUN_LABELS = ["je/j'", "tu", "il/elle", "nous", "vous", "ils/elles"]

# ── Sentence bank for Levels 2–4 ────────────────────────────────────────────
# Each entry: a sentence template with {0}, {1}, ... blanks, and a list of
# (verb, mood, tense, pronoun_index) specs — one per blank, in order.
# Level 2 = one blank; Levels 3–4 = multiple blanks / mixed tenses.
#
# IMPORTANT: French sentences must be grammatically correct. These seed
# examples are verified. To expand to ~100, copy the format exactly. The
# {n} blanks are filled at runtime with the live conjugation, so you only
# write the FRAME and specify which verb/mood/tense/pronoun fills each blank.
#
# pronoun_index: 0=je 1=tu 2=il/elle 3=nous 4=vous 5=ils/elles
SENTENCE_BANK = {
    2: [
        ("Tous les matins, (je/j') {0} un café.",
         [("boire", "Indicatif", "Présent", 0)]),
        ("Hier soir, nous {0} un film au cinéma.",
         [("regarder", "Indicatif", "Passé composé", 3)]),
        ("Demain, tu {0} tes grands-parents.",
         [("voir", "Indicatif", "Futur simple", 1)]),
        ("Quand j'étais petit, (je/j') {0} au parc chaque jour.",
         [("jouer", "Indicatif", "Imparfait", 0)]),
        ("Il faut que vous {0} vos devoirs ce soir.",
         [("finir", "Subjonctif", "Présent", 4)]),
        ("Le week-end, elle {0} dans le jardin.",
         [("travailler", "Indicatif", "Présent", 2)]),
        ("La semaine dernière, ils {0} à Paris.",
         [("aller", "Indicatif", "Passé composé", 5)]),
        ("Quand nous étions jeunes, nous {0} beaucoup de bandes dessinées.",
         [("lire", "Indicatif", "Imparfait", 3)]),
        ("L'été prochain, (je/j') {0} l'espagnol.",
         [("apprendre", "Indicatif", "Futur simple", 0)]),
        ("Chaque soir, tu {0} la télévision.",
         [("regarder", "Indicatif", "Présent", 1)]),
        ("Ce matin, (je/j') {0} le bus à huit heures.",
         [("prendre", "Indicatif", "Passé composé", 0)]),
        ("Avant, vous {0} dans une petite maison.",
         [("habiter", "Indicatif", "Imparfait", 4)]),
        ("Bientôt, nous {0} une nouvelle voiture.",
         [("acheter", "Indicatif", "Futur simple", 3)]),
        ("En ce moment, ils {0} pour leurs examens.",
         [("étudier", "Indicatif", "Présent", 5)]),
        ("Hier, elle {0} une longue lettre à son ami.",
         [("écrire", "Indicatif", "Passé composé", 2)]),
        ("Quand il pleuvait, (je/j') {0} à la maison.",
         [("rester", "Indicatif", "Imparfait", 0)]),
        ("L'année prochaine, tu {0} tes études.",
         [("finir", "Indicatif", "Futur simple", 1)]),
        ("D'habitude, nous {0} à sept heures.",
         [("manger", "Indicatif", "Présent", 3)]),
        ("Samedi dernier, vous {0} un beau cadeau.",
         [("recevoir", "Indicatif", "Passé composé", 4)]),
        ("Quand elle était enfant, elle {0} du piano.",
         [("jouer", "Indicatif", "Imparfait", 2)]),
        ("Plus tard, ils {0} médecins.",
         [("devenir", "Indicatif", "Futur simple", 5)]),
        ("Maintenant, (je/j') {0} mes amis au café.",
         [("attendre", "Indicatif", "Présent", 0)]),
        ("Le mois dernier, nous {0} un nouvel appartement.",
         [("trouver", "Indicatif", "Passé composé", 3)]),
        ("Quand tu étais petit, tu {0} très vite.",
         [("courir", "Indicatif", "Imparfait", 1)]),
        ("Ce soir, vous {0} un bon repas.",
         [("préparer", "Indicatif", "Futur simple", 4)]),
        ("Le matin, (je/j') {0} de bonne heure.",
         [("se lever", "Indicatif", "Présent", 0, {"réfléchi"})]),
        ("Tous les soirs, elle {0} vers dix heures.",
         [("se coucher", "Indicatif", "Présent", 2, {"réfléchi"})]),
        ("Après le sport, nous {0} les mains.",
         [("se laver", "Indicatif", "Présent", 3, {"réfléchi"})]),
        ("Le dimanche, ils {0} dans le parc.",
         [("se promener", "Indicatif", "Présent", 5, {"réfléchi"})]),
        ("En hiver, je {0} chaudement.",
         [("s'habiller", "Indicatif", "Présent", 0, {"réfléchi"})]),
    ],
    3: [
        ("Quand (je/j') {0} jeune, (je/j') {1} souvent au foot.",
         [("être", "Indicatif", "Imparfait", 0),
          ("jouer", "Indicatif", "Imparfait", 0)]),
        ("Si tu {0} plus tôt, tu {1} le train.",
         [("partir", "Indicatif", "Imparfait", 1),
          ("prendre", "Conditionnel", "Présent", 1)]),
        ("Hier, elle {0} ses amis et ils {1} ensemble.",
         [("appeler", "Indicatif", "Passé composé", 2),
          ("sortir", "Indicatif", "Passé composé", 5)]),
        ("Pendant que nous {0}, les enfants {1} dans le jardin.",
         [("cuisiner", "Indicatif", "Imparfait", 3),
          ("jouer", "Indicatif", "Imparfait", 5)]),
        ("Demain, (je/j') {0} mes devoirs puis (je/j') {1} mes amis.",
         [("finir", "Indicatif", "Futur simple", 0),
          ("voir", "Indicatif", "Futur simple", 0)]),
        ("Quand il {0} à la maison, il {1} qu'il avait perdu ses clés.",
         [("arriver", "Indicatif", "Passé composé", 2),
          ("comprendre", "Indicatif", "Passé composé", 2)]),
        ("Tous les matins, tu {0} et ensuite tu {1} ton petit-déjeuner.",
         [("se réveiller", "Indicatif", "Présent", 1),
          ("prendre", "Indicatif", "Présent", 1)]),
        ("Hier, nous {0} au marché et nous {1} des légumes.",
         [("aller", "Indicatif", "Passé composé", 3),
          ("acheter", "Indicatif", "Passé composé", 3)]),
        ("Quand vous {0} petits, vous {1} beaucoup de dessins animés.",
         [("être", "Indicatif", "Imparfait", 4),
          ("regarder", "Indicatif", "Imparfait", 4)]),
        ("Si elle {0} le temps, elle {1} plus souvent.",
         [("avoir", "Indicatif", "Imparfait", 2),
          ("voyager", "Conditionnel", "Présent", 2)]),
        ("Ce matin, ils {0} tôt parce qu'ils {1} un rendez-vous.",
         [("se lever", "Indicatif", "Passé composé", 5),
          ("avoir", "Indicatif", "Imparfait", 5)]),
        ("Quand (je/j') {0} mon travail, (je/j') {1} en vacances.",
         [("finir", "Indicatif", "Futur simple", 0),
          ("partir", "Indicatif", "Futur simple", 0)]),
        ("Pendant les vacances, nous {0} à la plage et nous {1} dans la mer.",
         [("aller", "Indicatif", "Imparfait", 3),
          ("nager", "Indicatif", "Imparfait", 3)]),
        ("Hier soir, tu {0} fatigué donc tu {1} de bonne heure.",
         [("être", "Indicatif", "Imparfait", 1),
          ("se coucher", "Indicatif", "Passé composé", 1)]),
        ("Quand elle {0} la nouvelle, elle {1} de joie.",
         [("entendre", "Indicatif", "Passé composé", 2),
          ("pleurer", "Indicatif", "Passé composé", 2)]),
        ("Si nous {0} riches, nous {1} le monde entier.",
         [("être", "Indicatif", "Imparfait", 3),
          ("visiter", "Conditionnel", "Présent", 3)]),
        ("Demain, vous {0} à l'école et vous {1} vos amis.",
         [("retourner", "Indicatif", "Futur simple", 4),
          ("retrouver", "Indicatif", "Futur simple", 4)]),
        ("Quand j'étais enfant, (je/j') {0} tôt et (je/j') {1} au jardin.",
         [("se réveiller", "Indicatif", "Imparfait", 0),
          ("courir", "Indicatif", "Imparfait", 0)]),
        ("Hier, il {0} sous la pluie et il {1} malade.",
         [("marcher", "Indicatif", "Passé composé", 2),
          ("tomber", "Indicatif", "Passé composé", 2)]),
        ("Si tu {0} plus, tu {1} de meilleures notes.",
         [("travailler", "Indicatif", "Imparfait", 1),
          ("avoir", "Conditionnel", "Présent", 1)]),
        ("Ce week-end, nous {0} nos grands-parents et nous {1} avec eux.",
         [("visiter", "Indicatif", "Futur simple", 3),
          ("dîner", "Indicatif", "Futur simple", 3)]),
        ("Quand ils {0} au cinéma, le film {1} déjà commencé.",
         [("arriver", "Indicatif", "Passé composé", 5),
          ("avoir", "Indicatif", "Imparfait", 2)]),
        ("Avant, elle {0} en ville mais maintenant elle {1} à la campagne.",
         [("habiter", "Indicatif", "Imparfait", 2),
          ("habiter", "Indicatif", "Présent", 2)]),
        ("Demain, (je/j') {0} très tôt car (je/j') {1} un train.",
         [("se lever", "Indicatif", "Futur simple", 0),
          ("prendre", "Indicatif", "Futur simple", 0)]),
        ("Pendant que tu {0}, (je/j') {1} le dîner.",
         [("dormir", "Indicatif", "Imparfait", 1),
          ("préparer", "Indicatif", "Imparfait", 0)]),
        ("Le matin, (je/j') {0} puis (je/j') {1} le petit-déjeuner.",
         [("se lever", "Indicatif", "Présent", 0, {"réfléchi"}),
          ("préparer", "Indicatif", "Présent", 0)]),
        ("Quand il pleut, nous {0} et nous {1} à la maison.",
         [("rester", "Indicatif", "Présent", 3),
          ("s'ennuyer", "Indicatif", "Présent", 3, {"réfléchi"})]),
        ("Le soir, elle {0} mais elle {1} avant minuit.",
         [("travailler", "Indicatif", "Présent", 2),
          ("se coucher", "Indicatif", "Présent", 2, {"réfléchi", "négatif"})]),
    ],
    4: [
        ("Bien que cela {0} difficile, nous {1} de résoudre le problème.",
         [("être", "Subjonctif", "Présent", 2),
          ("essayer", "Indicatif", "Passé composé", 3)]),
        ("Il aurait fallu que vous {0} avant qu'il {1}.",
         [("venir", "Subjonctif", "Présent", 4),
          ("partir", "Subjonctif", "Présent", 2)]),
        ("Je doute qu'il {0} la vérité, même s'il {1} le prouver.",
         [("connaître", "Subjonctif", "Présent", 2),
          ("vouloir", "Indicatif", "Imparfait", 2)]),
        ("Pourvu que nous {0} à temps, nous {1} le spectacle.",
         [("arriver", "Subjonctif", "Présent", 3),
          ("voir", "Conditionnel", "Présent", 3)]),
        ("Quoique tu {0} fatigué, tu {1} terminer ce travail.",
         [("être", "Subjonctif", "Présent", 1),
          ("devoir", "Indicatif", "Présent", 1)]),
        ("Si j'avais su, (je/j') {0} autrement et (je/j') {1} cette erreur.",
         [("agir", "Conditionnel", "Passé première forme", 0),
          ("commettre", "Conditionnel", "Passé première forme", 0,
           {"négatif"})]),
        ("Il faut que vous {0} ce livre avant que le cours {1}.",
         [("lire", "Subjonctif", "Présent", 4),
          ("commencer", "Subjonctif", "Présent", 2)]),
        ("Bien qu'elle {0} la réponse, elle {1} silencieuse.",
         [("savoir", "Subjonctif", "Présent", 2),
          ("rester", "Indicatif", "Passé composé", 2)]),
        ("Nous craignons qu'il {0} malade et qu'il {1} à l'hôpital.",
         [("tomber", "Subjonctif", "Présent", 2),
          ("aller", "Subjonctif", "Présent", 2)]),
        ("À condition que tu {0} prudent, tu {1} conduire ma voiture.",
         [("être", "Subjonctif", "Présent", 1),
          ("pouvoir", "Indicatif", "Présent", 1)]),
        ("Je voudrais que nous {0} ce projet, même s'il {1} du courage.",
         [("poursuivre", "Subjonctif", "Présent", 3),
          ("falloir", "Indicatif", "Présent", 2)]),
        ("Avant qu'ils {0}, il faut que je leur {1} la nouvelle.",
         [("partir", "Subjonctif", "Présent", 5),
          ("dire", "Subjonctif", "Présent", 0)]),
        ("Si vous aviez insisté, ils {0} et le projet {1} réussi.",
         [("céder", "Conditionnel", "Passé première forme", 5),
          ("avoir", "Conditionnel", "Passé première forme", 2)]),
        ("Il est essentiel que chacun {0} ses responsabilités et les {1}.",
         [("comprendre", "Subjonctif", "Présent", 2),
          ("assumer", "Subjonctif", "Présent", 2)]),
        ("Quoi que tu {0}, (je/j') {1} toujours à tes côtés.",
         [("faire", "Subjonctif", "Présent", 1),
          ("rester", "Indicatif", "Futur simple", 0)]),
        ("Je suis surpris que vous {0} déjà et que vous {1} si vite.",
         [("revenir", "Subjonctif", "Présent", 4),
          ("résoudre", "Indicatif", "Passé composé", 4)]),
        ("Pour que l'équipe {0}, il faudrait que chacun {1} un effort.",
         [("réussir", "Subjonctif", "Présent", 2),
          ("faire", "Subjonctif", "Présent", 2)]),
        ("Bien que le chemin {0} long, nous {1} d'atteindre le sommet.",
         [("paraître", "Subjonctif", "Présent", 2),
          ("convenir", "Indicatif", "Passé composé", 3)]),
        ("Il se peut qu'elle {0} en retard parce qu'elle {1} le bus.",
         [("être", "Subjonctif", "Présent", 2),
          ("manquer", "Indicatif", "Passé composé", 2)]),
        ("Je crains que nous {0} en retard et que le temps nous {1}.",
         [("arriver", "Subjonctif", "Présent", 3),
          ("manquer", "Subjonctif", "Présent", 2)]),
        ("Où que tu {0}, tu {1} toujours te souvenir de tes racines.",
         [("aller", "Subjonctif", "Présent", 1),
          ("devoir", "Indicatif", "Présent", 1)]),
        ("Si elle avait pu, elle {0} ce poste et {1} à l'étranger.",
         [("accepter", "Conditionnel", "Passé première forme", 2),
          ("vivre", "Conditionnel", "Passé première forme", 2)]),
        ("Il est rare que les gens {0} leurs erreurs et qu'ils {1}.",
         [("reconnaître", "Subjonctif", "Présent", 5),
          ("changer", "Subjonctif", "Présent", 5)]),
        ("Jusqu'à ce que tu {0} la vérité, je {1} le silence.",
         [("apprendre", "Subjonctif", "Présent", 1),
          ("garder", "Indicatif", "Futur simple", 0)]),
        ("Bien que nous {0} prudents, il {1} un accident.",
         [("être", "Subjonctif", "Présent", 3),
          ("se produire", "Indicatif", "Passé composé", 2)]),
        ("Il est important que tu {0} et que tu {1} devant l'obstacle.",
         [("persévérer", "Subjonctif", "Présent", 1),
          ("se décourager", "Subjonctif", "Présent", 1, {"réfléchi", "négatif"})]),
        ("Quoiqu'elle {0} fatiguée, elle {1} de la journée.",
         [("être", "Subjonctif", "Présent", 2),
          ("se plaindre", "Indicatif", "Présent", 2, {"réfléchi", "négatif"})]),
    ],
}


def _eligible_pairs(selected_moods_tenses):
    """selected_moods_tenses: list of (mood, tense) Étozi display tuples that
    the user enabled. Returns the same list, filtered to ones we can map."""
    out = []
    for mood, tense in selected_moods_tenses:
        if (mood, tense) in TENSE_KEY:
            out.append((mood, tense))
    return out


def generate_question(level, verbs, pronoun_indices, mood_tense_pairs,
                      conn=None, show_tense=False):
    """Build ONE question dict. answers = full forms (for display); accept =
    per-blank variant sets (pronoun-optional grading). None if not buildable."""
    pairs = _eligible_pairs(mood_tense_pairs)
    if not verbs or not pronoun_indices or not pairs:
        return None

    if level == 1:
        for _ in range(20):
            verb = random.choice(verbs)
            mood, tense = random.choice(pairs)
            pidx = random.choice(pronoun_indices)
            if mood == "Impératif" and pidx not in (1, 3, 4):
                continue
            form = get_form(verb, mood, tense, pidx, conn=conn)
            if not form:
                continue
            label = PROMPT_PRONOUN_LABELS[pidx]
            prompt = (
                f'<div class="ex-prompt">'
                f'<span class="ex-pronoun">{label}</span> '
                f'<span class="ex-verb">({verb})</span>'
                f'<div class="ex-tense">{mood} · {tense}</div>'
                f'</div>')
            return {
                "level": 1, "prompt_html": prompt,
                "answers": [form],
                "accept": [answer_variants(form, mood)],
                "blanks": 1,
                "meta": {"verb": verb, "mood": mood, "tense": tense,
                         "pronoun": label, "full": form},
            }
        return None

    bank = list(SENTENCE_BANK.get(level, []))
    if not bank:
        return None

    def _frame_subject(frame, blank_i):
        """The subject word the sentence uses right before blank {blank_i}, so
        the answer can agree (il vs elle, ils vs elles, je vs j')."""
        import re as _re
        m = _re.search(r"([A-Za-zÀ-ÿ'/()]+)\s*\{" + str(blank_i) + r"\}", frame)
        if not m:
            return None
        tok = m.group(1).lower().strip("()")
        # "(je/j')" placeholder → treat as je
        if "je" in tok or tok in ("j'", "j"):
            return "je"
        if tok in ("il", "elle", "on", "ils", "elles",
                   "je", "tu", "nous", "vous"):
            return tok
        return None

    random.shuffle(bank)
    for frame, specs in bank:
        answers, accepts, ok = [], [], True
        for bi, spec in enumerate(specs):
            v, mood, tense, pidx = spec[0], spec[1], spec[2], spec[3]
            flags = spec[4] if len(spec) > 4 else set()
            negative = "négatif" in flags
            reflexive = "réfléchi" in flags
            # Reflexive verbs are stored as "se laver"/"s'habiller"; conjugate
            # the BASE verb (build_form adds the reflexive pronoun itself).
            base = v
            if reflexive:
                low = v.lower()
                if low.startswith("se "):
                    base = v[3:]
                elif low.startswith("s'"):
                    base = v[2:]
            plain = get_form(base, mood, tense, pidx, conn=conn)
            if not plain:
                ok = False
                break
            # Make the answer's subject agree with the sentence (il↔elle, etc.)
            desired = _frame_subject(frame, bi)
            if negative or reflexive:
                form = build_form(plain, pidx, mood,
                                  negative=negative, reflexive=reflexive)
                if desired:
                    form = align_subject(form, desired, mood)
                accepts.append(variants_for_built(form, mood))
            else:
                form = align_subject(plain, desired, mood) if desired else plain
                accepts.append(answer_variants(form, mood))
            answers.append(form)
        if not ok:
            continue
        blanks_html = frame
        for i in range(len(specs)):
            spec = specs[i]
            v, mood, tense, pidx = spec[0], spec[1], spec[2], spec[3]
            flags = spec[4] if len(spec) > 4 else set()
            bits = [v]
            if show_tense:
                # Indicatif tense names are unambiguous on their own, but a bare
                # "présent" on a Conditionnel/Subjonctif/Impératif blank reads as
                # présent indicatif. Prefix the mood for non-indicatif blanks so
                # e.g. "Si nous étions… nous ____ (visiter, présent)" correctly
                # shows "(visiter, conditionnel présent)".
                bits.append(tense.lower() if mood == "Indicatif"
                            else f"{mood.lower()} {tense.lower()}")
            if "négatif" in flags:
                bits.append("négatif")
            if "réfléchi" in flags:
                bits.append("réfléchi")
            hint = ", ".join(bits)
            blanks_html = blanks_html.replace(
                "{" + str(i) + "}",
                f'<span class="ex-blank">____ ({hint})</span>')
        prompt = f'<div class="ex-sentence">{blanks_html}</div>'
        return {
            "level": level, "prompt_html": prompt,
            "answers": answers, "accept": accepts,
            "blanks": len(specs),
            "meta": {"frame": frame, "specs": specs},
        }
    return None


_SUBJECT_PRONOUNS = ("je", "tu", "il", "elle", "on", "nous", "vous",
                     "ils", "elles")

def _strip_pronoun(full_form, pronoun_index=None, mood=None):
    s = full_form.strip()
    if mood == "Subjonctif":
        low = s.lower()
        if low.startswith("que "):
            s = s[4:]
        elif low.startswith("qu'"):
            s = s[3:]
    s = s.strip()
    if s.lower().startswith("j'"):
        return s[2:].strip()
    parts = s.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in _SUBJECT_PRONOUNS:
        return parts[1].strip()
    return s


def answer_variants(full_form, mood=None):
    """Acceptable typed answers (pronoun optional), accents strict."""
    variants = set()
    s = full_form.strip()
    variants.add(s)
    body, low = s, s.lower()
    if mood == "Subjonctif":
        if low.startswith("que "):
            body = s[4:].strip(); variants.add(body)
        elif low.startswith("qu'"):
            body = s[3:].strip(); variants.add(body)
    b = body.strip()
    if b.lower().startswith("j'"):
        variants.add(b); variants.add(b[0] + b[2:]); variants.add(b[2:].strip())
    else:
        parts = b.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() in _SUBJECT_PRONOUNS:
            variants.add(b); variants.add(parts[1].strip())
    variants.add(_strip_pronoun(full_form, mood=mood))
    return {v for v in variants if v}


# ── Negation & reflexive construction ──────────────────────────────────────
_VOWELS = "aeiouàâäéèêëîïôöùûühAEIOUÀÂÄÉÈÊËÎÏÔÖÙÛÜH"
# Reflexive pronoun for each of our 6 pronoun slots
_REFLEX = {0: "me", 1: "te", 2: "se", 3: "nous", 4: "vous", 5: "se"}
# Auxiliary tokens that mark a compound tense
_AUX = {"ai", "as", "a", "avons", "avez", "ont",
        "suis", "es", "est", "sommes", "êtes", "sont",
        "avais", "avait", "avions", "aviez", "avaient",
        "étais", "était", "étions", "étiez", "étaient",
        "aurai", "auras", "aura", "aurons", "aurez", "auront",
        "serai", "seras", "sera", "serons", "serez", "seront",
        "aurais", "aurait", "aurions", "auriez", "auraient",
        "serais", "serait", "serions", "seriez", "seraient",
        "aie", "aies", "ait", "ayons", "ayez", "aient",
        "sois", "soit", "soyons", "soyez", "soient",
        "eus", "eut", "eûmes", "eûtes", "eurent",
        "fus", "fut", "fûmes", "fûtes", "furent"}


def _elide(word, nextword):
    """ne→n', me→m', etc. before a vowel/mute-h."""
    if nextword and nextword[0] in _VOWELS:
        return word[:-1] + "'"
    return word + " "


def _split_subject(full_form, mood=None):
    """Return (subject_str_with_trailing_space_or_apostrophe, rest).
    Handles 'je ', "j'", subjonctif 'que '/'qu''. rest is the verb cluster."""
    s = full_form.strip()
    prefix = ""
    low = s.lower()
    if mood == "Subjonctif":
        if low.startswith("que "):
            prefix = s[:4]; s = s[4:]
        elif low.startswith("qu'"):
            prefix = s[:3]; s = s[3:]
    low = s.lower()
    if low.startswith("j'"):
        return prefix, "j'", s[2:].strip()
    parts = s.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in _SUBJECT_PRONOUNS:
        return prefix, parts[0] + " ", parts[1].strip()
    return prefix, "", s  # no detectable subject


def _is_compound(verb_cluster):
    """True if the verb cluster begins with an auxiliary (compound tense)."""
    first = verb_cluster.split(" ", 1)[0].lower().strip("'")
    # handle "j'ai" style already stripped; check elided auxiliaries too
    return first in _AUX


def build_form(plain, pidx, mood=None, negative=False, reflexive=False):
    """Transform a plain conjugated form (e.g. 'je lave', "j'ai mangé") into its
    negative and/or reflexive version. Returns the full string with subject."""
    prefix, subject, verb = _split_subject(plain, mood)
    subj_token = subject.strip()  # 'je', "j'", 'tu', ...
    compound = _is_compound(verb)

    cluster = verb  # what we build between subject and end

    # 1) Reflexive: insert the reflexive pronoun before the verb cluster.
    if reflexive:
        rp = _REFLEX.get(pidx, "se")
        rp_out = _elide(rp, cluster)  # 'me ' or "m'"
        cluster = rp_out + cluster

    # 2) Negation: ne ... pas.
    if negative:
        ne_out = _elide("ne", cluster)  # 'ne ' or "n'"
        if compound and not reflexive:
            # wrap auxiliary: ne + aux + pas + participle
            parts = cluster.split(" ", 1)
            aux = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            cluster = f"{ne_out}{aux} pas" + (f" {rest}" if rest else "")
        elif compound and reflexive:
            # ne + reflexive + aux + pas + participle
            # cluster currently = "me suis lavé" → ne me suis pas lavé
            toks = cluster.split(" ")
            # toks[0]=reflexive, toks[1]=aux, toks[2:]=participle
            if len(toks) >= 2:
                head = " ".join(toks[:2])
                tail = " ".join(toks[2:])
                cluster = f"{ne_out}{head} pas" + (f" {tail}" if tail else "")
            else:
                cluster = f"{ne_out}{cluster} pas"
        else:
            # simple tense: ne + (reflexive+)verb + pas
            cluster = f"{ne_out}{cluster} pas"

    # Rebuild subject + cluster, re-eliding the subject if needed.
    if subj_token.lower() in ("je", "j'"):
        sub = _elide("je", cluster)  # 'je ' or "j'"
    elif subject:
        sub = subject  # already has trailing space
    else:
        sub = ""
    return (prefix + sub + cluster).strip()


def variants_for_built(full_built, mood=None):
    """Acceptable typed answers for a negative/reflexive form: the full form and
    the no-subject form. Per design, 'ne' may NOT be dropped. Accents strict.
    e.g. 'je ne me lave pas' -> {'je ne me lave pas', 'ne me lave pas'}."""
    variants = set()
    s = full_built.strip()
    variants.add(s)
    prefix, subject, rest = _split_subject(s, mood)
    # rest = everything after the subject (starts with 'ne'/"n'" or reflexive)
    if rest:
        variants.add((prefix + rest).strip())
    return {v for v in variants if v}


def align_subject(form, desired_subject, mood=None):
    """Swap the leading subject pronoun of `form` to `desired_subject` (e.g.
    make 'il se couche' agree with a sentence that says 'elle' → 'elle se
    couche'). Keeps elision correct for je/j'. If no subject is detected, the
    form is returned unchanged."""
    if not desired_subject:
        return form
    prefix, subject, rest = _split_subject(form, mood)
    if not subject:
        return form
    ds = desired_subject.strip().lower()
    if ds in ("je", "j'"):
        sub = _elide("je", rest)   # 'je ' or "j'"
    else:
        sub = ds + " "
    return (prefix + sub + rest).strip()


def make_exercise(level, verbs, pronoun_indices, mood_tense_pairs,
                  n=12, conn=None, show_tense=False):
    """Build up to n questions; pre-warm verbs (no hang); avoid repeats."""
    usable = []
    for v in verbs:
        if conjugate_verb(v, conn=conn) is not None:
            usable.append(v)
    if not usable:
        return []
    qs, seen, attempts = [], set(), 0
    while len(qs) < n and attempts < n * 12:
        attempts += 1
        q = generate_question(level, usable, pronoun_indices,
                              mood_tense_pairs, conn=conn, show_tense=show_tense)
        if not q:
            continue
        sig = q["prompt_html"]
        if sig in seen and len(seen) < _distinct_capacity(level):
            continue
        seen.add(sig)
        qs.append(q)
    return qs


def _distinct_capacity(level):
    if level == 1:
        return 999
    return len(SENTENCE_BANK.get(level, []))


def grade_answer(student, expected, accept=None):
    """Accents strict; tolerant of whitespace/case/trailing punctuation. If
    `accept` (variant set) given, matching ANY variant counts as correct."""
    def norm(x):
        return (x or "").strip().lower().rstrip(".!?").strip()
    s = norm(student)
    if not s:
        return False
    candidates = set()
    if accept:
        candidates |= {norm(a) for a in accept}
    candidates.add(norm(expected))
    return s in candidates
