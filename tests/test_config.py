from datetime import date

from ar_mis.config import split_into_chunks


def test_split_into_chunks_single_chunk_when_range_is_shorter_than_chunk_size():
    chunks = split_into_chunks(date(2026, 4, 1), date(2026, 4, 3), chunk_days=7)
    assert chunks == [(date(2026, 4, 1), date(2026, 4, 3))]


def test_split_into_chunks_starts_exactly_at_from_date_never_snapped():
    # 2026-04-01 is a Wednesday - must not be pulled back to any Monday.
    chunks = split_into_chunks(date(2026, 4, 1), date(2026, 4, 30), chunk_days=7)
    assert chunks[0][0] == date(2026, 4, 1)
    assert chunks[-1][1] == date(2026, 4, 30)


def test_split_into_chunks_covers_the_full_range_with_no_gap_or_overlap():
    chunks = split_into_chunks(date(2026, 4, 1), date(2026, 4, 30), chunk_days=7)
    for (_, end), (next_start, _) in zip(chunks, chunks[1:]):
        assert next_start == date.fromordinal(end.toordinal() + 1)
    assert chunks[0][0] == date(2026, 4, 1)
    assert chunks[-1][1] == date(2026, 4, 30)


def test_split_into_chunks_last_chunk_is_shorter_when_range_does_not_divide_evenly():
    # 30 days / 7-day chunks -> four full weeks (28 days) + one 2-day tail.
    chunks = split_into_chunks(date(2026, 4, 1), date(2026, 4, 30), chunk_days=7)
    assert len(chunks) == 5
    assert chunks[:4] == [
        (date(2026, 4, 1), date(2026, 4, 7)),
        (date(2026, 4, 8), date(2026, 4, 14)),
        (date(2026, 4, 15), date(2026, 4, 21)),
        (date(2026, 4, 22), date(2026, 4, 28)),
    ]
    assert chunks[4] == (date(2026, 4, 29), date(2026, 4, 30))


def test_split_into_chunks_a_single_day_range():
    assert split_into_chunks(date(2026, 4, 1), date(2026, 4, 1), chunk_days=7) == [
        (date(2026, 4, 1), date(2026, 4, 1))
    ]
