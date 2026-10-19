import pytest

from qalam_mcp import parsers
from qalam_mcp.errors import QalamError


def test_courses_and_origin_filter(fixtures):
    html = fixtures["dashboard"] + '<a href="https://evil.test/student/course/info/9">Bad</a>'
    found = parsers.courses(html)
    assert [c["course_id"] for c in found] == ["123", "456"]
    assert found[0]["code"] == "CS-101"
    assert found[0]["credit_hours"] == 4
    assert found[0]["name"] == "Introduction to Computing"
    assert found[0]["instructor"] == "Example Faculty"
    assert found[0]["term"] == "Fall 2026"


def test_attendance_components_are_not_blended(fixtures):
    result = parsers.attendance(fixtures["attendance"])
    assert result["components"] == [
        {"component": "Lecture", "attended": 15, "conducted": 20, "absent": 5, "percentage": 75},
        {"component": "Lab", "attended": 3, "conducted": 4, "absent": 1, "percentage": 75},
    ]
    assert result["combined"]["percentage"] == 75


def test_no_classes_is_unknown_not_zero(fixtures):
    html = fixtures["attendance"]
    for value in ("20", "15", "4", "3"):
        html = html.replace(f"<span>{value}</span>", "<span>0</span>")
    assert parsers.attendance(html)["combined"]["percentage"] is None


def test_inconsistent_counts_do_not_report_a_percentage(fixtures):
    html = fixtures["attendance"].replace("<span>15</span>", "<span>21</span>")
    with pytest.raises(QalamError, match="inconsistent"):
        parsers.attendance(html)


def test_grades_keep_blank_and_zero_different_and_separate_lab(fixtures):
    result = parsers.gradebook(fixtures["gradebook"])
    quiz, midterm, lab = result["categories"]
    assert quiz["weight_percent"] == 20
    assert quiz["assessments"][0]["obtained_marks"] == 0
    assert quiz["assessments"][1]["obtained_marks"] == 8
    assert quiz["assessments"][2]["obtained_marks"] is None
    assert midterm["category"] == "Midterm"
    assert lab["component"] == "Lab"


def test_assessment_and_category_filter(fixtures):
    result = parsers.gradebook(fixtures["gradebook"], "quiz 2")
    assert len(result["categories"]) == 1
    assert [a["name"] for a in result["categories"][0]["assessments"]] == ["Quiz 2"]
    assert parsers.gradebook(fixtures["gradebook"], "absent-assessment")["categories"] == []


def test_changed_grade_column_order_uses_headings():
    html = """<table><tr class="table-parent-row"><td>Quizzes</td></tr>
    <tr class="table-child-row"><th>Assessment</th><th>Obtained Marks</th><th>Class Average</th>
    <th>Total Marks</th></tr><tr class="table-child-row"><td>Quiz 1</td><td>8</td>
    <td>6</td><td>10</td></tr></table>"""
    item = parsers.gradebook(html)["categories"][0]["assessments"][0]
    assert item["maximum_marks"] == 10
    assert item["obtained_marks"] == 8
    assert item["class_average"] == 6


@pytest.mark.parametrize("parse", [parsers.attendance, parsers.gradebook, parsers.table_page])
def test_changed_markup_is_explicit_error(parse):
    with pytest.raises(QalamError) as caught:
        parse("<html><body>New portal version</body></html>")
    assert caught.value.code == "PORTAL_CHANGED"


def test_unmarked_attendance_and_empty_gradebook_are_known_empty_states():
    html = """<ul class="md-card-list"><li>Number of classes Conducted : <span></span></li>
    <li>Number of classes Attended : <span></span></li></ul>
    <table><tr><td>Attendance Not Marked</td></tr></table>"""
    result = parsers.attendance(html)
    assert result["components"][0]["status"] == "not_marked"
    assert result["combined"]["percentage"] is None
    html = "<table><tr><th>Assessment Type</th><th>Obtained Percentage</th></tr></table>"
    result = parsers.gradebook(html)
    assert result["categories"] == []
    assert result["has_categories"] is False


def test_section_links_are_discovered_and_restricted(fixtures):
    assert parsers.section_path(fixtures["dashboard"], "results") == "/student/results/"
    for href in [
        "https://evil.test/student/results",
        "/student/results?delete=1",
        "/student/drop/1",
        "javascript:alert(1)",
    ]:
        with pytest.raises(QalamError):
            parsers.section_path(f'<a href="{href}">Results</a>', "results")
