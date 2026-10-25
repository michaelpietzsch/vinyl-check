"""Record grade from its sides: average of the grade steps, half steps toward the worse grade, F if a side skips."""
from vinylcheck.grading import record_grade


def test_record_grade_is_the_side_average():
    assert record_grade(["NM", "NM"]) == "NM"
    assert record_grade(["NM", "VG"]) == "VG+"           # (0 + 2) / 2 = 1
    assert record_grade(["NM", "VG+"]) == "VG+"          # 0.5 → toward the worse grade
    assert record_grade(["VG+", "G"]) == "G+"            # (1 + 4) / 2 = 2.5 → 3
    assert record_grade(["VG", "VG"]) == "VG"


def test_record_grade_edge_cases():
    assert record_grade(["NM", "F"]) == "F"              # one side skips → the record skips
    assert record_grade(["NM"]) == "NM"                  # only one side measured
    assert record_grade([None, "VG"]) == "VG"            # an ungraded side does not count
    assert record_grade([]) is None and record_grade([None]) is None
