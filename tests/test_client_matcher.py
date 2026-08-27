"""Tests for app/client_matcher.py — fuzzy client name matching."""

from app.client_matcher import MatchResult, match_client_name


class TestConfirmedAliasShortCircuit:
    """Confirmed aliases should skip fuzzy matching entirely."""

    def test_confirmed_alias_returns_immediately(self):
        """Sheet name in known_aliases should return exact match with confidence=100."""
        sheet_name = "Іваненко Петро"
        folder_names = ["іваненко петро", "іваненко п."]  # Alternatives exist
        known_aliases = {sheet_name: "іваненко петро"}

        result = match_client_name(sheet_name, folder_names, known_aliases)

        assert result.sheet_name == "Іваненко Петро"
        assert result.matched_folder_name == "іваненко петро"
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is True
        assert result.candidates == [("іваненко петро", 100.0)]

    def test_confirmed_alias_ignores_other_candidates(self):
        """Confirmed alias should win even if folder_names has other close matches."""
        sheet_name = "Коваленко Іван"
        folder_names = [
            "коваленко іванович",  # Very similar to sheet name
            "коваленко іван",      # Exact match (but will be ignored)
        ]
        known_aliases = {"Коваленко Іван": "коваленко іван"}

        result = match_client_name(sheet_name, folder_names, known_aliases)

        assert result.matched_folder_name == "коваленко іван"
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is True

    def test_confirmed_alias_only_sheet_name_lookup(self):
        """Confirmed alias is keyed by exact sheet_name, not fuzzy-matched."""
        similar_name = "іваненко петро"  # Different case
        folder_names = ["іваненко петро"]
        known_aliases = {"Іваненко Петро": "іваненко петро"}  # Only this key

        result = match_client_name(similar_name, folder_names, known_aliases)

        # similar_name is not in known_aliases, so falls through to fuzzy matching
        # (not a confirmed match)
        assert result.is_confirmed_alias is False
        # But should still match via fuzzy logic
        assert result.matched_folder_name == "іваненко петро"


class TestNearExactMatch:
    """Case/whitespace differences should auto-match with high confidence."""

    def test_case_only_difference_auto_matches(self):
        """Sheet name differing only in case should auto-match above threshold."""
        sheet_name = "Іваненко Петро"
        folder_names = ["іваненко петро"]  # Only lowercase

        result = match_client_name(sheet_name, folder_names, {})

        assert result.matched_folder_name == "іваненко петро"
        assert result.confidence == 100.0  # Exact normalized match
        assert result.is_confirmed_alias is False
        assert len(result.candidates) == 1
        assert result.candidates[0] == ("іваненко петро", 100.0)

    def test_whitespace_normalization(self):
        """Leading/trailing spaces should be normalized before matching."""
        sheet_name = "  Коваленко Іван  "
        folder_names = ["коваленко іван"]  # No spaces

        result = match_client_name(sheet_name, folder_names, {})

        assert result.matched_folder_name == "коваленко іван"
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is False

    def test_combined_case_and_whitespace(self):
        """Both case and whitespace differences should normalize together."""
        sheet_name = "  СИДОРЕНКО МИКОЛА  "
        folder_names = ["сидоренко микола"]

        result = match_client_name(sheet_name, folder_names, {})

        assert result.matched_folder_name == "сидоренко микола"
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is False


class TestUnicodeNormalization:
    """Visually-identical Cyrillic text can arrive in different Unicode
    composition forms (e.g. from a sheet edited on one OS vs. an export
    folder created on another). Matching must not penalize this."""

    def test_nfd_decomposed_sheet_name_matches_nfc_folder(self):
        """Precomposed 'й' (U+0439) in the folder vs. decomposed 'и' + combining
        breve (U+0438 U+0306) in the sheet name should still be treated as an
        exact match, not scored as two different names."""
        import unicodedata

        nfc_name = unicodedata.normalize("NFC", "Гайдай Юрій")
        nfd_name = unicodedata.normalize("NFD", "Гайдай Юрій")
        assert nfc_name != nfd_name  # sanity: they really are different code points

        result = match_client_name(nfd_name, [nfc_name], {})

        assert result.matched_folder_name == nfc_name
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is False


class TestAmbiguousCandidates:
    """When top candidates are within ambiguous_margin, should not auto-match."""

    def test_two_similar_candidates_within_margin(self):
        """Two candidates with scores within ambiguous_margin should not auto-match."""
        sheet_name = "Коваленко І."
        folder_names = [
            "коваленко іван",   # Similar
            "коваленко ірина",  # Also similar
        ]

        result = match_client_name(
            sheet_name, folder_names, {}, auto_match_threshold=50.0, ambiguous_margin=15.0
        )

        # Both should score similarly, within margin of each other
        assert result.matched_folder_name is None
        # But best score should still be populated for UI
        assert result.confidence > 0.0
        # Candidates should show both options
        assert len(result.candidates) == 2
        assert result.candidates[0][1] >= result.candidates[1][1]

    def test_margin_prevents_close_match(self):
        """Scores differing by less than margin should not auto-match even if both high."""
        sheet_name = "Smith Johnson"
        folder_names = [
            "smith jonson",   # One char off
            "smith jonhson",  # Different one char off
        ]

        result = match_client_name(
            sheet_name, folder_names, {}, auto_match_threshold=80.0, ambiguous_margin=20.0
        )

        # Both candidates should be close enough to trigger ambiguity
        if len(result.candidates) >= 2:
            score_diff = result.candidates[0][1] - result.candidates[1][1]
            if score_diff < 20.0:
                # If margin not met, should not auto-match
                assert result.matched_folder_name is None


class TestBelowThreshold:
    """Scores below auto_match_threshold should not match."""

    def test_no_similar_folder_returns_none(self):
        """Completely different name should return None with low confidence."""
        sheet_name = "Іваненко Петро"
        folder_names = ["Сидоренко Микола"]  # Completely different

        result = match_client_name(sheet_name, folder_names, {})

        assert result.matched_folder_name is None
        # Confidence should be the fuzzy score (low)
        assert result.confidence < 90.0
        assert result.is_confirmed_alias is False

    def test_threshold_boundary(self):
        """Score exactly at threshold should auto-match."""
        # Create a scenario where we can control the score
        sheet_name = "Test"
        folder_names = ["Test"]  # Exact match = 100

        result = match_client_name(
            sheet_name, folder_names, {}, auto_match_threshold=100.0
        )

        # Should auto-match at exactly threshold
        assert result.matched_folder_name == "Test"
        assert result.confidence == 100.0

    def test_just_below_threshold_does_not_match(self):
        """A fuzzy (non-exact) score below threshold should not auto-match.
        An exact whole-name match is deliberately exempt from the threshold
        (see match_client_name's exact-match shortcut), so use a near-miss."""
        sheet_name = "Testtt"
        folder_names = ["Different"]

        result = match_client_name(
            sheet_name, folder_names, {}, auto_match_threshold=95.0
        )

        assert result.matched_folder_name is None
        assert result.confidence < 95.0


class TestEmptyFolderNames:
    """Empty folder list should return documented empty result."""

    def test_empty_folder_list(self):
        """Empty folder_names should return None match with no candidates."""
        sheet_name = "Іваненко Петро"

        result = match_client_name(sheet_name, [], {})

        assert result.sheet_name == "Іваненко Петро"
        assert result.matched_folder_name is None
        assert result.confidence == 0.0
        assert result.is_confirmed_alias is False
        assert result.candidates == []

    def test_empty_folders_with_confirmed_alias(self):
        """Confirmed alias should still work even with empty folder list."""
        sheet_name = "Іваненко Петро"
        known_aliases = {sheet_name: "іваненко петро"}

        result = match_client_name(sheet_name, [], known_aliases)

        assert result.matched_folder_name == "іваненко петро"
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is True


class TestCandidatesCapAndSort:
    """Candidates should be capped at 3 and sorted best-first."""

    def test_candidates_capped_at_three(self):
        """When more than 3 folders given, only top 3 should be in candidates."""
        sheet_name = "Петро"
        folder_names = [
            "петро",
            "петрова",
            "петрова анна",
            "петренко",
            "петровський",
        ]

        result = match_client_name(sheet_name, folder_names, {})

        assert len(result.candidates) == 3

    def test_candidates_sorted_best_first(self):
        """Candidates should be sorted by score descending."""
        sheet_name = "Коваленко"
        folder_names = ["коваленко", "коваленко і.", "коваленко іван"]

        result = match_client_name(sheet_name, folder_names, {})

        # Check candidates are sorted best-first
        for i in range(len(result.candidates) - 1):
            assert result.candidates[i][1] >= result.candidates[i + 1][1]

    def test_single_folder_returns_one_candidate(self):
        """Single folder should result in one candidate."""
        sheet_name = "Test"
        folder_names = ["test"]

        result = match_client_name(sheet_name, folder_names, {})

        assert len(result.candidates) == 1
        assert result.candidates[0] == ("test", 100.0)


class TestMatchResultDataclass:
    """MatchResult should have correct structure."""

    def test_match_result_fields(self):
        """MatchResult should have all expected fields."""
        result = MatchResult(
            sheet_name="Тест",
            matched_folder_name="тест",
            confidence=95.5,
            is_confirmed_alias=False,
            candidates=[("тест", 95.5)],
        )

        assert result.sheet_name == "Тест"
        assert result.matched_folder_name == "тест"
        assert result.confidence == 95.5
        assert result.is_confirmed_alias is False
        assert result.candidates == [("тест", 95.5)]


class TestIntegrationScenarios:
    """Real-world matching scenarios combining multiple aspects."""

    def test_export_folder_real_scenario(self):
        """Real export scenario: sheet client names vs folder names."""
        sheet_name = "Іваненко Петро"
        folder_names = [
            "іваненко петро ",  # Extra trailing space
            "іваненко п.",      # Abbreviated
            "сидоренко микола",  # Unrelated
        ]

        result = match_client_name(sheet_name, folder_names, {})

        # Should auto-match the first one despite trailing space
        assert result.matched_folder_name == "іваненко петро "
        assert result.confidence == 100.0
        assert result.is_confirmed_alias is False

    def test_typo_recovery_with_fuzzy(self):
        """Common typos should still fuzzy-match reasonably."""
        sheet_name = "Коваленко Иван"  # Cyrillic И instead of І
        folder_names = ["коваленко іван"]  # Correct spelling

        result = match_client_name(sheet_name, folder_names, {})

        # Should still get a reasonable match (may not be auto-match due to typo)
        assert result.candidates[0][0] == "коваленко іван"
        # Score will depend on how different И and І are
        assert result.confidence > 0.0

    def test_workflow_confirm_then_reuse(self):
        """Once confirmed via alias dict, same name should skip fuzzy."""
        sheet_name = "Клієнт Тест"
        folder_names = ["клієнт тест"]
        # First call: no alias, fuzzy matches
        result1 = match_client_name(sheet_name, folder_names, {})
        # Second call: with confirmed alias
        known_aliases = {sheet_name: "клієнт тест"}
        result2 = match_client_name(sheet_name, folder_names, known_aliases)

        assert result1.is_confirmed_alias is False
        assert result2.is_confirmed_alias is True
        assert result2.confidence == 100.0

    def test_ambiguous_resolved_by_alias(self):
        """Ambiguous fuzzy match can be resolved by adding a confirmed alias."""
        sheet_name = "Коваленко І."
        folder_names = ["коваленко іван", "коваленко ірина"]

        # Without alias: ambiguous
        result1 = match_client_name(sheet_name, folder_names, {})
        assert result1.matched_folder_name is None

        # With confirmed alias: resolved
        known_aliases = {sheet_name: "коваленко іван"}
        result2 = match_client_name(sheet_name, folder_names, known_aliases)
        assert result2.matched_folder_name == "коваленко іван"
        assert result2.is_confirmed_alias is True


class TestRealWorldNaming:
    """Naming the lab actually uses: surname always present, folder may be
    surname-only vs sheet's name+surname, Cyrillic vs Latin either side."""

    def test_surname_only_folder_matches_name_plus_surname_sheet(self):
        r = match_client_name("Петро Мулик", ["Мулик", "Сидоренко"], {})
        assert r.matched_folder_name == "Мулик"
        assert r.confidence >= 90

    def test_name_plus_surname_folder_matches_surname_only_sheet(self):
        r = match_client_name("Мулик", ["Петро Мулик", "Іван Кужим"], {})
        assert r.matched_folder_name == "Петро Мулик"

    def test_cyrillic_sheet_matches_latin_folder(self):
        r = match_client_name("Дяченко", ["Dyachenko", "Petrenko"], {})
        assert r.matched_folder_name == "Dyachenko"
        assert r.confidence >= 90

    def test_latin_sheet_matches_cyrillic_folder(self):
        r = match_client_name("Pavlenko", ["Павленко", "Науменко"], {})
        assert r.matched_folder_name == "Павленко"

    def test_transliteration_style_variation(self):
        # ya-style folder vs official-style expectation
        r = match_client_name("Юрій Ящук", ["Yashchuk", "Kovalenko"], {})
        assert r.matched_folder_name == "Yashchuk"

    def test_different_surnames_do_not_match(self):
        r = match_client_name("Мулик", ["Kuzhym", "Petrenko"], {})
        assert r.matched_folder_name is None
        assert r.confidence < 90

    def test_surname_match_wins_over_unrelated_shorter_string(self):
        r = match_client_name(
            "Олександр Підгорний", ["Підгорний", "Оля", "Стоянов"], {}
        )
        assert r.matched_folder_name == "Підгорний"


class TestMatcherPerformance:
    """Регрес-гард для «GET /handout took 1652s» (бойовий лог 27.08.26).

    Вузьким місцем виявився не мережевий диск, а саме зіставлення імен:
    _score_pair коштує ~30 викликів rapidfuzz на пару, і його ганяли по
    СОТНЯХ тек для КОЖНОГО клієнта."""

    def test_expensive_scorer_runs_only_on_shortlisted_candidates(self, monkeypatch):
        from app import client_matcher

        folders = [f"Клієнт {i:03d}" for i in range(400)] + ["Кривовид"]
        calls = []
        real = client_matcher._score_pair

        def counting(a, b):
            calls.append(b)
            return real(a, b)

        monkeypatch.setattr(client_matcher, "_score_pair", counting)
        client_matcher.match_client_name("Кривовид", folders, {})

        assert len(calls) <= 41, (
            f"дорогий скорер мав пройти по короткому списку, а пройшов "
            f"по {len(calls)} з {len(folders)} тек"
        )

    def test_exact_match_survives_the_prefilter(self):
        """Точний збіг не має загубитись у дешевому проході — на ньому
        тримається гілка exact."""
        from app.client_matcher import match_client_name

        folders = [f"Інший {i:03d}" for i in range(300)] + ["Басараб"]
        result = match_client_name("Басараб", folders, {})
        assert result.matched_folder_name == "Басараб"
        assert result.confidence == 100.0

    def test_repeated_names_are_normalised_once_not_per_comparison(self):
        """Ім'я нормалізується РАЗ і далі береться з кешу. Без цього одні й ті
        самі рядки перебудовувались мільйони разів за запит — саме це й давало
        хвилини очікування."""
        from app import client_matcher

        client_matcher._normalize.cache_clear()
        folders = [f"Кривовид {i:03d}" for i in range(50)]
        names = ["Кривовид", "Кривовид кл", "Кривовид лаб"] * 8
        for name in names:
            client_matcher.match_client_name(name, folders, {})

        info = client_matcher._normalize.cache_info()
        assert info.misses <= len(folders) + len(set(names)) + 5, (
            f"кожен рядок мав нормалізуватись один раз: {info}"
        )
        assert info.hits > info.misses * 10, f"кеш мав давати влучення: {info}"

    def test_dissimilar_candidates_never_reach_the_expensive_scorer(self):
        """Дешевий відсів відкидає явно несхожі теки ще до дорогого скорера."""
        from app import client_matcher

        client_matcher._transliterations_cached.cache_clear()
        folders = [f"Клієнт {i:03d}" for i in range(50)]
        client_matcher.match_client_name("Кривовид", folders, {})

        assert client_matcher._transliterations_cached.cache_info().misses == 0, (
            "жодна з несхожих тек не мала дійти до транслітерації"
        )
