
import csv
import json
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.robotparser

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError

import dns.resolver


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

LIMIT = 30
MAX_CANDIDATES = 150

HISTORY = "seen_businesses.txt"
OUTPUT = "business_leads.csv"
PUBLIC_OUTPUT = "public_leads.csv"

USER_AGENT = "BusinessFinderAI/1.0 (public business contact discovery)"

FIELDS = [
    "osm_id", "name", "category", "subcategory",
    "address", "city", "state", "phone", "email",
    "website", "phone_source", "email_source",
    "email_status", "contact_priority", "lead_score",
    "latitude", "longitude", "osm_url"
]

AMENITIES = (
    "restaurant|cafe|fast_food|bank|pharmacy|clinic|"
    "dentist|veterinary|car_repair|car_wash|fuel|"
    "beauty_salon|marketplace|coworking_space"
)

MX_CACHE = {}


# --------------------------------------------------
# COMMON FUNCTIONS
# --------------------------------------------------

def get_tag(tags, *keys):

    for key in keys:

        value = tags.get(key, "").strip()

        if value:
            return value

    return ""


# --------------------------------------------------
# 1. GET AHMEDABAD OSM DATA
# --------------------------------------------------

def get_osm_data():

    query = f"""
    [out:json][timeout:180];

    rel
      ["boundary"="administrative"]
      ["name"~"^Ahmedabad$",i]
      ["admin_level"="8"];

    map_to_area->.ahmedabad;

    (
      nwr(area.ahmedabad)["shop"];
      nwr(area.ahmedabad)["office"];
      nwr(area.ahmedabad)["craft"];
      nwr(area.ahmedabad)["healthcare"];
      nwr(area.ahmedabad)["amenity"~"^({AMENITIES})$"];
    );

    out center tags;
    """

    servers = [
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass-api.de/api/interpreter"
    ]

    for server in servers:

        try:

            print("Trying OSM server:", server)

            body = urllib.parse.urlencode(
                {"data": query}
            ).encode("utf-8")

            request = urllib.request.Request(
                server,
                data=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded"
                }
            )

            with urllib.request.urlopen(
                request, timeout=210
            ) as response:

                data = json.loads(
                    response.read().decode("utf-8")
                )

                if not data.get("elements"):

                    raise RuntimeError(
                        "No businesses found. "
                        "Ahmedabad boundary may not match this query."
                    )

                print(
                    "OSM records received:",
                    len(data["elements"])
                )

                return data

        except Exception as error:

            print("OSM server error:", error)
            print("Trying next server...")

            time.sleep(5)

    raise RuntimeError(
        "All Overpass servers failed. "
        "No leads were exported."
    )


# --------------------------------------------------
# 2. EXTRACT BUSINESS DETAILS
# --------------------------------------------------

def extract_osm_businesses(data, seen):

    businesses = []

    for item in data.get("elements", []):

        tags = item.get("tags", {})

        name = get_tag(tags, "name", "name:en")

        if not name:
            continue

        category = ""
        subcategory = ""

        for key in (
            "shop", "office", "craft", "healthcare", "amenity"
        ):

            if tags.get(key):

                category = key
                subcategory = tags[key]
                break

        if not category:
            continue

        if category == "office" and subcategory in (
            "government", "political_party"
        ):
            continue

        osm_id = f'{item["type"]}/{item["id"]}'

        if osm_id in seen:
            continue

        center = item.get("center", {})

        latitude = item.get(
            "lat", center.get("lat", "")
        )

        longitude = item.get(
            "lon", center.get("lon", "")
        )

        address_parts = [
            get_tag(tags, "addr:housenumber"),
            get_tag(tags, "addr:street"),
            get_tag(tags, "addr:suburb")
        ]

        address = ", ".join(
            part for part in address_parts if part
        )

        phone = get_tag(
            tags, "phone", "contact:phone", "mobile"
        )

        email = get_tag(
            tags, "email", "contact:email"
        )

        website = get_tag(
            tags, "website", "contact:website", "url"
        )

        businesses.append({

            "osm_id": osm_id,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "address": address,
            "city": "Ahmedabad",
            "state": get_tag(tags, "addr:state") or "Gujarat",
            "phone": phone,
            "email": email,
            "website": website,

            "phone_source": "OpenStreetMap" if phone else "",
            "email_source": "OpenStreetMap" if email else "",

            "email_status": "Not checked",

            "latitude": latitude,
            "longitude": longitude,

            "osm_url": (
                f'https://www.openstreetmap.org/'
                f'{item["type"]}/{item["id"]}'
            )
        })

    return businesses


# --------------------------------------------------
# 3. VALIDATE INDIAN PHONE NUMBERS
# --------------------------------------------------

def normalize_phone(phone):

    if not phone:
        return ""

    phone = str(phone).strip()

    if phone.startswith("+") and not phone.startswith("+91"):
        return ""

    digits = re.sub(r"\D", "", phone)

    if digits.startswith("0091"):
        digits = digits[4:]

    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]

    if digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]

    # Indian mobile number
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    # Indian landline number
    if len(digits) == 10 and digits[0] in "2345":
        return "+91" + digits

    return ""


def clean_phone(phone):

    if not phone:
        return ""

    parts = re.split(r"[;,/|]", str(phone))

    for part in parts:

        valid = normalize_phone(part)

        if valid:
            return valid

    return ""


# --------------------------------------------------
# 4. WEBSITE CONTACT PARSER
# --------------------------------------------------

class ContactParser(HTMLParser):

    def __init__(self):

        super().__init__()

        self.phones = set()
        self.emails = set()

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        href = attrs.get("href", "").strip()

        if href.lower().startswith("mailto:"):

            email = href[7:].split("?")[0].strip()

            if email:
                self.emails.add(email)

        if href.lower().startswith("tel:"):

            phone = href[4:].strip()

            if phone:
                self.phones.add(phone)

    def handle_data(self, data):

        emails = re.findall(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            data
        )

        for email in emails:
            self.emails.add(email)

        phones = re.findall(
            r"(?:\+?91[\s().-]*)?(?:0[\s().-]*)?"
            r"(?:[6-9](?:[\s().-]*\d){9}|"
            r"[2-5](?:[\s().-]*\d){9})",
            data
        )

        for phone in phones:
            self.phones.add(phone)


# --------------------------------------------------
# 5. NORMALIZE WEBSITE
# --------------------------------------------------

def normalize_website(url):

    if not url:
        return ""

    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if not parsed.hostname:
        return ""

    if parsed.scheme not in ("http", "https"):
        return ""

    return url


# --------------------------------------------------
# 6. CHECK ROBOTS.TXT
# --------------------------------------------------

def get_robots_parser(base_url):

    parsed = urlparse(base_url)

    robots_url = (
        f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    )

    parser = urllib.robotparser.RobotFileParser()

    parser.set_url(robots_url)

    request = urllib.request.Request(
        robots_url,
        headers={"User-Agent": USER_AGENT}
    )

    try:

        with urllib.request.urlopen(
            request, timeout=10
        ) as response:

            lines = response.read().decode(
                "utf-8", errors="ignore"
            ).splitlines()

            parser.parse(lines)

            return parser

    except HTTPError as error:

        if error.code == 404:

            parser.parse([])

            return parser

        return None

    except Exception:

        return None


# --------------------------------------------------
# 7. FETCH WEBSITE PAGE
# --------------------------------------------------

def fetch_page(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html"
        }
    )

    with urllib.request.urlopen(
        request, timeout=12
    ) as response:

        content_type = response.headers.get(
            "Content-Type", ""
        )

        if "text/html" not in content_type.lower():
            return ""

        return response.read(500000).decode(
            "utf-8", errors="ignore"
        )


# --------------------------------------------------
# 8. VALIDATE EMAIL
# --------------------------------------------------

def valid_email_domain(email):

    email = email.strip().lower()

    if not re.fullmatch(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        email
    ):
        return False

    domain = email.rsplit("@", 1)[1]

    if domain in MX_CACHE:
        return MX_CACHE[domain]

    try:

        dns.resolver.resolve(domain, "MX")

        MX_CACHE[domain] = True

        return True

    except Exception:

        MX_CACHE[domain] = False

        return False


# --------------------------------------------------
# 9. ENRICH BUSINESS CONTACT DETAILS
# --------------------------------------------------

def enrich_business(business):

    business["phone"] = clean_phone(
        business["phone"]
    )

    if business["email"]:

        business["email"] = business["email"].strip().lower()

        if not valid_email_domain(business["email"]):

            business["email"] = ""
            business["email_source"] = ""

    website = normalize_website(
        business["website"]
    )

    if not website:

        business["website"] = ""

        business["email_status"] = (
            "MX record found"
            if business["email"]
            else "No website found"
        )

        return business

    business["website"] = website

    robots = get_robots_parser(website)

    if robots is None:

        business["email_status"] = (
            "MX record found"
            if business["email"]
            else "Robots check unavailable"
        )

        return business

    parsed = urlparse(website)

    base = f"{parsed.scheme}://{parsed.netloc}"

    pages = [
        website,
        urljoin(base, "/contact"),
        urljoin(base, "/contact-us"),
        urljoin(base, "/about"),
        urljoin(base, "/about-us")
    ]

    for page_url in pages:

        try:

            if not robots.can_fetch(USER_AGENT, page_url):
                continue

            html = fetch_page(page_url)

            parser = ContactParser()

            parser.feed(html)

            if not business["phone"]:

                for phone in sorted(parser.phones):

                    valid = clean_phone(phone)

                    if valid:

                        business["phone"] = valid
                        business["phone_source"] = page_url

                        break

            if not business["email"]:

                for email in sorted(parser.emails):

                    if valid_email_domain(email):

                        business["email"] = email.lower()
                        business["email_source"] = page_url

                        break

            if business["phone"] and business["email"]:
                break

            time.sleep(0.5)

        except Exception as error:

            print("Page skipped:", page_url, error)

    if business["email"]:
        business["email_status"] = "MX record found"
    else:
        business["email_status"] = "Not found"

    return business


# --------------------------------------------------
# 10. SCORE BUSINESS
# --------------------------------------------------

def score_business(business):

    score = 0

    if business["phone"]:
        score += 3

    if business["email"]:
        score += 3

    if business["website"]:
        score += 1

    if business["address"]:
        score += 1

    if business["phone"] and business["email"]:

        priority = "Phone + Email"

        score += 3

    elif business["phone"] or business["email"]:

        priority = "One contact"

    else:

        priority = "No contact"

    business["contact_priority"] = priority

    business["lead_score"] = score

    return business


# --------------------------------------------------
# 11. DUPLICATE SIGNATURE
# --------------------------------------------------

def business_signatures(item):

    signatures = set()

    name = re.sub(
        r"[^a-z0-9]",
        "",
        item.get("name", "").lower()
    )

    phone = normalize_phone(
        item.get("phone", "")
    )

    website = normalize_website(
        item.get("website", "")
    )

    domain = urlparse(website).hostname or ""

    domain = domain.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    address = re.sub(
        r"[^a-z0-9]",
        "",
        item.get("address", "").lower()
    )

    if name and phone:
        signatures.add(("phone", name, phone))

    if name and domain:
        signatures.add(("website", name, domain))

    if name and address:
        signatures.add(("address", name, address))

    return signatures


def remove_duplicate_records(records):

    unique_records = []

    seen_signatures = set()

    seen_ids = set()

    for item in records:

        osm_id = item.get("osm_id", "")

        if osm_id and osm_id in seen_ids:
            continue

        signatures = business_signatures(item)

        if signatures and signatures.intersection(seen_signatures):
            continue

        if osm_id:
            seen_ids.add(osm_id)

        seen_signatures.update(signatures)

        unique_records.append(item)

    return unique_records


# --------------------------------------------------
# 12. READ OLD CSV
# --------------------------------------------------

def read_csv_file(filename):

    records = {}

    if not os.path.exists(filename):
        return records

    try:

        with open(
            filename,
            newline="",
            encoding="utf-8-sig"
        ) as file:

            reader = csv.DictReader(file)

            for row in reader:

                osm_id = row.get(
                    "osm_id", ""
                ).strip()

                if osm_id:
                    records[osm_id] = row

    except Exception as error:

        print("CSV read error:", error)

    return records


# --------------------------------------------------
# 13. WRITE CSV
# --------------------------------------------------

def write_csv_file(filename, records):

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
            extrasaction="ignore"
        )

        writer.writeheader()

        for record in records:

            writer.writerow({
                field: record.get(field, "")
                for field in FIELDS
            })


# --------------------------------------------------
# 14. MAIN PROGRAM
# --------------------------------------------------

def main():

    if os.path.exists(HISTORY):

        with open(
            HISTORY,
            encoding="utf-8"
        ) as file:

            seen = set(
                line.strip()
                for line in file
                if line.strip()
            )

    else:

        seen = set()

    print("Downloading Ahmedabad business records...")

    data = get_osm_data()

    businesses = extract_osm_businesses(
        data, seen
    )

    print(
        "New businesses found:",
        len(businesses)
    )

    # Existing contact information first.
    businesses.sort(
        key=lambda item: (
            0 if item["phone"] and item["email"]
            else 1 if item["phone"] or item["email"]
            else 2,
            item["name"].lower()
        )
    )

    candidates = businesses[:MAX_CANDIDATES]

    enriched = []

    for index, business in enumerate(
        candidates, start=1
    ):

        print(
            f'Checking {index}/{len(candidates)}: '
            f'{business["name"]}'
        )

        business = enrich_business(business)

        business = score_business(business)

        enriched.append(business)

        time.sleep(0.5)

    # Remove duplicate businesses.
    enriched = remove_duplicate_records(enriched)

    # Contact priority sorting.
    enriched.sort(
        key=lambda item: (
            0 if item["contact_priority"] == "Phone + Email"
            else 1 if item["contact_priority"] == "One contact"
            else 2,
            -int(item["lead_score"]),
            item["name"].lower()
        )
    )

    selected = enriched[:LIMIT]

    # Today's leads.
    write_csv_file(
        OUTPUT,
        selected
    )

    # Save exported OSM IDs.
    seen.update(
        item["osm_id"]
        for item in selected
    )

    with open(
        HISTORY,
        "w",
        encoding="utf-8"
    ) as file:

        for item in sorted(seen):
            file.write(item + "\n")

    # Preserve old dashboard leads.
    old_records = read_csv_file(
        PUBLIC_OUTPUT
    )

    all_records = (
        list(old_records.values()) + selected
    )

    all_records = remove_duplicate_records(
        all_records
    )

    all_records.sort(
        key=lambda item: (
            0 if item.get("contact_priority") == "Phone + Email"
            else 1 if item.get("contact_priority") == "One contact"
            else 2,
            item.get("name", "").lower()
        )
    )

    write_csv_file(
        PUBLIC_OUTPUT,
        all_records
    )

    # Report.
    both = sum(
        1 for item in selected
        if item["contact_priority"] == "Phone + Email"
    )

    one = sum(
        1 for item in selected
        if item["contact_priority"] == "One contact"
    )

    none = sum(
        1 for item in selected
        if item["contact_priority"] == "No contact"
    )

    print("--------------------------------")
    print("Ahmedabad Lead Report")
    print("Total exported:", len(selected))
    print("Phone + Email:", both)
    print("One contact:", one)
    print("No contact:", none)
    print("Total dashboard leads:", len(all_records))
    print("--------------------------------")


if __name__ == "__main__":
    main()
