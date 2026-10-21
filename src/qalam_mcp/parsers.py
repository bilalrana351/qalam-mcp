"""Parse portal HTML without treating missing marks as zero or estimating official results."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .errors import QalamError
from .session import HOST, ORIGIN


def text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def number(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?\s*%?", value):
        return float(value.rstrip("% "))
    return None


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def page_state(html: str) -> str:
    doc = soup(html)
    title = text(doc.title).lower()
    body = text(doc).lower()
    if "cloudflare" in title and (
        "attention required" in title or "sorry, you have been blocked" in body
    ):
        return "blocked"
    if (
        "just a moment" in title
        or "cf-chl" in html
        or "performing security verification" in body
        or "verify you are human" in body
    ):
        return "challenge"
    if doc.select_one('input[type="password"]'):
        return "login"
    return "content"


def is_student_page(html: str) -> bool:
    return bool(soup(html).select_one('a[href*="/student/"], a[href*="/web/session/logout"]'))


def safe_path(href: str) -> str | None:
    parsed = urlsplit(urljoin(ORIGIN + "/", href))
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (443, None):
        return None
    if parsed.username or parsed.password or parsed.query or ".." in parsed.path:
        return None
    return parsed.path


def courses(html: str) -> list[dict]:
    doc = soup(html)
    found: dict[str, dict] = {}
    for link in doc.select("a[href]"):
        path = safe_path(link["href"])
        match = re.fullmatch(r"/student/course/(?:info|attendance|gradebook)/(\d+)/?", path or "")
        if not match:
            continue
        course_id = match[1]
        card = link.select_one(".card, .md-card")
        if not card:
            card = link.find_parent(class_="md-card") or link
        heading = card.select_one(".card-header") or card.select_one(".card-title, h3, h4, h5")
        name = text(heading) or text(link)
        if not name:
            continue
        item = found.setdefault(course_id, {"course_id": course_id, "name": name})
        if heading:
            item["name"] = name
        details = text(card)
        code = re.search(r"\b[A-Z]{2,6}[- ]\d{2,4}\b", details)
        credits = re.search(r"Credit(?:\s+Hours?)?s?\s*:\s*(\d+(?:\.\d+)?)", details, re.I)
        if code:
            item["code"] = code.group()
        if credits:
            item["credit_hours"] = float(credits[1])
        if card.select_one(".card-header") and card.select_one(".card-body .card-title"):
            item["instructor"] = text(card.select_one(".card-body .card-title"))
        reported = re.search(r"Attendance\s*:\s*(\d+(?:\.\d+)?)\s*%", details, re.I)
        if reported:
            item["dashboard_attendance_percentage"] = float(reported[1])
        term = re.search(r"\b(?:Spring|Summer|Fall|Winter)\s+\d{4}\b", details, re.I)
        if term:
            item["term"] = term.group()
        item["info_url"] = f"{ORIGIN}/student/course/info/{course_id}"
    return list(found.values())


def components(doc: BeautifulSoup) -> list[tuple[str, Tag | BeautifulSoup]]:
    panels = doc.select("#tabs_anim1 > li")
    root = doc.select_one("#tabs_anim1")
    tabs = root.find_previous(class_="uk-tab") if root else None
    # Exclude the responsive dropdown clone of tab labels.
    labels = [
        text(li)
        for li in (tabs.find_all("li", recursive=False) if tabs else [])
        if "uk-tab-responsive" not in li.get("class", [])
    ]
    if not panels:
        return [("Course", doc)]
    return [
        (labels[index] if index < len(labels) else f"Component {index + 1}", panel)
        for index, panel in enumerate(panels)
    ]


def attendance(html: str) -> dict:
    results = []
    for name, panel in components(soup(html)):
        counts: dict[str, int] = {}
        summary_labels = set()
        for item in panel.select(".md-card-list li"):
            label = text(item)
            match = re.search(r"Number of classes\s+(Conducted|Attended)", label, re.I)
            if not match:
                continue
            summary_labels.add(match[1].lower())
            value = number(text(item.select_one("span")))
            if value is None:
                suffix = re.search(r"(?:Conducted|Attended)\s*:?\s*(\d+)\s*$", label, re.I)
                value = float(suffix[1]) if suffix else None
            if value is not None and value.is_integer():
                counts[match[1].lower()] = int(value)
        if counts:
            if not {"conducted", "attended"} <= counts.keys():
                raise QalamError("PORTAL_CHANGED", "Qalam returned incomplete attendance counts.")
            attended, conducted = counts["attended"], counts["conducted"]
            if not 0 <= attended <= conducted:
                raise QalamError(
                    "DATA_INCONSISTENT", "Qalam returned inconsistent attendance counts."
                )
            results.append(
                {
                    "component": name,
                    "attended": attended,
                    "conducted": conducted,
                    "absent": conducted - attended,
                    "percentage": round(100 * attended / conducted, 2) if conducted else None,
                }
            )
        elif (
            summary_labels == {"conducted", "attended"}
            and "attendance not marked" in text(panel).lower()
        ):
            results.append(
                {
                    "component": name,
                    "attended": None,
                    "conducted": None,
                    "absent": None,
                    "percentage": None,
                    "status": "not_marked",
                }
            )
    if not results:
        raise QalamError(
            "PORTAL_CHANGED", "Attendance summary was not found. Portal markup may have changed."
        )
    complete = all(c["conducted"] is not None and c["attended"] is not None for c in results)
    attended = sum(c["attended"] for c in results) if complete else None
    conducted = sum(c["conducted"] for c in results) if complete else None
    return {
        "components": results,
        "combined": {
            "attended": attended,
            "conducted": conducted,
            "percentage": round(100 * attended / conducted, 2) if conducted else None,
        },
        "note": "Combined percentage is calculated from class counts; components remain separate.",
    }


def _cells(row: Tag) -> list[Tag]:
    return row.find_all(["td", "th"], recursive=False)


def _record(headers: list[str], values: list[str]) -> dict:
    output: dict[str, str] = {}
    for index, value in enumerate(values):
        key = headers[index] if index < len(headers) and headers[index] else f"column_{index + 1}"
        if key in output:
            key = f"{key}_{index + 1}"
        output[key] = value
    return output


def _mark(values: list[str], headers: list[str], kind: str) -> float | None:
    if not headers:
        # Observed Qalam column order, used only when no column headings are present.
        index = {"maximum": 1, "obtained": 2, "average": 3}[kind]
        return number(values[index]) if index < len(values) else None
    aliases = {
        "maximum": r"(?:total|max(?:imum)?)\s*(?:marks?|score)?|marks?\s*(?:total|max(?:imum)?)",
        "obtained": r"(?:obtained|your|student)\s*(?:marks?|score)?|marks?\s*obtained",
        "average": r"(?:class\s*)?(?:avg\.?|average)(?:\s*(?:marks?|score))?",
    }
    for index, header in enumerate(headers):
        normalized = header.strip().rstrip(":")
        if index < len(values) and re.fullmatch(aliases[kind], normalized, re.I):
            return number(values[index])
    # Unexpected headers are preserved in portal_fields, without guessing their meaning.
    return None


def gradebook(html: str, assessment: str | None = None) -> dict:
    categories = []
    all_category_count = 0
    for component, panel in components(soup(html)):
        for parent in panel.select("tr.table-parent-row"):
            cells = _cells(parent)
            if not cells:
                continue
            all_category_count += 1
            label = soup(str(cells[0]))
            for badge in label.select(".uk-badge"):
                badge.decompose()
            name = text(label)
            weight = number(text(cells[0].select_one(".uk-badge")))
            entries = []
            headers: list[str] = []
            for sibling in parent.next_siblings:
                if not isinstance(sibling, Tag):
                    continue
                if "table-child-row" not in sibling.get("class", []):
                    break
                row_cells = _cells(sibling)
                if not row_cells:
                    continue
                if sibling.find("th", recursive=False):
                    headers = [text(c) for c in row_cells]
                    continue
                values = [text(c) for c in row_cells]
                if len(values) < 3:
                    continue
                if assessment and assessment.casefold() not in (name + " " + values[0]).casefold():
                    continue
                # Named headers take priority over the observed fallback column order.
                entry = {
                    "name": values[0],
                    "maximum_marks": _mark(values, headers, "maximum"),
                    "obtained_marks": _mark(values, headers, "obtained"),
                    "class_average": _mark(values, headers, "average"),
                    "portal_fields": _record(headers, values),
                }
                entries.append(entry)
            if not assessment or entries or assessment.casefold() in name.casefold():
                categories.append(
                    {
                        "component": component,
                        "category": name,
                        "weight_percent": weight,
                        "portal_summary": [text(c) for c in cells[1:]],
                        "assessments": entries,
                    }
                )
    doc = soup(html)
    empty_table = any(
        {text(th).casefold() for th in table.select("th")}
        >= {"assessment type", "obtained percentage"}
        for table in doc.select("table")
    )
    if not all_category_count and not empty_table:
        raise QalamError(
            "PORTAL_CHANGED", "Gradebook categories were not found. Portal markup may have changed."
        )
    return {
        "categories": categories,
        "has_categories": bool(all_category_count),
        "assessment_filter": assessment,
        "note": "Null marks mean blank or unavailable. Zeros are Qalam's displayed values and "
        "may be ungraded placeholders. No final grade or publication status is inferred.",
    }


def section_path(html: str, section: str) -> str:
    patterns = {
        "results": r"/student/results(?:/[A-Za-z0-9_-]+)*/?",
        "timetable": r"/student/(?:class/schedule|timetable|schedule)(?:/[A-Za-z0-9_-]+)*/?",
    }
    labels = {
        "results": r"\b(results?|grades?)\b",
        "timetable": r"\b(time\s*table|schedule)\b",
    }
    for link in soup(html).select("a[href]"):
        path = safe_path(link["href"])
        if (
            path
            and re.fullmatch(patterns[section], path)
            and re.search(labels[section], text(link), re.I)
        ):
            return path
    raise QalamError(
        "SECTION_UNAVAILABLE", f"No supported {section} link was found in your portal."
    )


def table_page(html: str) -> dict:
    doc = soup(html)
    tables = []
    for table in doc.select("table"):
        headers: list[str] = []
        rows = []
        for row in table.select("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = _cells(row)
            if not cells:
                continue
            if row.find("th", recursive=False):
                headers = [text(c) for c in cells]
            else:
                rows.append(_record(headers, [text(c) for c in cells]))
        if rows:
            tables.append({"headers": headers, "rows": rows})
    cards = []
    for card in doc.select(".md-card, .card"):
        if card.find_parent(class_="md-card") or card.find_parent(class_="card"):
            continue
        value = text(card)
        if value:
            cards.append(value)
    if not tables and not cards:
        raise QalamError("PORTAL_CHANGED", "No supported result cards or table rows were found.")
    return {"tables": tables, "cards": cards}
