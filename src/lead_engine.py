
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


def get_tag(tags, *keys):
    for key in keys:
        value = tags.get(key, "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------
# 1. GET BUSINESSES INSIDE AHMEDABAD MUNICIPAL LIMIT
# --------------------------------------------------

def get_osm_data():

    query = f"""
    [out:json][timeout:180];

    rel
      ["boundary"="local_authority"]
      ["local_authority:IN"="municipal_corporation"]
      ["admin_level"="8"]
      ["name"~"Ahmedabad",i];

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
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter"
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

            with urllib.request.urlopen(request, timeout=210) as response:
                data = json.loads(
                    response.read().decode("utf-8")
                )

                if not data.get("elements"):
                    raise RuntimeError(
                        "No businesses found inside Ahmedabad boundary."
                    )

                return data

        except Exception as error:
            print("OSM server error:", error)
            time.sleep(5)

    raise RuntimeError(
        "Ahmedabad boundary query failed on all OSM servers. "
        "No leads were exported."
    )


# --------------------------------------------------
# 2. EXTRACT BUSINESS INFORMATION
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

        for key in ("shop", "office", "craft", "healthcare", "amenity"):
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

        latitude = item.get("lat", center.get("lat", ""))
        longitude = item.get("lon", center.get("lon", ""))

        address_parts = [
            get_tag(tags, "addr:housenumber"),
            get_tag(tags, "addr:street"),
            get_tag(tags, "addr:suburb")
        ]

        address = ", ".join(
            part for part in address_parts if part
        )

        businesses.append({
            "osm_id": osm_id,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "address": address,
            "city": "Ahmedabad",
            "state": get_tag(tags, "addr:state") or "Gujarat",
            "phone": get_tag(
                tags, "phone", "contact:phone", "mobile"
            ),
            "email": get_tag(
                tags, "email", "contact:email"
            ),
            "website": get_tag(
                tags, "website", "contact:website", "url"
            ),
            "phone_source": "OpenStreetMap",
            "email_source": "OpenStreetMap",
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

    # Reject international numbers other than +91.
    if phone.startswith("+") and not phone.startswith("+91"):
        return ""

    digits = re.sub(r"\D", "", phone)

    if digits.startswith("0091"):
        digits = digits[4:]

    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]

    # Remove a leading zero.
    if digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]

    # Indian mobile number.
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    # Indian landline number.
    if len(digits) in (9, 10) and digits[0] in "12345":
        return "+91" + digits

    return ""


def clean_phone(phone):

    if not phone:
        return ""

    # Some OSM records contain multiple numbers.
    parts = re.split(r"[;,/|]", str(phone))

    for part in parts:
        valid = normalize_phone(part)

        if valid:
            return valid

    return ""


# --------------------------------------------------
# 4. FIND CONTACT DETAILS ON WEBSITES
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

        email_list = re.findall(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            data
        )

        for email in email_list:
            self.emails.add(email)

        phone_list = re.findall(
            r"(?:\+?91[\s().-]*)?(?:0[\s().-]*)?"
            r"(?:[6-9](?:[\s().-]*\d){9}|"
            r"[1-5](?:[\s().-]*\d){8,9})",
            data
        )

        for phone in phone_list:
            self.phones.add(phone)


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


def get_robots_parser(base_url):

    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)

    request = urllib.request.Request(
        robots_url,
        headers={"User-Agent": USER_AGENT}
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:

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


def fetch_page(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html"
        }
    )

    with urllib.request.urlopen(request, timeout=12) as response:

        content_type = response.headers.get("Content-Type", "")

        if "text/html" not in content_type.lower():
            return ""

        return response.read(500000).decode(
            "utf-8", errors="ignore"
        )


# --------------------------------------------------
# 5. CHECK EMAIL DOMAIN
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
# 6. ENRICH BUSINESS CONTACT DETAILS
# --------------------------------------------------

def enrich_business(business):

    business["phone"] = clean_phone(business["phone"])

    if business["email"]:
        business["email"] = business["email"].strip().lower()

        if not valid_email_domain(business["email"]):
            business["email"] = ""
            business["email_source"] = ""

    website = normalize_website(business["website"])

    if not website:
        business["website"] = ""
        business["email_status"] = (
            "No website found" if not business["email"]
            else "MX record found"
        )
        return business

    business["website"] = website

    robots = get_robots_parser(website)

    if robots is None:
        business["email_status"] = (
            "Robots check unavailable"
            if not business["email"]
            else "MX record found"
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
                    valid_phone = clean_phone(phone)

                    if valid_phone:
                        business["phone"] = valid_phone
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
# 7. SCORE AND PRIORITIZE LEADS
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
# 8. DUPLICATE CHECKING
# --------------------------------------------------

def business_signature(item):

    name = re.sub(
        r"[^a-z0-9]",
        "",
        item.get("name", "").lower()
    )

    phone = normalize_phone(item.get("phone", ""))

    website = normalize_website(item.get("website", ""))

    domain = urlparse(website).hostname or ""
    domain = domain.lower().replace("www.", "")

    address = re.sub(
        r"[^a-z0-9]",
        "",
        item.get("address", "").lower()
    )

    if name and phone:
        return "phone", name, phone

    if name and domain:
        return "website", name, domain

    if name and address:
        return "address", name, address

    return ""


def read_csv_file(filename):

    records = {}

    if not os.path.exists(filename):
        return records

    try:
        with open(
            filename, newline="", encoding="utf-8-sig"
        ) as file:

            reader = csv.DictReader(file)

            for row in reader:

                osm_id = row.get("osm_id", "").strip()

                if osm_id:
                    records[osm_id] = row

    except Exception as error:
        print("CSV read error:", error)

    return records


def remove_duplicate_records(records):

    unique = {}
    signatures = set()

    for item in records:

        signature = business_signature(item)

        if signature and signature in signatures:
            continue

        if signature:
            signatures.add(signature)

        unique.append(item)

    return unique


# --------------------------------------------------
# 9. WRITE CSV
# --------------------------------------------------

def write_csv_file(filename, records):

    with open(
        filename, "w", newline="", encoding="utf-8-sig"
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
# 10. MAIN PROGRAM
# --------------------------------------------------

def main():

    if os.path.exists(HISTORY):

        with open(HISTORY, encoding="utf-8") as file:

            seen = set(
                line.strip()
                for line in file
                if line.strip()
            )

    else:
        seen = set()

    print("Downloading Ahmedabad business records...")

    data = get_osm_data()

    businesses = extract_osm_businesses(data, seen)

    print("New businesses found:", len(businesses))

    # Check businesses with existing contact details first.
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

    for index, business in enumerate(candidates, start=1):

        print(
            f'Checking {index}/{len(candidates)}: '
            f'{business["name"]}'
        )

        business = enrich_business(business)
        business = score_business(business)

        enriched.append(business)

        # Be polite to public websites.
        time.sleep(0.5)

    # Remove duplicate businesses from today's results.
    enriched = remove_duplicate_records(enriched)

    # Both contacts first, then one contact, then no contacts.
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

    # Save today's new leads.
    write_csv_file(OUTPUT, selected)

    # Only exported leads enter OSM ID history.
    seen.update(item["osm_id"] for item in selected)

    with open(HISTORY, "w", encoding="utf-8") as file:

        for item in sorted(seen):
            file.write(item + "\n")

    # Keep old dashboard leads and add today's leads.
    old_records = read_csv_file(PUBLIC_OUTPUT)

    all_records = list(old_records.values()) + selected

    # Remove duplicate businesses from dashboard records.
    all_records = remove_duplicate_records(all_records)

    all_records.sort(
        key=lambda item: (
            0 if item.get("contact_priority") == "Phone + Email"
            else 1 if item.get("contact_priority") == "One contact"
            else 2,
            item.get("name", "").lower()
        )
    )

    write_csv_file(PUBLIC_OUTPUT, all_records)

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
