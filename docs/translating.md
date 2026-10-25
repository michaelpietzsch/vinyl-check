# Translating vinyl-check

vinyl-check ships with **English** (the reference language and fallback) and **German**. A new language needs no
code changes for most languages: you copy the English catalogs, translate the values, and the language shows up in
the settings.

## How translations work

There are two sets of catalogs:

| Set | Location | Used for |
|---|---|---|
| server | `vinylcheck/locales/<lang>/<namespace>.json` | API messages and errors, analysis findings and advice, PDF reports |
| web | `vinylcheck/static/locales/<lang>/<namespace>.json` | the web interface |

- Each file is a **flat JSON object** for one namespace. The full key is `<namespace>.<key>`. For example, the
  key `title` in `records.json` is `records.title`. Keys use lowercase letters, digits, `_` and `.` only.
- Values are **templates with named placeholders**: `"{n} clicks per minute"`, or with a fixed number of decimals,
  `"{db:.1f} dB"`. Numbers are formatted in the reader's convention automatically (for example a decimal comma in
  German).
- **Plural forms** are objects selected by the parameter `n`: `{"one": "{n} click", "other": "{n} clicks"}`.
  `other` is required.
- A language exists once it has a **web catalog directory**. Its name in the language menu comes from the key
  `meta.language_name` in `vinylcheck/static/locales/<lang>/meta.json`.
- **Language choice**, in order of precedence: the `?lang=` parameter of a request, the language chosen in the
  settings (stored in the cookie `vc_lang`), the browser's `Accept-Language`, and English. Regional codes are reduced
  to the base language (`de-AT` → `de`).
- **Missing keys fall back to English**, then to the key itself. The server logs a warning once per key and
  language, and the browser logs one in the console.
- Server messages are stored language-neutral (as a key plus parameters) and rendered when a response is sent, so
  stored findings appear in the reader's language. Texts stored by versions before 1.0 are shown as they are.

Implementation: `vinylcheck/i18n.py` (server) and `vinylcheck/static/i18n.js` (web).

## Adding a language

The steps below use French (`fr`) as an example.

### 1. Copy the English catalogs

```bash
cp -r vinylcheck/locales/en        vinylcheck/locales/fr
cp -r vinylcheck/static/locales/en vinylcheck/static/locales/fr
```

Use the two-letter ISO 639-1 code in lowercase as the directory name.

### 2. Name the language

In `vinylcheck/static/locales/fr/meta.json`, set the native name:

```json
{
  "language_name": "Français"
}
```

### 3. Translate the values

Translate every value and leave the keys untouched.

- **Keep every placeholder**, with exactly the same name and format: `{n}`, `{db:.1f}`, `{grade}`. You may move them
  within the sentence. In the web catalogs, only the forms `{name}` and `{name:.Nf}` are supported.
- **Keep plural objects as objects**, and translate each form. If your language has more plural categories than
  `one` and `other`, see [step 5](#5-plural-forms-and-number-format).
- **Keep markup** in values used with `data-i18n-html` (tags such as `<b>` or `<a href="…">`). Translate the text
  between the tags.
- **Do not translate** Goldmine grades (M, NM, VG+, VG, G+, G, F, P), units (dB, dBFS, Hz, %, /min, rpm), product
  and standard names (Discogs, AAPT 1, DIN 45500, ITU-R BS.1770) or file formats.
- **Dates** on the server use `strftime` patterns in `common.date_format` and `common.datetime_format`, for example
  `"%d/%m/%Y"` and `"%d/%m/%Y %H:%M"`. The web interface formats dates with the browser's locale data
  automatically.
- **Terminology.** vinyl-check uses audio-engineering language. Compare with the English and German catalogs, and
  keep a term consistent across all files. Core terms: click, pop, crackle, groove noise, lead-in and run-out
  groove, silent groove, skip, stylus, cartridge, tracking force, anti-skating, VTA, azimuth, crosstalk,
  wow & flutter, test record, test pressing, master, condition grade.
- Interface space is limited. Prefer short wording for buttons, table headings and chart labels.

### 4. Run the tests

```bash
pytest tests/test_i18n.py
```

The tests check that:

- every language has exactly the English keys, in both the server and the web catalogs;
- every value has the same placeholders as its English counterpart (across all plural forms);
- every file is flat JSON with valid keys, and every plural object has `other`;
- every key used in the code exists in the English catalog.

### 5. Plural forms and number format

Most languages need nothing here. Two cases need a small code change in `vinylcheck/i18n.py`:

- **More plural categories** (for example Polish or Russian with `one`, `few`, `many`, `other`). The web interface
  already uses the CLDR rules of the browser (`Intl.PluralRules`), so add the extra forms to your plural objects.
  On the server, `_plural()` only distinguishes `one` and `other`. Extend it with the rule for your language.
- **Decimal and thousands separators.** The web interface uses the browser's locale data. On the server, add your
  language to `SEPARATORS` if it does not use `.` for decimals and `,` for thousands, for example
  `"fr": (",", " ")`.

### 6. Try it

Catalogs are loaded once per process, so restart vinyl-check after editing them. Then:

- choose the language in the interface settings, or set it as the preferred language of your browser;
- check API texts and PDF reports directly with the `lang` parameter, for example
  `/api/status?lang=fr` or `/api/recordings/<id>/report.pdf?lang=fr`;
- look through the main pages (record, recording report, comparison, turntable check, settings) for text that
  does not fit, and check both PDF reports.

### 7. Submit

Open a pull request with both catalog directories (and any change to `i18n.py`), and add a line to
[CHANGELOG.md](../CHANGELOG.md) under *Unreleased*. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Updating an existing language

When a new key is added to English, the tests fail until it is added to every language. Contributors add the
German text themselves. For other languages, a missing key shows the English text until a translator adds it.
To find keys that differ from English, run the test: its failure message lists the missing and extra keys per
language.

## For developers

- Never hard-code user-visible text. On the server, return `i18n.msg("namespace.key", **params)`. For text needed
  right away (PDF reports), call `i18n.t("namespace.key", **params)`. In the web interface, use
  `T("namespace.key", params)` or the `data-i18n*` attributes.
- Errors a user should see are raised as `i18n.MessageError` (or a subclass) carrying a message;
  `i18n.error_message(e)` returns it. A message kept in a plain text column is stored with `i18n.dumps` and read
  back with `i18n.loads_text`.
- Values that are stored or compared (kinds, judgements, roles, verdicts) are English codes and never translated;
  the interface shows them through catalog labels.
- Add new keys to English and German in the same change, in roughly the order they appear in the interface, with
  2-space indentation and UTF-8 (no ASCII escaping).
- Log messages are plain English and are not translated.
