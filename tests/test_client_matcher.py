"""Tests for app/client_matcher.py — fuzzy client name matching."""

import pytest
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
        sheet_name = "Іваненко Петро"
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
        """Score just below threshold should not auto-match."""
        sheet_name = "Test Name"
        folder_names = ["Test Name"]  # Will be 100 when normalized

        # Set threshold higher than any possible match
        result = match_client_name(
            sheet_name, folder_names, {}, auto_match_threshold=101.0
        )

        assert result.matched_folder_name is None
        assert result.confidence <= 100.0


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
