from decimal import Decimal

from analysis.portfolio import effective_number


def test_ten_equal_weights_behave_like_ten():
    assert effective_number({f"c{i}": 10 for i in range(10)}) == 10


def test_one_weight_is_one():
    assert effective_number({"United States": Decimal("1382.72")}) == 1


def test_uneven_weights_count_for_less_than_their_number():
    # 75/25: 1 / (0.75^2 + 0.25^2) = 1 / 0.625 = 1.6, not 2.
    assert effective_number({"A": 75, "B": 25}) == Decimal("1.6")


def test_excluded_categories_dont_change_the_rest():
    # Unclassified could hide any country, so it's left out and A and B are rescaled to 50/50.
    assert effective_number({"A": 50, "B": 50, "Unclassified": 100}, exclude=("Unclassified",)) == 2


def test_diffuse_rest_counts_in_the_total_but_not_as_one_block():
    # A fund's unlisted rest is hundreds of tiny positions: in the total, but adding ~0 to the squares.
    # A and B are 25% each of 200: 1 / (0.0625 + 0.0625) = 8.
    with_rest = effective_number({"A": 50, "B": 50, "Rest": 100}, diffuse=("Rest",))
    assert with_rest == 8
    assert with_rest >= effective_number({"A": 50, "B": 50})  # it raises N, never lowers it


def test_nothing_to_count_gives_none():
    assert effective_number({}) is None
    assert effective_number({"Unclassified": 10}, exclude=("Unclassified",)) is None
