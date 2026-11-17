
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
MAX_CANDIDATES = 100

SOUTH, WEST, NORTH, EAST = 22.90, 72.40, 23.20, 72.80

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


def get_tag(tags, *keys):
    for key in keys:
        value = tags.get(key, "").strip()
        if value:
            return value
    return ""


def get_osm_data():
    bbox = f"{SOUTH},{WEST},{NORTH},{EAST}"

    query = f"""
    [out:json][timeout:120];
    (
      nwr["shop"]({bbox});
      nwr["office"]({bbox});
      nwr["craft"]({bbox});
      nwr["healthcare"]({bbox});
      nwr["amenity"~"^({AMENITIES})$"]({bbox});
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

            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except Exception as error:
            print("OSM server error:", error)
            time.sleep(5)

    raise RuntimeError("All free OSM servers failed.")


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

        website = get_tag(
            tags, "website", "contact:website", "url"
        )

        phone = get_tag(
            tags, "phone", "contact:phone", "mobile"
        )

        email = get_tag(
            tags, "email", "contact:email"
        )

        businesses.append({
            "osm_id": osm_id,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "address": address,
            "city": get_tag(
                tags, "addr:city", "addr:town"
            ) or "Ahmedabad",
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
            r"(?:\+91[\s-]?)?[6-9]\d{9}",
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
        # A missing robots.txt (404) means no published rules.
        if error.code == 404:
            parser.parse([])
            return parser

        return None

    except Exception:
        # If robots.txt cannot be checked, skip the website.
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


def valid_email_domain(email):
    if not re.fullmatch(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        email
    ):
        return False

    domain = email.rsplit("@", 1)[1]

    try:
        dns.resolver.resolve(domain, "MX")
        return True
    except Exception:
        return False


def enrich_business(business):
    website = normalize_website(business["website"])

    if not website:
        business["email_status"] = "No website found"
        return business

    business["website"] = website

    robots = get_robots_parser(website)

    if robots is None:
        business["email_status"] = "Robots check unavailable"
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

            if parser.phones and not business["phone"]:
                business["phone"] = sorted(parser.phones)[0]
                business["phone_source"] = page_url

            if parser.emails and not business["email"]:
                for email in sorted(parser.emails):
                    if valid_email_domain(email):
                        business["email"] = email
                        business["email_source"] = page_url
                        break

            if business["phone"] and business["email"]:
                break

            time.sleep(0.5)

        except Exception as error:
            print("Page skipped:", page_url, error)

    if business["email"]:
        if valid_email_domain(business["email"]):
            business["email_status"] = "MX record found"
        else:
            business["email_status"] = "MX record not found"
    else:
        business["email_status"] = "Not found"

    return business


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

    print("Downloading Ahmedabad OSM records...")

    data = get_osm_data()
    businesses = extract_osm_businesses(data, seen)

    print("New businesses found:", len(businesses))

    # Check businesses that already have contact information first.
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

    # Only exported leads are added to duplicate history.
    seen.update(item["osm_id"] for item in selected)

    with open(HISTORY, "w", encoding="utf-8") as file:
        for item in sorted(seen):
            file.write(item + "\n")

    # Keep old dashboard leads and add today's new leads.
    old_records = read_csv_file(PUBLIC_OUTPUT)

    for item in selected:
        old_records[item["osm_id"]] = item

    all_records = list(old_records.values())

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
