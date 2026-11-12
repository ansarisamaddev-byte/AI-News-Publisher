
import os
import re
import sys
import json
import html
import random
import requests

from io import BytesIO
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageOps, ImageFilter

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "data" / "content"
PUBLIC_DIR = BASE_DIR / "public"
FONT_DIR = BASE_DIR / "fonts"
IMAGE_ROOT = BASE_DIR / "data" / "images"

WIDTH, HEIGHT = 1080, 1350
MAX_NEWS_PER_RUN = 10
TIMEOUT = 30
MIN_FONT_SIZE = 24
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

CURRENT_POST_USED_LOCAL_IMAGES = set()

def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

load_env()

UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
DEFAULT_PROVIDER = os.getenv("IMAGE_PROVIDER", "unsplash").lower()
BRAND = os.getenv("NEWS_BRAND", "BreakingNews").lstrip("#")
WEBSITE = os.getenv("NEWS_WEBSITE", "www.yourwebsite.com")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "AINewsPublisher/2.0",
    "Accept-Version": "v1"
})

# =========================================================
# CATEGORY THEMES AND FALLBACK IMAGE QUERIES
# =========================================================

THEMES = {
    "health": {"accent": "#F04444", "label": "HEALTH",
               "fallback": "healthcare hospital medical doctor"},
    "technology": {"accent": "#20BCE5", "label": "TECHNOLOGY",
                   "fallback": "modern technology computer digital"},
    "business": {"accent": "#E6B85C", "label": "BUSINESS",
                 "fallback": "business finance corporate office"},
    "india": {"accent": "#F28C28", "label": "INDIA",
              "fallback": "India city landmark skyline"},
    "world": {"accent": "#F04444", "label": "WORLD",
              "fallback": "global world international city"},
    "politics": {"accent": "#E45B65", "label": "POLITICS",
                 "fallback": "parliament government building"},
    "science": {"accent": "#A78BFA", "label": "SCIENCE",
                "fallback": "scientific research laboratory"},
    "sports": {"accent": "#27B77D", "label": "SPORTS",
               "fallback": "sports stadium athletes"},
    "entertainment": {"accent": "#D96BD5", "label": "ENTERTAINMENT",
                      "fallback": "cinema film entertainment"},
    "automobile": {"accent": "#E87555", "label": "AUTOMOTIVE",
                   "fallback": "modern car automotive industry"},
    "education": {"accent": "#E6C45C", "label": "EDUCATION",
                  "fallback": "university education classroom"}
}

DEFAULT_THEME = {
    "accent": "#F04444",
    "label": "NEWS",
    "fallback": "news newsroom journalism"
}

# =========================================================
# FONT MANAGEMENT
# =========================================================

FONT_FILES = []
FONTS = {}

def discover_fonts():
    global FONT_FILES, FONTS

    FONT_FILES = [
        p for p in FONT_DIR.rglob("*")
        if p.suffix.lower() in (".ttf", ".otf")
    ] if FONT_DIR.exists() else []

    FONTS = {}

    for path in FONT_FILES:
        name = re.sub(r"[^a-z]", "", path.stem.lower())

        if "italic" in name or "oblique" in name:
            continue

        if any(k in name for k in ("black", "extrabold", "bold", "heavy", "semibold")):
            FONTS.setdefault("bold", path)
        elif any(k in name for k in ("regular", "normal", "medium", "book", "light")):
            FONTS.setdefault("regular", path)

    if "regular" not in FONTS and FONT_FILES:
        reg_candidates = [p for p in FONT_FILES if "regular" in p.stem.lower()]
        FONTS["regular"] = reg_candidates[0] if reg_candidates else FONT_FILES[0]

    if "bold" not in FONTS:
        bold_candidates = [p for p in FONT_FILES if "bold" in p.stem.lower()]
        FONTS["bold"] = bold_candidates[0] if bold_candidates else FONTS.get("regular")

    print(f"[FONTS] Regular: {FONTS.get('regular')}")
    print(f"[FONTS] Bold: {FONTS.get('bold')}")
    print(f"[FONTS] Minimum font size: {MIN_FONT_SIZE}px")

def font(size, style="regular"):
    size = max(int(size), MIN_FONT_SIZE)
    path = FONTS.get(style) or FONTS.get("regular")

    try:
        return ImageFont.truetype(str(path), size) if path else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()

# =========================================================
# TEXT / COLOR HELPERS
# =========================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

def is_light(rgb):
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) > 160

def text_width(draw, text, selected_font):
    box = draw.textbbox((0, 0), text, font=selected_font)
    return box[2] - box[0]

def wrap_text(draw, text, selected_font, max_width):
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()

        if text_width(draw, candidate, selected_font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)

            if text_width(draw, word, selected_font) > max_width:
                chunk = ""

                for char in word:
                    if text_width(draw, chunk + char, selected_font) > max_width and chunk:
                        lines.append(chunk)
                        chunk = char
                    else:
                        chunk += char

                current = chunk
            else:
                current = word

    if current:
        lines.append(current)

    return lines

def fit_lines(draw, text, max_width, max_lines, start_size, min_size, style="bold"):
    min_size = max(min_size, MIN_FONT_SIZE)
    start_size = max(start_size, min_size)

    for size in range(start_size, min_size - 1, -2):
        selected_font = font(size, style)
        lines = wrap_text(draw, text, selected_font, max_width)

        if len(lines) <= max_lines:
            return selected_font, lines

    selected_font = font(min_size, style)
    lines = wrap_text(draw, text, selected_font, max_width)

    return selected_font, lines[:max_lines]

def published_date(value):
    try:
        if value:
            return datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).strftime("%d %b")
    except (ValueError, TypeError):
        pass

    return datetime.now().strftime("%d %b")

def clean_metadata(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return clean_text(html.unescape(value))

def normalize_folder_name(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")

# =========================================================
# LOCAL ENTITY IMAGE LIBRARY
# =========================================================

def normalize_match_text(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())

def get_story_tags(data):
    tags = (data.get("classification") or {}).get("tags") or []

    if isinstance(tags, str):
        tags = [tags]

    return [clean_text(tag) for tag in tags if clean_text(tag)]


def get_main_subject(data):
    subject = data.get("main_subject") or {}
    name = clean_text(subject.get("name") or subject.get("image_search_query"))
    return {
        "name": name,
        "type": clean_text(subject.get("type") or "other"),
        "image_search_query": name
    } if name else {}

def get_story_context(data, slide=None, query=""):
    content = data.get("content") or {}
    classification = data.get("classification") or {}

    parts = [
        content.get("title", ""),
        content.get("caption", ""),
        classification.get("category", ""),
        query
    ]

    if slide:
        parts.extend([
            slide.get("heading", ""),
            slide.get("text", ""),
            slide.get("image_search_query", "")
        ])

    return normalize_match_text(" ".join(str(x or "") for x in parts))

def contains_phrase(text, phrase):
    if not text or not phrase:
        return False

    return re.search(
        rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
        text
    ) is not None

def get_direct_images(folder):
    if not folder.exists() or not folder.is_dir():
        return []

    return [
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]

def get_folder_match_score(tag, folder_name):
    tag = normalize_match_text(tag)
    folder_name = normalize_match_text(folder_name)

    if not tag or not folder_name:
        return 0

    # Full folder name matches the tag.
    if tag == folder_name:
        return 120

    # Complete folder name or tag appears inside the other.
    if contains_phrase(tag, folder_name):
        return 110

    if contains_phrase(folder_name, tag):
        return 100

    # Match meaningful individual words.
    # Example: "Gandhi" matches "Mahatma Gandhi".
    tag_words = set(tag.split())
    folder_words = set(folder_name.split())

    matched_words = [
        word for word in tag_words.intersection(folder_words)
        if len(word) >= 4
    ]

    if matched_words:
        return 80 + max(len(word) for word in matched_words)

    return 0

def get_local_entity_match(data, slide=None, query=""):
    if not IMAGE_ROOT.exists():
        print(f"[LOCAL IMAGE] Image directory not found: {IMAGE_ROOT}")
        return None

    # Prioritize the main subject's folder for the cover.
    if slide and slide.get("_is_cover"):
        subject = get_main_subject(data)
        subject_name = subject.get("name", "")

        if subject_name:
            for folder in IMAGE_ROOT.iterdir():
                if not folder.is_dir():
                    continue

                if get_folder_match_score(subject_name, folder.name) >= 100:
                    images = get_direct_images(folder)

                    if images:
                        unused = [
                            path for path in images
                            if str(path.resolve()) not in CURRENT_POST_USED_LOCAL_IMAGES
                        ]
                        candidates = unused or images
                        random.shuffle(candidates)

                        for selected in candidates:
                            try:
                                image = ImageOps.exif_transpose(
                                    Image.open(selected)
                                ).convert("RGB")

                                if image.width < 300 or image.height < 300:
                                    continue

                                CURRENT_POST_USED_LOCAL_IMAGES.add(str(selected.resolve()))
                                print(f"[LOCAL IMAGE] Main subject folder: {folder.name}")
                                print(f"[LOCAL IMAGE] Selected: {selected}")

                                return folder, [{
                                    "path": selected,
                                    "image": image
                                }]

                            except Exception as exc:
                                print(f"[LOCAL IMAGE] Failed to open {selected}: {exc}")

    tags = get_story_tags(data)

    if not tags:
        print("[LOCAL IMAGE] No AI tags available.")
        return None

    matches = []

    for folder in IMAGE_ROOT.iterdir():
        if not folder.is_dir():
            continue

        images = get_direct_images(folder)

        if not images:
            continue

        best_tag_score = 0
        matched_tag = ""

        for tag in tags:
            score = get_folder_match_score(tag, folder.name)

            if score > best_tag_score:
                best_tag_score = score
                matched_tag = tag

        if best_tag_score:
            matches.append({
                "score": best_tag_score,
                "folder": folder,
                "images": images,
                "tag": matched_tag
            })

    if not matches:
        print("[LOCAL IMAGE] No matching entity folder found.")
        return None

    best_score = max(item["score"] for item in matches)
    best_matches = [item for item in matches if item["score"] == best_score]
    selected_match = random.choice(best_matches)

    folder = selected_match["folder"]
    images = selected_match["images"]

    print(
        f"[LOCAL IMAGE] Matched folder: {folder.name} "
        f"| Tag: {selected_match['tag']} "
        f"| Score: {best_score} "
        f"| Images: {len(images)}"
    )

    return folder, images

def search_local_entity_image(data, slide=None, query=""):
    match = get_local_entity_match(data, slide, query)

    if not match:
        return None

    folder, images = match

    # Main-subject folder may return an already loaded image.
    if images and isinstance(images[0], dict):
        selected = images[0]["path"]
        image = images[0]["image"]

        CURRENT_POST_USED_LOCAL_IMAGES.add(str(selected.resolve()))

        return image, {
            "provider": "local-entity",
            "folder": folder.name,
            "source_path": str(selected),
            "query": query,
            "matched_tags": get_story_tags(data),
            "main_subject_match": True,
            "fallback_used": False,
            "local_default": False
        }

    unused = [
        path for path in images
        if str(path.resolve()) not in CURRENT_POST_USED_LOCAL_IMAGES
    ]

    candidates = unused or images
    random.shuffle(candidates)

    for selected in candidates:
        try:
            image = ImageOps.exif_transpose(
                Image.open(selected)
            ).convert("RGB")

            if image.width < 300 or image.height < 300:
                print(f"[LOCAL IMAGE] Image too small: {selected}")
                continue

            CURRENT_POST_USED_LOCAL_IMAGES.add(str(selected.resolve()))

            print(f"[LOCAL IMAGE] Selected: {selected}")

            return image, {
                "provider": "local-entity",
                "folder": folder.name,
                "source_path": str(selected),
                "query": query,
                "matched_tags": get_story_tags(data),
                "fallback_used": False,
                "local_default": False
            }

        except Exception as exc:
            print(f"[LOCAL IMAGE] Failed to open {selected}: {exc}")

    return None

# =========================================================
# IMAGE PROVIDER: UNSPLASH
# =========================================================

def add_utm(url):
    if not url:
        return ""

    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))

    params.update({
        "utm_source": "ai_news_publisher",
        "utm_medium": "referral"
    })

    return urlunparse(parsed._replace(query=urlencode(params)))

def search_unsplash(query):
    if not UNSPLASH_ACCESS_KEY:
        print("[IMAGE] UNSPLASH_ACCESS_KEY missing")
        return None

    try:
        response = SESSION.get(
            "https://api.unsplash.com/search/photos",
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            params={
                "query": query,
                "per_page": 10,
                "orientation": "portrait",
                "content_filter": "high"
            },
            timeout=TIMEOUT
        )

        response.raise_for_status()
        photos = response.json().get("results", [])

        if not photos:
            return None

        photos.sort(
            key=lambda p: (
                p.get("height", 0) >= HEIGHT,
                p.get("width", 0) >= WIDTH,
                p.get("likes", 0)
            ),
            reverse=True
        )

        photo = photos[0]
        user = photo.get("user", {})
        links = photo.get("links", {})
        urls = photo.get("urls", {})

        download_location = links.get("download_location")

        if download_location:
            try:
                SESSION.get(
                    download_location,
                    headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                    timeout=TIMEOUT
                )
            except requests.RequestException:
                pass

        return {
            "provider": "Unsplash",
            "photo_id": photo.get("id"),
            "photographer": user.get("name", ""),
            "photographer_url": add_utm(user.get("links", {}).get("html", "")),
            "source_url": add_utm(links.get("html", "")),
            "image_url": urls.get("regular") or urls.get("full"),
            "query": query
        }

    except requests.RequestException as exc:
        print(f"[IMAGE] Unsplash error: {exc}")
        return None

# =========================================================
# IMAGE PROVIDER: WIKIMEDIA COMMONS
# =========================================================

def search_wikimedia(query):
    try:
        response = SESSION.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": 6,
                "gsrlimit": 10,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": WIDTH,
                "format": "json"
            },
            headers={
                "User-Agent": "AINewsPublisher/2.0 (news image publisher)"
            },
            timeout=TIMEOUT
        )

        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})

        if not pages:
            return None

        candidates = []

        for page in pages.values():
            image_info = (page.get("imageinfo") or [{}])[0]
            metadata = image_info.get("extmetadata", {})

            image_url = image_info.get("thumburl") or image_info.get("url")

            if not image_url:
                continue

            candidates.append({
                "provider": "Wikimedia Commons",
                "photographer": clean_metadata(
                    metadata.get("Artist", {}).get("value", "")
                ),
                "license": clean_metadata(
                    metadata.get("LicenseShortName", {}).get("value", "")
                ),
                "license_url": metadata.get("LicenseUrl", {}).get("value", ""),
                "source_url": image_info.get("descriptionurl", ""),
                "image_url": image_url,
                "query": query
            })

        if not candidates:
            return None

        return random.choice(candidates)

    except requests.RequestException as exc:
        print(f"[IMAGE] Wikimedia error: {exc}")
        return None
    except (ValueError, KeyError, TypeError) as exc:
        print(f"[IMAGE] Wikimedia response error: {exc}")
        return None

# =========================================================
# IMAGE DOWNLOAD
# =========================================================

def download_image(info):
    if not info or not info.get("image_url"):
        return None

    try:
        response = SESSION.get(info["image_url"], timeout=TIMEOUT)
        response.raise_for_status()

        image = ImageOps.exif_transpose(
            Image.open(BytesIO(response.content))
        ).convert("RGB")

        if image.width < 300 or image.height < 300:
            print("[IMAGE] Downloaded image is too small")
            return None

        return image

    except Exception as exc:
        print(f"[IMAGE] Download failed: {exc}")
        return None

# =========================================================
# LOCAL CATEGORY IMAGE FALLBACK
# =========================================================

def pick_default_local_image(category=None, tags=None):
    folders = []

    category = normalize_folder_name(category or "")
    tags = tags or []

    if isinstance(tags, str):
        tags = [tags]

    if category:
        folders.extend([
            IMAGE_ROOT / "news" / category,
            IMAGE_ROOT / category
        ])

    for tag in tags:
        tag = normalize_folder_name(tag)

        if not tag:
            continue

        folders.extend([
            IMAGE_ROOT / "news" / tag,
            IMAGE_ROOT / "places" / tag,
            IMAGE_ROOT / tag
        ])

    if category:
        folders.append(IMAGE_ROOT / "backgrounds" / category)

    folders.extend([
        IMAGE_ROOT / "placeholders" / "default-news",
        IMAGE_ROOT / "placeholders"
    ])

    seen_folders = set()
    candidates = []

    for folder in folders:
        if not folder.exists() or not folder.is_dir():
            continue

        resolved_folder = str(folder.resolve())

        if resolved_folder in seen_folders:
            continue

        seen_folders.add(resolved_folder)

        directories = [folder] + [
            path for path in folder.rglob("*") if path.is_dir()
        ]

        for directory in directories:
            candidates.extend(get_direct_images(directory))

    candidates.extend(get_direct_images(IMAGE_ROOT))
    candidates = list(dict.fromkeys(candidates))

    if not candidates:
        return None

    unused = [
        path for path in candidates
        if str(path.resolve()) not in CURRENT_POST_USED_LOCAL_IMAGES
    ]

    selected = random.choice(unused or candidates)
    CURRENT_POST_USED_LOCAL_IMAGES.add(str(selected.resolve()))

    print(f"[IMAGE] Local fallback: {selected}")

    return str(selected)

# =========================================================
# IMAGE SELECTION PIPELINE
# =========================================================

def get_image(query, category=None, tags=None, data=None, slide=None):
    category = clean_text(category).lower()
    theme = THEMES.get(category, DEFAULT_THEME)

    provider = os.getenv(
        f"IMAGE_PROVIDER_{category.upper()}",
        DEFAULT_PROVIDER
    ).lower()

    data = data or {}
    tags = tags or get_story_tags(data)

    # 1. Match a local entity folder using AI tags.
    local_result = search_local_entity_image(data, slide, query)

    if local_result:
        image, info = local_result
        info["original_query"] = query
        return image, info

    # 2. Search internet providers.
    if slide and slide.get("_is_cover") and get_main_subject(data):
        queries = [get_main_subject(data)["name"]]
    else:
        queries = [
            clean_text(query),
            clean_text(theme["fallback"]),
            "editorial news photography"
        ]

    unique_queries = []

    for q in queries:
        if q and q.lower() not in [item.lower() for item in unique_queries]:
            unique_queries.append(q)

    for index, search_query in enumerate(unique_queries, start=1):
        print(
            f"\n[IMAGE] Internet search "
            f"{index}/{len(unique_queries)}: {search_query}"
        )

        if provider == "unsplash":
            info = search_unsplash(search_query)

            if info:
                image = download_image(info)

                if image is not None:
                    info["fallback_used"] = index > 1
                    info["original_query"] = query
                    print("[IMAGE] Selected: Unsplash")
                    return image, info

        else:
            print(
                f"[IMAGE] Unsupported provider '{provider}', "
                "trying Wikimedia"
            )

        print(f"[IMAGE] Trying Wikimedia Commons: {search_query}")

        info = search_wikimedia(search_query)

        if info:
            image = download_image(info)

            if image is not None:
                info["fallback_used"] = True
                info["original_query"] = query
                print("[IMAGE] Selected: Wikimedia Commons")
                return image, info

    # 3. Search local category/default folders.
    fallback_path = pick_default_local_image(category, tags)

    if fallback_path:
        try:
            local_image = ImageOps.exif_transpose(
                Image.open(fallback_path)
            ).convert("RGB")

            return local_image, {
                "provider": "local-default",
                "query": query,
                "fallback_query": theme["fallback"],
                "source_path": fallback_path,
                "fallback_used": True,
                "local_default": True
            }

        except Exception as exc:
            print(f"[IMAGE] Local default image failed: {exc}")

    # 4. Final fallback: generated background.
    print("[IMAGE] No image found. Using generated background.")

    return None, {
        "provider": "generated",
        "query": query,
        "fallback_query": theme["fallback"],
        "fallback_used": True,
        "error": "No image found"
    }

# =========================================================
# BACKGROUND DESIGN
# =========================================================

def generated_fallback(theme):
    accent = hex_to_rgb(theme["accent"])
    second = (accent[2], accent[0], accent[1])

    base = Image.new("RGBA", (WIDTH, HEIGHT), (12, 18, 28, 255))
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    draw.ellipse((-300, -260, 640, 680), fill=(*accent, 255))
    draw.ellipse((470, 760, 1400, 1700), fill=(*second, 255))

    layer = layer.filter(ImageFilter.GaussianBlur(8))

    return Image.alpha_composite(base, layer).convert("RGB")

def make_background(query, category, theme, style="photo", tags=None, data=None, slide=None):
    image, info = get_image(
        query,
        category,
        tags,
        data=data,
        slide=slide
    )

    if image is None:
        image = generated_fallback(theme)
        info["fallback_graphic"] = True
    else:
        image = ImageOps.fit(
            image,
            (WIDTH, HEIGHT),
            method=Image.Resampling.LANCZOS
        )

    if style == "glass":
        image = ImageEnhance.Color(image).enhance(1.2)
        image = ImageEnhance.Brightness(image).enhance(0.75)
        image = image.convert("RGBA")
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (8, 10, 16, 90))
    else:
        image = ImageEnhance.Brightness(image).enhance(0.68)
        image = ImageEnhance.Contrast(image).enhance(1.15)
        image = image.convert("RGBA")

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        for y in range(HEIGHT):
            t = y / HEIGHT
            alpha = int(60 + 175 * max(0, (t - 0.15) / 0.85) ** 1.6)
            draw.line((0, y, WIDTH, y), fill=(8, 10, 16, alpha))

    image = Image.alpha_composite(image, overlay)

    return image.convert("RGB"), info

# =========================================================
# GLASSMORPHISM
# =========================================================

def draw_glass_card(image, box, radius=44, blur=45):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    source = image.convert("RGBA")
    base = source.copy()

    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))

    ImageDraw.Draw(shadow).rounded_rectangle(
        (x0, y0 + 22, x1, y1 + 22),
        radius=radius,
        fill=(0, 0, 0, 120)
    )

    shadow = shadow.filter(ImageFilter.GaussianBlur(32))
    base = Image.alpha_composite(base, shadow)

    frosted = source.filter(ImageFilter.GaussianBlur(blur)).crop(box)

    frosted = Image.alpha_composite(
        frosted,
        Image.new("RGBA", (w, h), (10, 14, 24, 85))
    )

    gradient = Image.linear_gradient("L").resize((w, h))
    sheen_alpha = gradient.point(lambda v: 10 + int(60 * (255 - v) / 255))
    sheen = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    sheen.putalpha(sheen_alpha)

    frosted = Image.alpha_composite(frosted, sheen)

    ss = 3
    mask = Image.new("L", (w * ss, h * ss), 0)

    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1),
        radius=radius * ss,
        fill=255
    )

    mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    base.paste(frosted, (x0, y0), mask)

    border = Image.new("RGBA", base.size, (0, 0, 0, 0))

    ImageDraw.Draw(border).rounded_rectangle(
        box,
        radius=radius,
        outline=(255, 255, 255, 100),
        width=2
    )

    base = Image.alpha_composite(base, border)

    return base.convert("RGB")

# =========================================================
# COMMON DESIGN ELEMENTS
# =========================================================

def draw_top(draw, date_text):
    draw.rounded_rectangle(
        (64, 50, 390, 126),
        radius=20,
        fill=(10, 12, 18, 185)
    )

    draw.text(
        (88, 67),
        BRAND,
        font=font(34, "bold"),
        fill="white"
    )

    draw.text(
        (WIDTH - 70, 72),
        date_text.upper(),
        font=font(30, "bold"),
        fill="white",
        anchor="ra"
    )

def draw_category(draw, category, theme, y=165):
    label = theme["label"]
    selected_font = font(24, "bold")
    width = text_width(draw, label, selected_font) + 42
    accent = hex_to_rgb(theme["accent"])

    draw.rounded_rectangle(
        (70, y, 70 + width, y + 54),
        radius=16,
        fill=theme["accent"]
    )

    draw.text(
        (91, y + 12),
        label,
        font=selected_font,
        fill=(15, 18, 26) if is_light(accent) else "white"
    )

def draw_footer(draw, index, total, accent):
    y = HEIGHT - 85

    draw.rounded_rectangle(
        (70, y, WIDTH - 70, y + 6),
        radius=3,
        fill=(255, 255, 255, 80)
    )

    progress = int((WIDTH - 140) * index / max(total, 1))

    draw.rounded_rectangle(
        (70, y, 70 + progress, y + 6),
        radius=3,
        fill=accent
    )

    draw.text(
        (70, y + 18),
        "SWIPE FOR THE FULL STORY",
        font=font(30, "bold"),
        fill="white"
    )

    draw.text(
        (WIDTH - 70, y + 16),
        f"{index:02d} / {total:02d}",
        font=font(30, "bold"),
        fill="white",
        anchor="ra"
    )

# =========================================================
# COVER SLIDE
# =========================================================
def render_cover(data, slide, theme, date_text, total, category):
    main_subject = get_main_subject(data)

    # Cover image search must use only the main subject's name.
    query = main_subject.get("name") or clean_text(
        slide.get("image_search_query")
        or slide.get("heading")
        or data.get("content", {}).get("title")
    )

    # Mark this slide so local image search prioritizes the subject folder.
    slide = dict(slide)
    slide["_is_cover"] = True

    tags = get_story_tags(data)

    if main_subject.get("name") and main_subject["name"] not in tags:
        tags.insert(0, main_subject["name"])

    image, info = make_background(
        query,
        category,
        theme,
        style="photo",
        tags=tags,
        data=data,
        slide=slide
    )

    draw = ImageDraw.Draw(image, "RGBA")
    draw_top(draw, date_text)

    title = clean_text(
        slide.get("heading") or data.get("content", {}).get("title")
    ).upper()

    title_font, title_lines = fit_lines(
        draw, title, 930, 4, 78, MIN_FONT_SIZE, "bold"
    )

    title_y = 440
    line_height = int(title_font.size * 1.15)

    for line in title_lines:
        draw.text(
            (70, title_y),
            line,
            font=title_font,
            fill="white",
            stroke_width=2,
            stroke_fill=(0, 0, 0, 120)
        )
        title_y += line_height

    body = clean_text(slide.get("text", ""))

    if body:
        body_font = font(44, "regular")
        body_lines = wrap_text(draw, body, body_font, 920)[:4]
        y = title_y + 30

        for line in body_lines:
            draw.text(
                (75, y),
                line,
                font=body_font,
                fill=(255, 255, 255, 255),
                stroke_width=1,
                stroke_fill=(0, 0, 0, 100)
            )
            y += int(body_font.size * 1.4)

    draw_footer(draw, 1, total, theme["accent"])

    return image.convert("RGB"), info

# =========================================================
# CONTENT SLIDES
# =========================================================

def collect_highlights(slide):
    raw = slide.get("highlight_words") or slide.get("highlight_phrases") or []

    if isinstance(raw, str):
        raw = [raw]

    raw = list(raw)

    if slide.get("highlight_text"):
        raw.append(slide["highlight_text"])

    words = set()

    for item in raw:
        for part in clean_text(item).lower().split():
            norm = re.sub(r"[^\w'-]", "", part)
            if norm:
                words.add(norm)

    return words

def render_content(data, slide, theme, date_text, index, total, category):
    query = clean_text(slide.get("image_search_query") or slide.get("heading"))
    tags = get_story_tags(data)

    image, info = make_background(
        query,
        category,
        theme,
        style="glass",
        tags=tags,
        data=data,
        slide=slide
    )

    panel = (60, 250, WIDTH - 60, 1140)
    image = draw_glass_card(image, panel, radius=44, blur=45)

    draw = ImageDraw.Draw(image, "RGBA")
    draw_top(draw, date_text)

    heading = clean_text(slide.get("heading", "")).upper()
    body = clean_text(slide.get("text", ""))

    padding = 64
    max_width = (panel[2] - panel[0]) - padding * 2
    max_height = (panel[3] - panel[1]) - padding * 2

    accent = hex_to_rgb(theme["accent"])
    accent_bar_h = 10
    accent_gap = 24
    spacing = 24

    headline_font, headline_lines = fit_lines(
        draw, heading, max_width, 3, 72, MIN_FONT_SIZE, "bold"
    )

    headline_lh = int(headline_font.size * 1.18)
    headline_h = len(headline_lines) * headline_lh

    available = max_height - accent_bar_h - accent_gap - headline_h - spacing
    body_lines, body_font, body_lh = [], font(42), int(42 * 1.35)

    if body:
        for size in range(52, MIN_FONT_SIZE - 1, -2):
            body_font = font(size, "regular")
            body_lh = int(body_font.size * 1.35)
            body_lines = wrap_text(draw, body, body_font, max_width)

            if len(body_lines) * body_lh <= available:
                break
        else:
            body_font = font(MIN_FONT_SIZE, "regular")
            body_lh = int(body_font.size * 1.35)
            body_lines = wrap_text(draw, body, body_font, max_width)

            keep = max(1, available // body_lh)

            if len(body_lines) > keep:
                body_lines = body_lines[:keep]
                body_lines[-1] = body_lines[-1].rstrip(".,;: ") + "..."

    body_h = len(body_lines) * body_lh

    block_h = (
        accent_bar_h + accent_gap + headline_h
        + (spacing if body_lines else 0) + body_h
    )

    y = panel[1] + max(
        padding,
        ((panel[3] - panel[1]) - block_h) // 2
    )

    x_left = panel[0] + padding

    draw.rounded_rectangle(
        (x_left, y, x_left + 110, y + accent_bar_h),
        radius=5,
        fill=theme["accent"]
    )

    y += accent_bar_h + accent_gap

    for line in headline_lines:
        draw.text(
            (x_left, y),
            line,
            font=headline_font,
            fill="white"
        )
        y += headline_lh

    if body_lines:
        y += spacing

    highlights = collect_highlights(slide)
    highlight_text_color = (
        (15, 18, 26) if is_light(accent) else (255, 255, 255)
    )

    space_width = text_width(draw, " ", body_font)

    for line in body_lines:
        x = x_left

        for word in line.split():
            word_w = text_width(draw, word, body_font)
            norm = re.sub(r"[^\w'-]", "", word.lower())
            is_hl = norm in highlights

            if is_hl:
                draw.rounded_rectangle(
                    (
                        x - 8,
                        y - 2,
                        x + word_w + 8,
                        y + body_font.size + 10
                    ),
                    radius=8,
                    fill=theme["accent"]
                )

            draw.text(
                (x, y),
                word,
                font=body_font,
                fill=highlight_text_color if is_hl else (244, 246, 250, 255)
            )

            x += word_w + space_width

        y += body_lh

    draw_footer(draw, index, total, theme["accent"])

    return image.convert("RGB"), info

# =========================================================
# SOURCE SLIDE
# =========================================================

def render_source(data, theme, date_text, index, total, category):
    query = theme.get("fallback", "journalism newsroom")
    tags = get_story_tags(data)

    image, info = make_background(
        query,
        category,
        theme,
        style="photo",
        tags=tags,
        data=data
    )

    draw = ImageDraw.Draw(image, "RGBA")

    accent = hex_to_rgb(theme["accent"])
    on_accent = (15, 18, 26) if is_light(accent) else "white"

    draw_top(draw, date_text)

    draw.text(
        (70, 430),
        "STAY INFORMED",
        font=font(72, "bold"),
        fill="white"
    )

    draw.rounded_rectangle(
        (70, 535, 390, 600),
        radius=16,
        fill=theme["accent"]
    )

    draw.text(
        (94, 545),
        "SOURCE",
        font=font(42, "bold"),
        fill=on_accent
    )

    source = clean_text(data.get("source", "Original news source"))

    source_font, source_lines = fit_lines(
        draw, source, 880, 2, 42, MIN_FONT_SIZE, "bold"
    )

    y = 665

    for line in source_lines:
        draw.text((75, y), line, font=source_font, fill="white")
        y += int(source_font.size * 1.3)

    url = clean_text(data.get("source_url") or WEBSITE)

    url_font, url_lines = fit_lines(
        draw, url, 880, 2, 26, MIN_FONT_SIZE, "regular"
    )

    y += 25

    for line in url_lines:
        draw.text(
            (75, y),
            line,
            font=url_font,
            fill=(255, 255, 255, 225)
        )
        y += int(url_font.size * 1.4)

    draw.rounded_rectangle(
        (70, 965, WIDTH - 70, 1045),
        radius=18,
        fill=theme["accent"]
    )

    draw.text(
        (95, 985),
        "FOLLOW FOR MORE STORIES",
        font=font(25, "bold"),
        fill=on_accent
    )

    draw_footer(draw, index, total, theme["accent"])

    return image.convert("RGB"), info

# =========================================================
# MAIN EXECUTION
# =========================================================

def content_date_key(value=None):
    date_value = value or datetime.now().isoformat()

    try:
        parsed = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")

def generate_post(json_file):
    CURRENT_POST_USED_LOCAL_IMAGES.clear()
    json_file = Path(json_file)

    with open(json_file, "r", encoding="utf-8") as file:
        data = json.load(file)

    news_id = clean_text(data.get("news_id")) or json_file.stem

    classification = data.get("classification") or {}
    content = data.get("content") or {}

    category = clean_text(classification.get("category", "world")).lower()
    theme = THEMES.get(category, DEFAULT_THEME)

    slides = content.get("slides") or []

    if not slides:
        raise ValueError("No slides found in JSON: expected content.slides")

    date_key = content_date_key(data.get("published_at"))
    output_dir = PUBLIC_DIR / date_key / news_id
    output_dir.mkdir(parents=True, exist_ok=True)

    date_text = published_date(data.get("published_at"))
    total = len(slides) + 1

    manifest = {
        "news_id": news_id,
        "title": content.get("title", ""),
        "main_subject": get_main_subject(data),
        "category": category,
        "tags": classification.get("tags", []),
        "source": data.get("source", ""),
        "source_url": data.get("source_url", ""),
        "published_at": data.get("published_at", ""),
        "provider_default": DEFAULT_PROVIDER,
        "image_count": total,
        "images": []
    }

    print(f"\n[NEWS] {content.get('title', news_id)}")
    print(f"[CATEGORY] {category}")
    print(f"[TAGS] {classification.get('tags', [])}")

    for index, slide in enumerate(slides, start=1):
        is_cover = (
            index == 1
            or clean_text(slide.get("type")).lower() == "cover"
        )

        if is_cover:
            image, info = render_cover(
                data, slide, theme, date_text, total, category
            )
        else:
            image, info = render_content(
                data, slide, theme, date_text, index, total, category
            )

        filename = f"slide_{index:02d}.png"
        image.save(output_dir / filename, "PNG", optimize=True)

        manifest["images"].append({
            "file": filename,
            "slide_number": index,
            "heading": slide.get("heading", ""),
            "image": info
        })

        print(f"[SAVED] {output_dir / filename}")

    final_image, info = render_source(
        data, theme, date_text, total, total, category
    )

    filename = f"slide_{total:02d}.png"
    final_image.save(output_dir / filename, "PNG", optimize=True)

    manifest["images"].append({
        "file": filename,
        "slide_number": total,
        "type": "source_follow",
        "image": info
    })

    # Save manifest only after every slide has been generated.
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    # Mark the source JSON complete only after successful image generation.
    data["image_done"] = True
    json_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"[DONE] {output_dir}")
    print(f"[STATUS] image_done = true")

    return total

def load_json_files():
    if not CONTENT_DIR.exists():
        raise FileNotFoundError(f"Content folder not found: {CONTENT_DIR}")

    files = []

    for path in CONTENT_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            if data.get("image_done") is not True:
                files.append(path)

        except Exception as exc:
            print(f"[SKIP] Invalid JSON {path.name}: {exc}")

    return sorted(files, key=lambda p: p.stat().st_mtime)

def main(filename=None):
    discover_fonts()

    if filename:
        path = Path(filename)

        if not path.is_absolute():
            path = BASE_DIR / path

        files = [path]
    else:
        files = load_json_files()[-MAX_NEWS_PER_RUN:]

    if not files:
        print("[INFO] No JSON files found")
        return

    success = 0
    failed = 0

    print("\n========== NEWS IMAGE GENERATOR ==========")
    print(f"Provider: {DEFAULT_PROVIDER}")
    print("Image priority: Local Entity Images → Unsplash → Wikimedia → Local Default → Generated")
    print(f"Local image directory: {IMAGE_ROOT}")
    print(f"Minimum font size: {MIN_FONT_SIZE}px")
    print(f"Output: {PUBLIC_DIR}")
    print(f"Size: {WIDTH}x{HEIGHT}")
    print("==========================================")

    for path in files:
        try:
            if not path.exists():
                raise FileNotFoundError(path)

            generate_post(path)
            success += 1

        except Exception as exc:
            print(f"[FAILED] {path.name}: {exc}")
            failed += 1

    print(f"\n[SUMMARY] Success: {success} | Failed: {failed}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
