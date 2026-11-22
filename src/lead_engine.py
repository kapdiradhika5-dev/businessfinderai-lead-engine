
import csv
import os
import re
import random
import socket
import urllib.request
import urllib.robotparser
import urllib.error
import dns.resolver
import osmium

from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, quote_plus
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

LIMIT = 30

MAX_NO_WEBSITE = 500
MAX_WEAK_WEBSITE = 300
MAX_INCOMPLETE_WEBSITE = 200
MAX_GOOD_WEBSITE = 100

PBF_FILE = "india-latest.osm.pbf"
DAILY_CSV = "business_leads.csv"
DASHBOARD_CSV = "public_leads.csv"
HISTORY_FILE = "seen_businesses.txt"

USER_AGENT = "BusinessFinderAI/1.0 (business website opportunity research)"
REQUEST_TIMEOUT = 5
ROBOTS_TIMEOUT = 4
MAX_PAGE_BYTES = 400_000
MAX_CONTACT_PAGES = 2

ROOT_DIR = Path(__file__).resolve().parent.parent

PBF_PATH = ROOT_DIR / PBF_FILE
DAILY_PATH = ROOT_DIR / DAILY_CSV
DASHBOARD_PATH = ROOT_DIR / DASHBOARD_CSV
HISTORY_PATH = ROOT_DIR / HISTORY_FILE

random.seed()


FIELDS = [
    "osm_id",
    "name",
    "category",
    "subcategory",
    "address",
    "city",
    "state",
    "phone",
    "email",
    "website",
    "website_search_url",
    "website_discovery_status",
    "website_status",
    "website_opportunity",
    "opportunity_reason",
    "phone_source",
    "email_source",
    "email_status",
    "contact_priority",
    "lead_score",
    "latitude",
    "longitude",
    "osm_url",
]


# --------------------------------------------------
# GENERAL HELPERS
# --------------------------------------------------

def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def normalize_phone(value):
    digits = re.sub(r"\D", "", clean_text(value))

    if len(digits) < 8 or len(digits) > 15:
        return ""

    return digits


def normalize_email(value):
    value = clean_text(value).lower()

    if not re.fullmatch(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        value,
        re.IGNORECASE
    ):
        return ""

    return value


def normalize_website(value):
    value = clean_text(value)

    if not value:
        return ""

    if value.startswith("//"):
        value = "https:" + value

    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value

    try:
        parsed = urlparse(value)

        if parsed.scheme.lower() not in ("http", "https"):
            return ""

        if not parsed.hostname:
            return ""

        if " " in parsed.hostname:
            return ""

        if parsed.username or parsed.password:
            return ""

        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            "",
            parsed.query,
            ""
        ))

    except Exception:
        return ""


def website_domain(value):
    try:
        return (urlparse(value).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def get_first(tags, keys):
    for key in keys:
        value = clean_text(tags.get(key, ""))

        if value:
            return value

    return ""


# --------------------------------------------------
# ADDRESS AND CATEGORY
# --------------------------------------------------

def get_address(tags):
    full_address = clean_text(tags.get("addr:full", ""))

    if full_address:
        return full_address

    parts = []

    for key in (
        "addr:housenumber",
        "addr:street",
        "addr:suburb",
        "addr:district",
        "addr:city"
    ):
        value = clean_text(tags.get(key, ""))

        if value and value not in parts:
            parts.append(value)

    return ", ".join(parts)


def get_category(tags):
    category_keys = [
        "shop",
        "amenity",
        "office",
        "healthcare",
        "craft",
        "tourism"
    ]

    for key in category_keys:
        value = clean_text(tags.get(key, ""))

        if value:
            return key, value

    return "", ""


def get_website(tags):
    return normalize_website(get_first(tags, [
        "website",
        "contact:website",
        "url"
    ]))


def get_phone(tags):
    value = get_first(tags, [
        "contact:phone",
        "phone",
        "contact:mobile",
        "mobile"
    ])

    return clean_text(value)


def get_email(tags):
    value = get_first(tags, [
        "contact:email",
        "email"
    ])

    return normalize_email(value)


# --------------------------------------------------
# CANDIDATE SAMPLING
# --------------------------------------------------

class IndiaBusinessHandler(osmium.SimpleHandler):

    def __init__(self):
        super().__init__()

        self.pools = {
            "no_website": [],
            "weak_website": [],
            "incomplete_website": [],
            "good_website": []
        }

        self.counts = {
            "no_website": 0,
            "weak_website": 0,
            "incomplete_website": 0,
            "good_website": 0
        }

        self.total_businesses = 0

    def add_to_pool(self, pool_name, candidate, maximum):
        self.counts[pool_name] += 1
        count = self.counts[pool_name]

        pool = self.pools[pool_name]

        if len(pool) < maximum:
            pool.append(candidate)
            return

        position = random.randint(0, count - 1)

        if position < maximum:
            pool[position] = candidate

    def node(self, node):

        if not node.location.valid():
            return

        tags = {tag.k: tag.v for tag in node.tags}

        name = clean_text(tags.get("name", ""))

        if not name:
            return

        category, subcategory = get_category(tags)

        if not category:
            return

        self.total_businesses += 1

        website = get_website(tags)
        phone = get_phone(tags)
        email = get_email(tags)

        candidate = {
            "osm_id": f"node/{node.id}",
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "address": get_address(tags),
            "city": get_first(tags, [
                "addr:city",
                "is_in:city",
                "addr:town"
            ]),
            "state": get_first(tags, [
                "addr:state",
                "is_in:state"
            ]),
            "phone": phone,
            "email": email,
            "website": website,
            "phone_source": "OpenStreetMap" if phone else "",
            "email_source": "OpenStreetMap" if email else "",
            "latitude": str(node.location.lat),
            "longitude": str(node.location.lon),
            "osm_url": f"https://www.openstreetmap.org/node/{node.id}"
        }

        if not website:
            self.add_to_pool(
                "no_website",
                candidate,
                MAX_NO_WEBSITE
            )

        elif not phone and not email:
            self.add_to_pool(
                "weak_website",
                candidate,
                MAX_WEAK_WEBSITE
            )

        elif not phone or not email:
            self.add_to_pool(
                "incomplete_website",
                candidate,
                MAX_INCOMPLETE_WEBSITE
            )

        else:
            self.add_to_pool(
                "good_website",
                candidate,
                MAX_GOOD_WEBSITE
            )


def get_osm_data():

    if not PBF_PATH.exists():
        raise FileNotFoundError(
            f"India OSM file not found: {PBF_PATH}"
        )

    print("Reading India OpenStreetMap data...")

    handler = IndiaBusinessHandler()

    handler.apply_file(str(PBF_PATH), locations=True)

    print("\nOpenStreetMap business records:", handler.total_businesses)

    for name, count in handler.counts.items():
        print(f"{name}: {count}")

    candidates = []

    for pool_name in (
        "no_website",
        "weak_website",
        "incomplete_website",
        "good_website"
    ):
        candidates.extend(handler.pools[pool_name])

    random.shuffle(candidates)

    print("Sampled candidates:", len(candidates))

    return candidates


# --------------------------------------------------
# DUPLICATE HISTORY
# --------------------------------------------------

def load_history():

    history = set()

    if not HISTORY_PATH.exists():
        return history

    with open(
        HISTORY_PATH,
        "r",
        encoding="utf-8"
    ) as file:

        for line in file:
            value = line.strip()

            if value:
                # Old plain OSM IDs remain supported.
                history.add(value)

    return history


def business_signatures(row):

    signatures = set()

    name = normalize_text(row.get("name", ""))

    if not name:
        return signatures

    phone = normalize_phone(row.get("phone", ""))
    email = normalize_email(row.get("email", ""))
    domain = website_domain(row.get("website", ""))
    address = normalize_text(row.get("address", ""))
    city = normalize_text(row.get("city", ""))

    if phone:
        signatures.add(f"phone:{name}|{phone}")

    if email:
        signatures.add(f"email:{name}|{email}")

    if domain:
        signatures.add(f"website:{name}|{domain}")

    if address:
        signatures.add(f"address:{name}|{address}")

    # Name + city alone is intentionally not used:
    # different branches may have the same name.

    return signatures


def already_seen(row, history):

    osm_id = clean_text(row.get("osm_id", ""))

    if osm_id and osm_id in history:
        return True

    return bool(business_signatures(row) & history)


def save_history(rows):

    existing = load_history()

    new_values = set()

    for row in rows:

        osm_id = clean_text(row.get("osm_id", ""))

        if osm_id:
            # Keep the old plain-ID format compatible.
            new_values.add(osm_id)

        new_values.update(business_signatures(row))

    combined = existing | new_values

    with open(
        HISTORY_PATH,
        "w",
        encoding="utf-8"
    ) as file:

        for value in sorted(combined):
            file.write(value + "\n")

    print("Duplicate history entries:", len(combined))


# --------------------------------------------------
# HTML PARSER
# --------------------------------------------------

class PageParser(HTMLParser):

    def __init__(self):
        super().__init__()

        self.links = []
        self.emails = []
        self.phones = []

    def handle_starttag(self, tag, attrs):

        attributes = dict(attrs)

        href = clean_text(attributes.get("href", ""))

        if not href:
            return

        if href.lower().startswith("mailto:"):
            email = normalize_email(
                href.split(":", 1)[1].split("?", 1)[0]
            )

            if email:
                self.emails.append(email)

        elif href.lower().startswith("tel:"):
            phone = normalize_phone(
                href.split(":", 1)[1]
            )

            if phone:
                self.phones.append(phone)

        elif tag.lower() == "a":
            self.links.append(href)


# --------------------------------------------------
# WEBSITE REQUESTS AND ROBOTS POLICY
# --------------------------------------------------

def make_request(url, timeout=REQUEST_TIMEOUT):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml"
        }
    )

    return urllib.request.urlopen(
        request,
        timeout=timeout
    )


def get_robots_policy(website):

    parsed = urlparse(website)

    robots_url = (
        f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    )

    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)

    try:

        with make_request(
            robots_url,
            timeout=ROBOTS_TIMEOUT
        ) as response:

            content = response.read(100_000).decode(
                "utf-8",
                errors="replace"
            )

            parser.parse(content.splitlines())

            return parser, "available"

    except urllib.error.HTTPError as error:

        if error.code == 404:
            parser.parse([])
            return parser, "not_found"

        if error.code in (401, 403):
            return None, "restricted"

        return None, f"http_error_{error.code}"

    except Exception:
        return None, "could_not_check"


def robots_allows(parser, url):

    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return False


def fetch_page(url):

    try:

        with make_request(url) as response:

            content_type = response.headers.get(
                "Content-Type",
                ""
            ).lower()

            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                return "not_html", "", None

            data = response.read(MAX_PAGE_BYTES)

            text = data.decode(
                "utf-8",
                errors="replace"
            )

            return "success", text, response.status

    except urllib.error.HTTPError as error:

        return "http_error", "", error.code

    except (urllib.error.URLError, TimeoutError, socket.timeout):

        return "network_error", "", None

    except Exception:

        return "network_error", "", None


# --------------------------------------------------
# CONTACT EXTRACTION
# --------------------------------------------------

EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE
)

PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+?\d[\d\s()./-]{7,18}\d)(?!\w)"
)


def extract_contacts(html):

    parser = PageParser()

    try:
        parser.feed(html)
    except Exception:
        pass

    emails = set(parser.emails)
    phones = set(parser.phones)

    for match in EMAIL_PATTERN.findall(html):
        email = normalize_email(match)

        if email:
            emails.add(email)

    for match in PHONE_PATTERN.findall(html):

        phone = normalize_phone(match)

        if phone:
            phones.add(phone)

    return emails, phones, parser.links


def choose_email(emails):

    filtered = []

    unwanted = (
        "example.com",
        "domain.com",
        "yourname",
        "wixpress.com"
    )

    for email in emails:

        if any(item in email.lower() for item in unwanted):
            continue

        filtered.append(email)

    return sorted(filtered)[0] if filtered else ""


def choose_phone(phones):

    valid = []

    for phone in phones:

        normalized = normalize_phone(phone)

        if normalized:
            valid.append(normalized)

    return sorted(valid)[0] if valid else ""


def check_email_mx(email):

    if not email or "@" not in email:
        return "Not available"

    domain = email.split("@", 1)[1]

    try:

        resolver = dns.resolver.Resolver()
        resolver.timeout = 1.5
        resolver.lifetime = 2.5

        resolver.resolve(domain, "MX")

        return "MX found"

    except dns.resolver.NXDOMAIN:
        return "No valid email domain"

    except dns.resolver.NoAnswer:
        return "No MX record"

    except Exception:
        return "Could not verify"


# --------------------------------------------------
# WEBSITE ENRICHMENT
# --------------------------------------------------

def build_website_search_url(row):
    """
    Build a human-search link only.
    This does not discover or verify a business website automatically.
    """
    search_parts = [
        clean_text(row.get("name", "")),
        clean_text(row.get("city", "")),
        clean_text(row.get("state", "")),
        "India official website"
    ]

    query = " ".join(part for part in search_parts if part)
    return "https://www.google.com/search?q=" + quote_plus(query)


def contact_page_candidates(homepage, links):

    selected = []

    keywords = (
        "contact",
        "about",
        "support",
        "reach-us",
        "get-in-touch"
    )

    base_domain = website_domain(homepage)

    for link in links:

        try:
            full_url = urljoin(homepage, link)
            parsed = urlparse(full_url)

            if parsed.scheme not in ("http", "https"):
                continue

            if website_domain(full_url) != base_domain:
                continue

            path = parsed.path.lower()

            if any(keyword in path for keyword in keywords):

                if full_url not in selected:
                    selected.append(full_url)

            if len(selected) >= MAX_CONTACT_PAGES:
                break

        except Exception:
            continue

    return selected


def enrich_website(row):

    website = row.get("website", "")

    # A search link helps a person verify the official website.
    # It is deliberately kept separate from the verified website field.
    row["website_search_url"] = build_website_search_url(row)

    if not website:

        row["website_discovery_status"] = "Manual search required - not listed in OSM"
        row["website_status"] = "Not listed in OSM"
        row["website_opportunity"] = "Potential - Website Not Listed"
        row["opportunity_reason"] = (
            "Website URL is not listed in OpenStreetMap. "
            "The business website needs manual verification."
        )

        return row

    row["website_discovery_status"] = (
        "Website URL listed in OSM - official ownership not independently confirmed"
    )

    robots, robots_status = get_robots_policy(website)

    if robots_status == "restricted":

        row["website_status"] = "Access restricted - robots.txt"
        row["website_opportunity"] = "Potential - Access Restricted"
        row["opportunity_reason"] = (
            "Website blocks automated access through robots policy."
        )

        return row

    if robots_status not in ("available", "not_found"):

        row["website_status"] = "Could not verify - robots policy"
        row["website_opportunity"] = "Potential - Needs Verification"
        row["opportunity_reason"] = (
            "Could not safely verify the website access policy."
        )

        return row

    if not robots_allows(robots, website):

        row["website_status"] = "Not checked - robots policy"
        row["website_opportunity"] = "Potential - Needs Verification"
        row["opportunity_reason"] = (
            "Website robots policy does not allow this page to be checked."
        )

        return row

    status, html, http_code = fetch_page(website)

    if status == "http_error":

        if http_code in (401, 403):
            row["website_status"] = f"Access restricted (HTTP {http_code})"
            row["website_opportunity"] = "Potential - Access Restricted"

        elif http_code in (404, 410):
            row["website_status"] = f"Unavailable response (HTTP {http_code})"
            row["website_opportunity"] = "Potential - Website Unavailable"

        elif http_code == 429:
            row["website_status"] = "Rate limited (HTTP 429)"
            row["website_opportunity"] = "Potential - Needs Verification"

        else:
            row["website_status"] = f"HTTP error ({http_code})"
            row["website_opportunity"] = "Potential - Needs Verification"

        row["opportunity_reason"] = (
            "The website returned an HTTP error. "
            "This does not by itself prove the website is permanently broken."
        )

        return row

    if status == "network_error":

        row["website_status"] = "Could not verify"
        row["website_opportunity"] = "Potential - Needs Verification"
        row["opportunity_reason"] = (
            "A network or timeout error prevented website verification."
        )

        return row

    if status == "not_html":

        row["website_status"] = "Accessible - non-HTML response"
        row["website_opportunity"] = "Potential - Needs Verification"
        row["opportunity_reason"] = (
            "The website responded, but its homepage was not an HTML page."
        )

        return row

    row["website_status"] = "Accessible"

    emails, phones, links = extract_contacts(html)

    # Only check a small number of relevant contact pages.
    contact_pages = contact_page_candidates(website, links)

    for page_url in contact_pages:

        if not robots_allows(robots, page_url):
            continue

        page_status, page_html, _ = fetch_page(page_url)

        if page_status != "success":
            continue

        page_emails, page_phones, _ = extract_contacts(page_html)

        emails.update(page_emails)
        phones.update(page_phones)

        if row.get("email") and row.get("phone"):
            break

    if not row.get("email"):

        found_email = choose_email(emails)

        if found_email:
            row["email"] = found_email
            row["email_source"] = "Business website"

    if not row.get("phone"):

        found_phone = choose_phone(phones)

        if found_phone:
            row["phone"] = found_phone
            row["phone_source"] = "Business website"

    if row.get("email"):
        row["email_status"] = check_email_mx(row["email"])
    else:
        row["email_status"] = "Not found"

    if not row.get("phone") and not row.get("email"):

        row["website_opportunity"] = "Review - No Contact Details Found"
        row["opportunity_reason"] = (
            "Website is accessible, but phone and email were not found "
            "on the checked pages."
        )

    elif not row.get("phone") or not row.get("email"):

        row["website_opportunity"] = "Review - Incomplete Online Presence"
        row["opportunity_reason"] = (
            "Website is accessible, but one contact detail is missing."
        )

    else:

        row["website_opportunity"] = "Low - Good Online Presence"
        row["opportunity_reason"] = (
            "Website is accessible and both phone and email were found."
        )

    return row


# --------------------------------------------------
# LEAD SCORING AND PRIORITY
# --------------------------------------------------

def opportunity_rank(row):

    opportunity = row.get("website_opportunity", "")

    if opportunity == "Potential - Website Not Listed":
        return 0

    if opportunity == "Potential - Website Unavailable":
        return 1

    if opportunity == "Potential - Access Restricted":
        return 2

    if opportunity == "Potential - Needs Verification":
        return 3

    if opportunity.startswith("Review -"):
        return 4

    if opportunity == "Low - Good Online Presence":
        return 5

    return 6


def score_business(row):

    rank = opportunity_rank(row)

    base_scores = {
        0: 55,
        1: 48,
        2: 42,
        3: 36,
        4: 28,
        5: 12,
        6: 10
    }

    score = base_scores.get(rank, 10)

    if row.get("phone"):
        score += 10

    if row.get("email"):
        score += 12

    if row.get("address"):
        score += 5

    if row.get("city"):
        score += 3

    if row.get("category") in (
        "shop",
        "amenity",
        "office",
        "healthcare",
        "craft",
        "tourism"
    ):
        score += 5

    return min(score, 100)


def set_contact_priority(row):

    has_phone = bool(row.get("phone"))
    has_email = bool(row.get("email"))

    if has_phone and has_email:
        return "High"

    if has_phone or has_email:
        return "Medium"

    return "Low"


def prepare_lead(row):

    row = dict(row)

    row.setdefault("website_status", "")
    row.setdefault("website_opportunity", "")
    row.setdefault("opportunity_reason", "")
    row.setdefault("email_status", "")
    row.setdefault("website_search_url", "")
    row.setdefault("website_discovery_status", "")

    row = enrich_website(row)

    row["contact_priority"] = set_contact_priority(row)
    row["lead_score"] = score_business(row)

    row["_opportunity_rank"] = opportunity_rank(row)

    return row


# --------------------------------------------------
# DUPLICATE REMOVAL
# --------------------------------------------------

def remove_duplicate_records(rows):

    unique_rows = []
    seen_osm_ids = set()
    seen_signatures = set()

    for row in rows:

        osm_id = clean_text(row.get("osm_id", ""))

        if osm_id and osm_id in seen_osm_ids:
            continue

        signatures = business_signatures(row)

        if signatures & seen_signatures:
            continue

        if osm_id:
            seen_osm_ids.add(osm_id)

        seen_signatures.update(signatures)
        unique_rows.append(row)

    return unique_rows


# --------------------------------------------------
# CSV HELPERS
# --------------------------------------------------

def read_csv_file(path):

    if not path.exists():
        return []

    rows = []

    try:

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            newline=""
        ) as file:

            reader = csv.DictReader(file)

            for old_row in reader:

                row = {}

                for field in FIELDS:
                    row[field] = clean_text(
                        old_row.get(field, "")
                    )

                rows.append(row)

    except Exception as error:

        print(f"Could not read {path.name}: {error}")

    return rows


def write_csv_file(path, rows):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        path,
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
            extrasaction="ignore"
        )

        writer.writeheader()

        for row in rows:

            clean_row = {}

            for field in FIELDS:
                clean_row[field] = clean_text(
                    row.get(field, "")
                )

            writer.writerow(clean_row)


def dashboard_sort_key(row):

    try:
        score = int(row.get("lead_score", 0) or 0)
    except (ValueError, TypeError):
        score = 0

    return (
        opportunity_rank(row),
        -score,
        normalize_text(row.get("name", ""))
    )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    print("=" * 55)
    print("BUSINESSFINDERAI - INDIA LEAD ENGINE")
    print("=" * 55)

    started_at = datetime.now(timezone.utc)

    history = load_history()

    print("Previously saved history entries:", len(history))

    candidates = get_osm_data()

    processed = []
    skipped_history = 0

    print("\nChecking business opportunities...")

    for index, candidate in enumerate(candidates, start=1):

        # First skip businesses whose OSM ID or known contact
        # signatures already exist in previous history.
        if already_seen(candidate, history):
            skipped_history += 1
            continue

        try:

            lead = prepare_lead(candidate)

            # Check again after website enrichment because
            # website/contact details may reveal duplicates.
            if already_seen(lead, history):
                skipped_history += 1
                continue

            processed.append(lead)

        except Exception as error:

            print(
                f"Could not process {candidate.get('name', 'Unknown')}: "
                f"{error}"
            )

        if index % 50 == 0 or index == len(candidates):

            print(
                f"Processed {index}/{len(candidates)} candidates"
            )

    # Remove duplicates within today's processed candidates.
    processed = remove_duplicate_records(processed)

    # Website opportunity first, then lead score.
    processed.sort(key=dashboard_sort_key)

    selected = processed[:LIMIT]

    # Daily CSV contains only today's selected leads.
    write_csv_file(DAILY_PATH, selected)

    # Save OSM IDs and business signatures for future duplicate checks.
    save_history(selected)

    # Preserve the existing dashboard records.
    old_dashboard = read_csv_file(DASHBOARD_PATH)

    combined_dashboard = remove_duplicate_records(
        old_dashboard + selected
    )

    combined_dashboard.sort(key=dashboard_sort_key)

    write_csv_file(
        DASHBOARD_PATH,
        combined_dashboard
    )

    # Summary
    no_website = sum(
        1 for row in selected
        if row.get("website_status") == "Not listed in OSM"
    )

    high_opportunity = sum(
        1 for row in selected
        if opportunity_rank(row) <= 2
    )

    with_phone = sum(
        1 for row in selected
        if row.get("phone")
    )

    with_email = sum(
        1 for row in selected
        if row.get("email")
    )

    duration = (
        datetime.now(timezone.utc) - started_at
    ).total_seconds()

    print("\n" + "=" * 55)
    print("BUSINESSFINDERAI - DAILY REPORT")
    print("=" * 55)

    print("Candidates sampled:", len(candidates))
    print("Skipped from previous history:", skipped_history)
    print("New unique candidates processed:", len(processed))
    print("Today's selected leads:", len(selected))
    print("Website not listed in OSM:", no_website)
    print("Manual website searches available:", sum(1 for row in selected if row.get("website_search_url")))
    print("High website opportunity:", high_opportunity)
    print("Leads with phone:", with_phone)
    print("Leads with email:", with_email)
    print("Dashboard total:", len(combined_dashboard))
    print(f"Total duration: {duration / 60:.2f} minutes")

    print("\nFiles created:")
    print("-", DAILY_CSV)
    print("-", DASHBOARD_CSV)
    print("-", HISTORY_FILE)

    print("\nBusinessFinderAI run completed.")


if __name__ == "__main__":
    main()
