# data/jlpt_bank.json.gz

Compiled by `scripts/build_jlpt_bank.py` from two sources, both CC-BY-SA-4.0:

- **JLPT levels**: [stephenmk/yomitan-jlpt-vocab](https://github.com/stephenmk/yomitan-jlpt-vocab),
  which is Jonathan Waller's Tanos JLPT vocabulary lists, cross-referenced against
  JMdict entry IDs to standardize spellings and disambiguate homographs.
- **"Is this a real word" check**: [scriptin/jmdict-simplified](https://github.com/scriptin/jmdict-simplified)
  (`jmdict-eng-common` release).

## Important caveat

**Official JLPT vocabulary lists have not been published since the exam format
changed in 2010.** These are widely-used community reconstructions, not an
official source. In particular the N1 list is a catch-all for "anything
difficult not on the N2–N5 lists," so it is by far the largest bucket
(3,427 rows vs. 1,812 for N2) — a heavy N1 skew in results is expected, not a
sign the data is broken.

## Regenerating

```
python scripts/build_jlpt_bank.py
```

Fetches both sources fresh and overwrites `jlpt_bank.json.gz`. Not run
automatically — regenerating is a deliberate, occasional action, not part of
every build.

## Format

```json
{
  "version": "1",
  "jlpt": {"会う\tあう": 5, "遭う\tあう": 2, ...},
  "jlpt_by_form": {"会う": 5, "遭う": 2, ...},
  "jlpt_en": {"会う\tあう": "to meet", "遭う\tあう": "to meet, to encounter (undesirable nuance)", ...},
  "common": ["会う", "青い", ...]
}
```

`jlpt`/`jlpt_en` keys are `"{form}\t{reading}"`; level is the JLPT N-number the
word was found on (**easiest** attested level, if a word appears on more than
one list). `jlpt_by_form` is a form-only fallback for when the tokenizer's
reading doesn't exactly match (e.g. katakana loanwords, where the bank's own
"reading" is the katakana headword itself, not a converted hiragana reading).
`common` is the flat set of common JMdict surface forms, used to decide
whether an unmatched word is real vocabulary (→ context-specific) or
tokenizer noise (→ dropped).
