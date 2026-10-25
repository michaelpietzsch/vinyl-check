"""Interface values of versions before 1.0 and their names since 1.0, in one place.

Versions before 1.0 stored German identifiers in the database and sent them through the API ("knackser",
"rueckwaerts", "stille_rille", "Freigabe empfohlen" …). Since 1.0 these are English codes; the rest of the code only
knows the new ones. An existing database is migrated once when it is opened (db.DB._migrate_1_0), using the tables
and functions below.

upgrade() rewrites a stored JSON value **by context**: dict keys are renamed from KEYS, string values only under the
fields listed in VALUES and only when they are one of the known old values. Free text (notes, titles, copy labels,
labels typed by users, i18n messages) is never touched, and since no new value is also an old one, upgrading twice
changes nothing.
"""

from __future__ import annotations

# ─── Record check ────────────────────────────────────────────────────────────
EVENT_LABELS = {"knackser": "defect", "musik": "music", "unsicher": "unsure"}          # judgement of one event
SKIP_LABELS = {"endrille": "run_out", "kein_sprung": "no_skip"}                        # judgement of one skip
SKIP_KINDS = {"rueckwaerts": "backward", "unsicher": "uncertain", "endrille": "run_out"}
FINDINGS = {"kratzer": "scratch", "haarkratzer": "hairline_scratch", "welle": "warp", "schmutz": "dirt",
            "fingerabdruecke": "fingerprints", "exzentrisch": "off_center"}
# Where a side length or the turntable floor comes from (side lengths were only ever sent, the floor is stored)
SOURCES = {"gemessen": "measured", "Titelliste": "track_list", "Discogs": "discogs",
           "stille Rille der Testplatte": "test_record_silent_groove"}

# ─── Master comparison and test pressing approval ────────────────────────────
POLARITY = {"gedreht": "inverted", "links verpolt": "left_inverted", "rechts verpolt": "right_inverted",
            "keine": "none", "wie Master": "ok"}
GAP_KINDS = {"Anfang": "start", "Pause": "gap", "Ende": "end"}
# Labels of the wow and frequency response sections: stored as German text before 1.0, translatable messages since
SEGMENT_LABELS = {"Anfang": "master.part_start", "Mitte": "master.part_middle", "Ende": "master.part_end"}
APPROVAL_VERDICTS = {"Freigabe empfohlen": "approve", "Freigabe mit Hinweisen": "approve_with_notes",
                     "nicht freigeben": "reject"}

# ─── Turntable alignment ─────────────────────────────────────────────────────
BAND_TYPES = {"ton": "tone", "rampe": "ramp", "zweiton": "two_tone", "rauschen": "noise", "stille": "silence",
              "unbekannt": "unknown"}
SWEEP_DIRECTIONS = {"auf": "up", "ab": "down"}
CHANNEL_MODES = {"links": "left", "rechts": "right", "vertikal": "vertical", "gemischt": "mixed"}
BAND_ROLES = {"xt_links": "xt_left", "xt_rechts": "xt_right", "sweep_hoch": "sweep_high", "sweep_tief": "sweep_low",
              "gleichlauf": "wow_flutter", "resonanz": "resonance", "rosa_lateral": "pink_lateral",
              "rosa_vertikal": "pink_vertical", "stille_rille": "silent_groove"}
ANTISKATING = {"zu stark": "too_strong", "zu schwach": "too_weak", "ausgeglichen": "balanced"}
RESULT_KEYS = {"pegel": "ref_level"}                   # finding key and metric of the target maps
MAP_IDS = {"labor": "lab"}                             # built-in target map, stored in profiles and settings
METRIC_KINDS = {"justage": "alignment"}                # metrics["kind"] of a test record side

# Dict keys, wherever they occur in stored results
KEYS = {"hinweis": "explanation", "deutung": "interpretation", "soll": "target", "pegel": "ref_level",
        "vertikal": "vertical"}
# Old → new string values per field name ("label" only on events and skips, see upgrade)
VALUES = {
    "label": EVENT_LABELS | SKIP_LABELS,
    "kind": SKIP_KINDS | GAP_KINDS | METRIC_KINDS,
    "kind_auto": SKIP_KINDS,
    "findings": FINDINGS,
    "source": SOURCES,
    "polarity": POLARITY,
    "verdict": APPROVAL_VERDICTS | ANTISKATING,
    "type": BAND_TYPES,
    "direction": SWEEP_DIRECTIONS,
    "mode": CHANNEL_MODES,
    "role": BAND_ROLES,
    "key": RESULT_KEYS,
}
EVENT_KINDS = ("pop", "click", "tick")


def _is_msg(value) -> bool:
    return isinstance(value, dict) and ("$t" in value or "$join" in value)


def _segment(seg):
    """A wow or frequency response section with its old text label replaced by the message it has since 1.0."""
    if isinstance(seg, dict) and isinstance(seg.get("label"), str) and seg["label"] in SEGMENT_LABELS:
        return {**seg, "label": {"$t": SEGMENT_LABELS[seg["label"]]}}
    return seg


def upgrade(value, field: str | None = None):
    """A stored JSON value (parsed) with the interface values of 1.0. `field` is the key the value is stored under.

    Positional lists are handled too: the bands of an alignment overview ([t0, t1, i, role, label]) and events as the
    API lists them ([t, kind, region, strength, label, …])."""
    if isinstance(value, dict):
        if _is_msg(value):                             # a translatable text, not an interface value
            return value
        out = {}
        for k, v in value.items():
            nk = KEYS.get(k, k)
            # a judgement sits on an event or a skip (both have a kind); other labels are display texts
            out[nk] = v if nk == "label" and "kind" not in value else upgrade(v, nk)
        if out.get("key") == "antiskating" and isinstance(out.get("value"), str):   # its verdict is the value
            out["value"] = ANTISKATING.get(out["value"], out["value"])
        return out
    if isinstance(value, list):
        if field == "bands" and all(isinstance(b, list) for b in value):
            return [b[:3] + [BAND_ROLES.get(b[3], b[3])] + b[4:] if len(b) > 3 and isinstance(b[3], str) else b
                    for b in value]
        if field == "events" and all(isinstance(e, list) for e in value):
            return [e[:4] + [EVENT_LABELS.get(e[4], e[4])] + e[5:]
                    if len(e) > 4 and e[1] in EVENT_KINDS and isinstance(e[4], str) else e for e in value]
        if field == "segments":
            return [_segment(upgrade(v)) for v in value]
        return [upgrade(v, field) for v in value]
    if isinstance(value, str) and field in VALUES:
        return VALUES[field].get(value, value)
    return value


def upgrade_target_maps(maps: dict) -> dict:
    """Custom and changed target maps (setting `target_maps`): map ids from MAP_IDS, metric keys from RESULT_KEYS.
    Should a map exist under both its old and its new id, the new one is kept."""
    out: dict = {}
    for mid in sorted(maps, key=lambda m: m in MAP_IDS):            # unchanged ids first
        m = maps[mid]
        if isinstance(m, dict) and isinstance(m.get("targets"), dict):
            m = m | {"targets": {RESULT_KEYS.get(k, k): v for k, v in m["targets"].items()}}
        new = MAP_IDS.get(mid, mid)
        if new not in out:
            out[new] = m
    return out
