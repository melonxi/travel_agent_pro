from memory.destination_normalization import match_destination, normalize_destination


def test_normalize_destination_resolves_tokyo_alias():
    normalized = normalize_destination("東京")

    assert normalized.canonical == "东京"
    assert "東京" in normalized.aliases
    assert normalized.region == "关东"


def test_match_destination_returns_exact_for_same_canonical_input():
    match = match_destination("京都", "京都")

    assert match.match_type == "exact"
    assert match.score == 1.0


def test_match_destination_returns_alias_for_alias_and_canonical():
    match = match_destination("東京", "东京")

    assert match.match_type == "alias"
    assert match.score == 0.95


def test_match_destination_returns_parent_child_for_region_and_city():
    match = match_destination("关西", "京都")

    assert match.match_type == "parent_child"
    assert match.score == 0.75


def test_match_destination_returns_region_weak_for_same_region_siblings():
    match = match_destination("大阪", "京都")

    assert match.match_type == "region_weak"
    assert match.score == 0.35


def test_match_destination_returns_none_for_unrelated_destinations():
    match = match_destination("巴黎", "京都")

    assert match.match_type == "none"
    assert match.score == 0.0
