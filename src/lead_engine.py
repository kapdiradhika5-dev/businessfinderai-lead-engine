
import csv
import os
import re
import time
import random
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

MAX_NO_WEBSITE = 500
MAX_WEAK_WEBSITE = 300
MAX_INCOMPLETE_WEBSITE = 200
MAX_GOOD_WEBSITE = 100

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
WEBSITE_CACHE = {}


# --------------------------------------------------
# BASIC HELPERS
# --------------------------------------------------

def clean(value):
    return str(value or "").strip()


def load_history():

    if not os.path.exists(HISTORY):
        return set()

    with open(HISTORY, "r", encoding="utf-8") as file:
        return {
            line.strip()
            for line in file
            if line.strip()
        }


def save_history(seen_ids):

    with open(HISTORY, "w", encoding="utf-8") as file:
        for item in sorted(seen_ids):
            file.write(item + "\n")


def normalize_phone(value):

    digits = re.sub(r"\D", "", clean(value))

    if digits.startswith("0091"):
        digits = digits[4:]

    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]

    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    return ""


def normalize_email(value):

    email = clean(value).lower()

    if not re.fullmatch(
        r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}",
        email
    ):
        return ""

    return email


def normalize_website(value):

    value = clean(value)

    if not value:
        return ""

    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    parsed = urlparse(value)

    if not parsed.netloc or "." not in parsed.netloc:
        return ""

    if parsed.scheme not in ("http", "https"):
        return ""

    return value.rstrip("/")


def website_domain(value):

    website = normalize_website(value)

    if not website:
        return ""

    domain = urlparse(website).netloc.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def valid_email_domain(email):

    email = normalize_email(email)

    if not email:
        return False

    domain = email.split("@")[-1]

    if domain in MX_CACHE:
        return MX_CACHE[domain]

    try:

        dns.resolver.resolve(
            domain,
            "MX",
            lifetime=4
        )

        MX_CACHE[domain] = True

    except Exception:

        MX_CACHE[domain] = False

    return MX_CACHE[domain]


# --------------------------------------------------
# OSM DATA EXTRACTION
# --------------------------------------------------

def get_category(tags):

    for key in (
        "shop", "office", "craft",
        "healthcare", "amenity", "tourism"
    ):

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


def get_city(tags):

    return clean(
        tags.get("addr:city")
        or tags.get("addr:town")
        or tags.get("addr:village")
        or tags.get("addr:district")
    )


def get_state(tags):

    return clean(
        tags.get("addr:state")
        or tags.get("is_in:state")
    )


def make_row(obj):

    tags = obj.tags

    name = clean(tags.get("name"))

    if not name:
        return None

    category, subcategory = get_category(tags)

    if not category:
        return None

    if not obj.location.valid():
        return None

    phone = normalize_phone(
        tags.get("contact:phone")
        or tags.get("phone")
    )

    email = normalize_email(
        tags.get("contact:email")
        or tags.get("email")
    )

    website = normalize_website(
        tags.get("contact:website")
        or tags.get("website")
    )

    return {
        "osm_id": f"node/{obj.id}",
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "address": get_address(tags),
        "city": get_city(tags),
        "state": get_state(tags),
        "phone": phone,
        "email": email,
        "website": website,
        "phone_source": "OpenStreetMap" if phone else "",
        "email_source": "OpenStreetMap" if email else "",
        "email_status": "",
        "contact_priority": "",
        "lead_score": 0,
        "latitude": obj.location.lat,
        "longitude": obj.location.lon,
        "osm_url": (
            f"https://www.openstreetmap.org/node/{obj.id}"
        )
    }


# --------------------------------------------------
# WEBSITE OPPORTUNITY CANDIDATE POOLS
# --------------------------------------------------

class IndiaBusinessHandler(osmium.SimpleHandler):

    def __init__(self, seen):

        super().__init__()

        self.seen = seen

        self.no_website = []
        self.weak_website = []
        self.incomplete_website = []
        self.good_website = []

        self.counts = {
            "no_website": 0,
            "weak_website": 0,
            "incomplete_website": 0,
            "good_website": 0
        }

        self.total_found = 0
        self.total_nodes = 0

        self.randomizer = random.Random(
            date.today().isoformat()
        )

    def add_to_pool(self, row, pool, key, maximum):

        self.counts[key] += 1
        count = self.counts[key]

        if len(pool) < maximum:
            pool.append(row)

        else:

            position = self.randomizer.randint(1, count)

            if position <= maximum:
                pool[position - 1] = row

    def node(self, obj):

        self.total_nodes += 1

        if self.total_nodes % 1000000 == 0:

            print(
                "OSM nodes processed:",
                self.total_nodes,
                "| Businesses found:",
                self.total_found,
                flush=True
            )

        osm_id = f"node/{obj.id}"

        if osm_id in self.seen:
            return

        row = make_row(obj)

        if not row:
            return

        self.total_found += 1

        if not row["website"]:

            self.add_to_pool(
                row,
                self.no_website,
                "no_website",
                MAX_NO_WEBSITE
            )

        elif not row["phone"] and not row["email"]:

            self.add_to_pool(
                row,
                self.weak_website,
                "weak_website",
                MAX_WEAK_WEBSITE
            )

        elif not row["phone"] or not row["email"]:

            self.add_to_pool(
                row,
                self.incomplete_website,
                "incomplete_website",
                MAX_INCOMPLETE_WEBSITE
            )

        else:

            self.add_to_pool(
                row,
                self.good_website,
                "good_website",
                MAX_GOOD_WEBSITE
            )


def get_osm_data(seen):

    if not os.path.exists(PBF_FILE):

        raise FileNotFoundError(
            "India PBF file not found. Check workflow download step."
        )

    print(
        "Reading India-wide OpenStreetMap nodes...",
        flush=True
    )

    handler = IndiaBusinessHandler(seen)

    # Memory-efficient nodes-only extraction.
    handler.apply_file(PBF_FILE)

    print("Total OSM nodes processed:", handler.total_nodes)
    print("Total new businesses found:", handler.total_found)

    print("No website:", handler.counts["no_website"])
    print("Weak website:", handler.counts["weak_website"])
    print("Incomplete website:", handler.counts["incomplete_website"])
    print("Good website:", handler.counts["good_website"])

    candidates = (
        handler.no_website
        + handler.weak_website
        + handler.incomplete_website
        + handler.good_website
    )

    print("Total sampled candidates:", len(candidates))

    return candidates


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

            if href.lower().startswith("mailto:"):

                self.emails.add(
                    href[7:].split("?")[0]
                )

            elif href.lower().startswith("tel:"):

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

        with urllib.request.urlopen(
            request,
            timeout=7
        ) as response:

            content_type = response.headers.get(
                "Content-Type", ""
            )

            if "text/html" not in content_type.lower():
                return ""

            return response.read(
                350000
            ).decode(
                "utf-8",
                errors="ignore"
            )

    except Exception:

        return ""


def can_visit_website(base_url, page_url):

    try:

        robots_url = urljoin(
            base_url + "/",
            "robots.txt"
        )

        request = urllib.request.Request(
            robots_url,
            headers={"User-Agent": USER_AGENT}
        )

        with urllib.request.urlopen(
            request,
            timeout=5
        ) as response:

            robots_text = response.read(
                100000
            ).decode(
                "utf-8",
                errors="ignore"
            )

        parser = urllib.robotparser.RobotFileParser()

        parser.set_url(robots_url)
        parser.parse(robots_text.splitlines())

        return parser.can_fetch(
            USER_AGENT,
            page_url
        )

    except HTTPError as error:

        if error.code == 404:
            return True

        return False

    except Exception:

        return False


def enrich_business(row):

    row["phone"] = normalize_phone(row["phone"])
    row["email"] = normalize_email(row["email"])

    if row["email"]:

        if valid_email_domain(row["email"]):

            row["email_status"] = "MX valid"

        else:

            row["email"] = ""
            row["email_status"] = "Invalid or no MX"

    website = row["website"]

    if not website:
        return row

    homepage = fetch_page(website)

    if not homepage:

        row["_website_status"] = "Not accessible"
        return row

    row["_website_status"] = "Accessible"

    pages_to_check = [website]

    parser = ContactParser()
    parser.feed(homepage)

    for link in parser.links:

        text = link.lower()

        if any(word in text for word in (
            "contact", "about", "support"
        )):

            full_url = urljoin(
                website + "/",
                link
            )

            if (
                urlparse(full_url).netloc
                == urlparse(website).netloc
            ):

                pages_to_check.append(full_url)

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

                found_email = normalize_email(found_email)

                if (
                    found_email
                    and valid_email_domain(found_email)
                ):

                    row["email"] = found_email
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
# WEBSITE OPPORTUNITY SCORING
# --------------------------------------------------

def score_business(row):

    score = 0

    website = row.get("website", "")
    website_status = row.get("_website_status", "")

    if not website:

        score += 10
        opportunity = "High - No Website"

    elif website_status == "Not accessible":

        score += 8
        opportunity = "High - Website Not Accessible"

    elif not row["phone"] or not row["email"]:

        score += 6
        opportunity = "Medium - Incomplete Online Presence"

    else:

        score += 2
        opportunity = "Low - Website Available"

    if row["phone"]:
        score += 2

    if row["email"]:
        score += 2

    if row["address"]:
        score += 1

    if row["phone"] and row["email"]:
        contact_priority = "High - Phone + Email"

    elif row["phone"] or row["email"]:
        contact_priority = "Medium - One Contact"

    else:
        contact_priority = "Low - No Contact"

    row["contact_priority"] = contact_priority
    row["lead_score"] = score

    row["_website_opportunity"] = opportunity

    return row


# --------------------------------------------------
# DUPLICATE CHECKING
# --------------------------------------------------

def business_signatures(row):

    name = clean(row.get("name")).lower()
    phone = clean(row.get("phone"))
    email = clean(row.get("email")).lower()

    domain = website_domain(row.get("website"))

    address = re.sub(
        r"\s+",
        " ",
        clean(row.get("address")).lower()
    )

    signatures = []

    if name and phone:
        signatures.append(
            "phone:" + name + "|" + phone
        )

    if name and email:
        signatures.append(
            "email:" + name + "|" + email
        )

    if name and domain:
        signatures.append(
            "website:" + name + "|" + domain
        )

    if name and address:
        signatures.append(
            "address:" + name + "|" + address
        )

    return signatures


def remove_duplicate_records(rows):

    unique = []
    seen = set()

    for row in rows:

        osm_id = clean(row.get("osm_id"))

        keys = []

        if osm_id:
            keys.append("osm:" + osm_id)

        keys.extend(
            business_signatures(row)
        )

        if any(key in seen for key in keys):
            continue

        seen.update(keys)
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

        reader = csv.DictReader(file)

        return [
            {
                field: clean(row.get(field))
                for field in FIELDS
            }
            for row in reader
        ]


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

            clean_row = {
                field: row.get(field, "")
                for field in FIELDS
            }

            clean_row["phone"] = str(
                clean_row["phone"] or ""
            )

            writer.writerow(clean_row)


# --------------------------------------------------
# MAIN AGENT
# --------------------------------------------------

def main():

    print("=" * 55)
    print("BUSINESSFINDERAI - WEBSITE OPPORTUNITY AGENT")
    print("=" * 55)

    seen_ids = load_history()

    candidates = get_osm_data(seen_ids)

    processed = []

    print(
        "Checking website and contact details...",
        flush=True
    )

    for index, row in enumerate(
        candidates,
        start=1
    ):

        print(
            f"Checking {index}/{len(candidates)}: {row['name']}",
            flush=True
        )

        try:

            row = enrich_business(row)
            row = score_business(row)

            processed.append(row)

        except Exception as error:

            print(
                "Business skipped:",
                error,
                flush=True
            )

        time.sleep(0.2)

    # Website opportunity first.
    def opportunity_priority(row):

        status = row.get("_website_opportunity", "")

        if status == "High - No Website":
            return 4

        if status == "High - Website Not Accessible":
            return 3

        if status == "Medium - Incomplete Online Presence":
            return 2

        return 1

    processed.sort(
        key=lambda row: (
            opportunity_priority(row),
            row["lead_score"],
            bool(row["phone"] and row["email"])
        ),
        reverse=True
    )

    processed = remove_duplicate_records(
        processed
    )

    today_leads = processed[:LIMIT]

    write_csv_file(
        OUTPUT,
        today_leads
    )

    # Save only selected IDs.
    for row in today_leads:
        seen_ids.add(row["osm_id"])

    save_history(seen_ids)

    # Preserve previous dashboard data.
    old_public_leads = read_csv_file(
        PUBLIC_OUTPUT
    )

    combined = old_public_leads + today_leads

    combined = remove_duplicate_records(
        combined
    )

    combined.sort(
        key=lambda row: int(row.get("lead_score") or 0),
        reverse=True
    )

    write_csv_file(
        PUBLIC_OUTPUT,
        combined
    )

    no_website = sum(
        1 for row in today_leads
        if not row["website"]
    )

    high_opportunity = sum(
        1 for row in today_leads
        if row.get("_website_opportunity", "")
        in (
            "High - No Website",
            "High - Website Not Accessible"
        )
    )

    print("=" * 55)
    print("INDIA BUSINESSFINDERAI REPORT")
    print("New leads:", len(today_leads))
    print("No website:", no_website)
    print("High website opportunity:", high_opportunity)
    print(
        "High contact priority:",
        sum(
            1 for row in today_leads
            if row["contact_priority"] == "High - Phone + Email"
        )
    )
    print("Total dashboard leads:", len(combined))
    print("CSV saved:", OUTPUT)
    print("Dashboard data saved:", PUBLIC_OUTPUT)
    print("=" * 55)


if __name__ == "__main__":
    main()
