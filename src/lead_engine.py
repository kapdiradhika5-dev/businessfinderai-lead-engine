
import csv
import os
import re
import random
import time
import urllib.request
import urllib.robotparser
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError

import dns.resolver
import osmium


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

LIMIT = 30
MAX_CANDIDATES = 150
PBF_FILE = "india-latest.osm.pbf"

HISTORY = "seen_businesses.txt"
OUTPUT = "business_leads.csv"
PUBLIC_OUTPUT = "public_leads.csv"

USER_AGENT = "BusinessFinderAI/1.0 (business contact discovery)"

FIELDS = [
    "osm_id", "name", "category", "subcategory",
    "address", "city", "state", "phone", "email",
    "website", "phone_source", "email_source",
    "email_status", "contact_priority", "lead_score",
    "latitude", "longitude", "osm_url"
]

AMENITY_VALUES = {
    "restaurant", "cafe", "fast_food", "bank",
    "pharmacy", "clinic", "doctors", "dentist",
    "veterinary", "car_repair", "car_wash", "fuel",
    "beauty_salon", "marketplace", "coworking_space",
    "hospital", "hotel", "school", "college",
    "gym", "bar", "pub", "laundry",
    "travel_agency", "car_rental"
}

MX_CACHE = {}


# --------------------------------------------------
# BASIC HELPERS
# --------------------------------------------------

def clean(value):
    return str(value or "").strip()


def load_history():
    if not os.path.exists(HISTORY):
        return set()

    with open(HISTORY, "r", encoding="utf-8") as file:
        return {line.strip() for line in file if line.strip()}


def save_history(seen_ids):
    with open(HISTORY, "w", encoding="utf-8") as file:
        for item in sorted(seen_ids):
            file.write(item + "\n")


def normalize_phone(value):
    digits = re.sub(r"\D", "", clean(value))

    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    return ""


def normalize_website(value):
    value = clean(value)

    if not value:
        return ""

    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    parsed = urlparse(value)

    if not parsed.netloc or "." not in parsed.netloc:
        return ""

    return value.rstrip("/")


def valid_email_domain(email):
    email = clean(email).lower()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return False

    domain = email.split("@")[-1]

    if domain in MX_CACHE:
        return MX_CACHE[domain]

    try:
        dns.resolver.resolve(domain, "MX", lifetime=4)
        MX_CACHE[domain] = True
    except Exception:
        MX_CACHE[domain] = False

    return MX_CACHE[domain]


# --------------------------------------------------
# OSM DATA EXTRACTION - NODES ONLY
# --------------------------------------------------

def get_category(tags):
    for key in ("shop", "office", "craft", "healthcare", "amenity", "tourism"):
        value = clean(tags.get(key))

        if not value or value in ("yes", "no"):
            continue

        if key == "amenity" and value not in AMENITY_VALUES:
            continue

        return key, value

    return "", ""


def get_address(tags):
    full = clean(tags.get("addr:full"))

    if full:
        return full

    parts = []

    for key in (
        "addr:housenumber",
        "addr:street",
        "addr:suburb",
        "addr:neighbourhood",
        "addr:postcode"
    ):
        value = clean(tags.get(key))

        if value:
            parts.append(value)

    return ", ".join(parts)


class IndiaBusinessHandler(osmium.SimpleHandler):

    def __init__(self, seen):
        super().__init__()

        self.seen = seen
        self.candidates = []
        self.total_found = 0
        self.total_nodes = 0

        self.randomizer = random.Random(date.today().isoformat())

    def node(self, obj):

        self.total_nodes += 1

        # Progress message after every 1 million nodes
        if self.total_nodes % 1000000 == 0:
            print(
                "OSM nodes processed:",
                self.total_nodes,
                "| Businesses found:",
                self.total_found,
                flush=True
            )

        tags = obj.tags

        name = clean(tags.get("name"))

        if not name:
            return

        category, subcategory = get_category(tags)

        if not category:
            return

        osm_id = f"node/{obj.id}"

        if osm_id in self.seen:
            return

        if not obj.location.valid():
            return

        lat = obj.location.lat
        lon = obj.location.lon

        self.total_found += 1

        phone = normalize_phone(
            tags.get("contact:phone") or tags.get("phone")
        )

        email = clean(
            tags.get("contact:email") or tags.get("email")
        ).lower()

        website = normalize_website(
            tags.get("contact:website") or tags.get("website")
        )

        row = {
            "osm_id": osm_id,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "address": get_address(tags),
            "city": clean(
                tags.get("addr:city") or tags.get("addr:district")
            ),
            "state": clean(tags.get("addr:state")),
            "phone": phone,
            "email": email,
            "website": website,
            "phone_source": "OpenStreetMap" if phone else "",
            "email_source": "OpenStreetMap" if email else "",
            "email_status": "",
            "contact_priority": "",
            "lead_score": 0,
            "latitude": lat,
            "longitude": lon,
            "osm_url": f"https://www.openstreetmap.org/node/{obj.id}"
        }

        # Reservoir sampling
        if len(self.candidates) < MAX_CANDIDATES:
            self.candidates.append(row)

        else:
            position = self.randomizer.randint(1, self.total_found)

            if position <= MAX_CANDIDATES:
                self.candidates[position - 1] = row


def get_osm_data(seen):

    if not os.path.exists(PBF_FILE):
        raise FileNotFoundError(
            "India PBF file not found. Workflow download step check karo."
        )

    print("Reading India-wide OpenStreetMap nodes...", flush=True)

    handler = IndiaBusinessHandler(seen)

    # IMPORTANT:
    # No locations=True
    # No dense_mmap_array
    # No way processing

    handler.apply_file(PBF_FILE)

    print("Total OSM nodes processed:", handler.total_nodes)
    print("Total new businesses found:", handler.total_found)
    print("Sampled businesses:", len(handler.candidates))

    return handler.candidates


# --------------------------------------------------
# WEBSITE CONTACT FINDER
# --------------------------------------------------

class ContactParser(HTMLParser):

    def __init__(self):
        super().__init__()

        self.emails = set()
        self.phones = set()
        self.links = set()

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)
        href = clean(attrs.get("href"))

        if tag == "a" and href:

            if href.startswith("mailto:"):
                self.emails.add(href[7:].split("?")[0])

            elif href.startswith("tel:"):
                self.phones.add(href[4:])

            else:
                self.links.add(href)

    def handle_data(self, data):

        text = clean(data)

        for email in re.findall(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            text
        ):
            self.emails.add(email)

        for phone in re.findall(
            r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}",
            text
        ):
            self.phones.add(phone)


def fetch_page(url):

    try:

        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT}
        )

        with urllib.request.urlopen(request, timeout=7) as response:

            content_type = response.headers.get("Content-Type", "")

            if "text/html" not in content_type.lower():
                return ""

            return response.read(350000).decode(
                "utf-8",
                errors="ignore"
            )

    except Exception:
        return ""


def can_visit_website(base_url, page_url):

    try:

        robots_url = urljoin(base_url + "/", "robots.txt")

        request = urllib.request.Request(
            robots_url,
            headers={"User-Agent": USER_AGENT}
        )

        with urllib.request.urlopen(request, timeout=5) as response:

            robots_text = response.read(100000).decode(
                "utf-8",
                errors="ignore"
            )

        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(robots_text.splitlines())

        return parser.can_fetch(USER_AGENT, page_url)

    except HTTPError as error:

        if error.code == 404:
            return True

        return False

    except Exception:
        return False


def enrich_business(row):

    row["phone"] = normalize_phone(row["phone"])
    row["email"] = clean(row["email"]).lower()

    if row["email"]:

        if valid_email_domain(row["email"]):
            row["email_status"] = "MX valid"

        else:
            row["email"] = ""
            row["email_status"] = "Invalid or no MX"

    website = row["website"]

    if not website:
        return row

    pages_to_check = [website]

    homepage = fetch_page(website)

    if homepage:

        parser = ContactParser()
        parser.feed(homepage)

        for link in parser.links:

            text = link.lower()

            if any(word in text for word in (
                "contact", "about", "support"
            )):

                full_url = urljoin(website + "/", link)

                if urlparse(full_url).netloc == urlparse(website).netloc:
                    pages_to_check.append(full_url)

    # Maximum 2 pages per business
    for page_url in pages_to_check[:2]:

        if not can_visit_website(website, page_url):
            continue

        html = fetch_page(page_url)

        if not html:
            continue

        parser = ContactParser()
        parser.feed(html)

        if not row["email"]:

            for found_email in parser.emails:

                if valid_email_domain(found_email):

                    row["email"] = found_email.lower()
                    row["email_source"] = page_url
                    row["email_status"] = "MX valid"

                    break

        if not row["phone"]:

            for found_phone in parser.phones:

                phone = normalize_phone(found_phone)

                if phone:

                    row["phone"] = phone
                    row["phone_source"] = page_url

                    break

        if row["phone"] and row["email"]:
            break

    return row


# --------------------------------------------------
# LEAD SCORING
# --------------------------------------------------

def score_business(row):

    score = 0

    if row["phone"]:
        score += 3

    if row["email"]:
        score += 3

    if row["website"]:
        score += 1

    if row["address"]:
        score += 1

    if row["phone"] and row["email"]:
        score += 3

    if row["phone"] and row["email"]:
        priority = "High - Phone + Email"

    elif row["phone"] or row["email"]:
        priority = "Medium - One Contact"

    else:
        priority = "Low - No Contact"

    row["contact_priority"] = priority
    row["lead_score"] = score

    return row


# --------------------------------------------------
# DUPLICATE CHECKING
# --------------------------------------------------

def business_signatures(row):

    name = clean(row.get("name")).lower()
    phone = clean(row.get("phone"))
    address = clean(row.get("address")).lower()

    website = clean(row.get("website")).lower()
    domain = urlparse(website).netloc.replace("www.", "")

    signatures = []

    if name and phone:
        signatures.append("phone:" + name + "|" + phone)

    if name and domain:
        signatures.append("website:" + name + "|" + domain)

    if name and address:
        signatures.append("address:" + name + "|" + address)

    return signatures


def remove_duplicate_records(rows):

    unique = []
    seen = set()

    for row in rows:

        signatures = business_signatures(row)

        if not signatures:
            signatures = [clean(row.get("osm_id"))]

        if any(signature in seen for signature in signatures):
            continue

        seen.update(signatures)
        unique.append(row)

    return unique


# --------------------------------------------------
# CSV HELPERS
# --------------------------------------------------

def read_csv_file(filename):

    if not os.path.exists(filename):
        return []

    with open(
        filename,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        return list(csv.DictReader(file))


def write_csv_file(filename, rows):

    with open(
        filename,
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
            writer.writerow(row)


# --------------------------------------------------
# MAIN AGENT
# --------------------------------------------------

def main():

    print("=" * 50)
    print("BUSINESSFINDERAI - INDIA LEAD ENGINE")
    print("=" * 50)

    seen_ids = load_history()

    candidates = get_osm_data(seen_ids)

    # Existing contact details vala business pehla
    candidates.sort(
        key=lambda row: (
            bool(row["phone"] and row["email"]),
            bool(row["phone"] or row["email"]),
            bool(row["website"])
        ),
        reverse=True
    )

    processed = []

    print("Checking business contact details...", flush=True)

    for index, row in enumerate(candidates, start=1):

        print(
            f"Checking {index}/{len(candidates)}: {row['name']}",
            flush=True
        )

        try:

            row = enrich_business(row)
            row = score_business(row)

            processed.append(row)

        except Exception as error:

            print("Business skipped:", error, flush=True)

        time.sleep(0.2)

    processed.sort(
        key=lambda row: (
            row["lead_score"],
            bool(row["phone"] and row["email"])
        ),
        reverse=True
    )

    # Today's maximum 30 new leads
    today_leads = processed[:LIMIT]

    write_csv_file(OUTPUT, today_leads)

    # Save selected IDs for future duplicate checking
    for row in today_leads:
        seen_ids.add(row["osm_id"])

    save_history(seen_ids)

    # Preserve previous dashboard data
    old_public_leads = read_csv_file(PUBLIC_OUTPUT)

    combined = old_public_leads + today_leads
    combined = remove_duplicate_records(combined)

    combined.sort(
        key=lambda row: int(row.get("lead_score") or 0),
        reverse=True
    )

    write_csv_file(PUBLIC_OUTPUT, combined)

    print("=" * 50)
    print("INDIA BUSINESSFINDERAI REPORT")
    print("New leads:", len(today_leads))

    print(
        "High priority:",
        sum(
            1 for row in today_leads
            if row["contact_priority"] == "High - Phone + Email"
        )
    )

    print("Total dashboard leads:", len(combined))
    print("CSV saved:", OUTPUT)
    print("Dashboard data saved:", PUBLIC_OUTPUT)
    print("=" * 50)


if __name__ == "__main__":
    main()
