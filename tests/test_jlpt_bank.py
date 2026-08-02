from lib.jlpt_bank import lookup, is_real_word, gloss, scan

# The calibration list previously hard-coded into analyzer.py's prompt as
# words the LLM "must never label N1 or N2" — the bank should agree on its own.
CALIBRATION_WORDS = (
    "強い 国 問題 正しい 高い 使う 気をつけて 意味 驚く 得意 経験 成功 期待 冗談 "
    "結局 能力 尊敬 不思議 相手 注目 選択肢"
).split()


def test_calibration_words_grade_n3_or_easier():
    graded = {w: lookup(w) for w in CALIBRATION_WORDS}
    bad = {w: lv for w, lv in graded.items() if lv is not None and lv <= 2}
    assert not bad, f"words wrongly graded N2 or harder: {bad}"
    # At least most of them should actually be in the bank (a couple of
    # multi-kanji compounds like 選択肢 legitimately aren't).
    found = sum(1 for lv in graded.values() if lv is not None)
    assert found >= len(CALIBRATION_WORDS) - 2


def test_homographs_disambiguate_by_reading():
    # Same kana reading, different kanji, genuinely different JLPT levels.
    assert lookup("会う", "あう") == 5
    assert lookup("遭う", "あう") == 2
    assert lookup("相変わらず", "あいかわらず") == 1


def test_lookup_falls_back_to_form_only():
    # Katakana loanwords: the bank's own "reading" is the katakana headword
    # itself, not a hiragana conversion, so an exact (form, hiragana-reading)
    # key won't exist — the form-only fallback must still resolve it.
    assert lookup("アイデア") is not None


def test_unknown_word_returns_none():
    assert lookup("ゾグワッシュフルーガン") is None


def test_gloss_returns_english_definition():
    en = gloss("結局")
    assert en and isinstance(en, str)


def test_is_real_word():
    assert is_real_word("国債")  # real domain word, not on any JLPT list
    assert not is_real_word("ゾグワッシュフルーガン")


def test_scan_filters_noise_and_grades_by_tier():
    segments = [
        {"ja": "結局、彼は国債を買った。それはどういうことか分からない。"},
    ]
    candidates, ctx = scan(segments, ["N2", "N1"])
    cand_words = {c["word"] for c in candidates}
    # 結局 and 買う are N3/N5 — below the N2/N1 band, must not appear as candidates.
    assert "結局" not in cand_words
    assert "買う" not in cand_words
    # どういう is an explicit stop-form — must never appear anywhere.
    all_words = cand_words | {c["word"] for c in ctx}
    assert "どういう" not in all_words


def test_scan_puts_domain_words_in_context_bucket():
    segments = [{"ja": "国債の金利が上昇した。"}]
    candidates, ctx = scan(segments, ["N5"])  # band that excludes these words
    ctx_words = {c["word"] for c in ctx}
    assert "国債" in ctx_words
    for c in ctx:
        assert c["level"] == "context-specific"


def test_scan_tracks_surfaces_and_example():
    segments = [{"ja": "強い意志を持って強く戦った。"}]
    candidates, _ = scan(segments, ["N5"])
    strong = next((c for c in candidates if c["word"] == "強い"), None)
    assert strong is not None
    assert strong["count"] >= 2  # 強い appears twice in inflected forms
    assert strong["example"] == segments[0]["ja"]
