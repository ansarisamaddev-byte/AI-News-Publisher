
import csv
import json
import os
import re
import time
import requests
import trafilatura

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from llama_cpp import Llama


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
NEWS_DIR = BASE_DIR / "data" / "news"
CONTENT_DIR = BASE_DIR / "data" / "content"

TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date()
QUEUE_FILE = NEWS_DIR / f"{TODAY}.csv"

MAX_NEWS_PER_RUN = 10
TOP_NEWS_LIMIT = 20
MAX_ARTICLE_CHARS = 14000
REQUEST_TIMEOUT = 25

MIN_SLIDES = 3
MAX_SLIDES = 4
MAX_SLIDE_WORDS = 35
MAX_HEADING_WORDS = 10
MIN_IMAGE_QUERY_WORDS = 3
MAX_IMAGE_QUERY_WORDS = 8

llm = None


# ============================================================
# ENVIRONMENT
# ============================================================

def load_env_file(env_file=None):
    env_path = Path(env_file) if env_file else BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

MODEL_PATH = Path(
    os.getenv(
        "LOCAL_MODEL_PATH",
        BASE_DIR / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    )
)

MODEL_NAME = MODEL_PATH.name
N_CTX = int(os.getenv("LLM_N_CTX", "8192"))
N_THREADS = int(os.getenv("LLM_N_THREADS", "4"))
N_GPU_LAYERS = int(os.getenv("LLM_N_GPU_LAYERS", "0"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AINewsPublisher/1.0)"
}

CATEGORIES = [
    "technology", "business", "india", "world", "politics",
    "science", "sports", "entertainment", "automobile",
    "education", "health"
]

CSV_EXTRA_FIELDS = [
    "country", "category", "subcategory", "importance_score",
    "relevance_score", "tags", "selected_channels",
    "classified", "ai_done"
]


# ============================================================
# MODEL
# ============================================================

def get_client():
    global llm

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Local model not found: {MODEL_PATH}\n"
            "Set LOCAL_MODEL_PATH in .env or place the GGUF file "
            "inside the models folder."
        )

    if llm is None:
        print(f"[MODEL] Loading local model: {MODEL_PATH}")
        llm = Llama(
            model_path=str(MODEL_PATH),
            n_ctx=N_CTX,
            n_threads=N_THREADS,
            n_gpu_layers=N_GPU_LAYERS,
            verbose=False
        )
        print("[MODEL] Qwen loaded successfully.")

    return llm


# ============================================================
# CSV QUEUE
# ============================================================

def load_queue():
    if not QUEUE_FILE.exists():
        raise FileNotFoundError(
            f"News CSV not found: {QUEUE_FILE}\n"
            "Run ai_news_fetcher.py before ai_content_generator.py."
        )

    with open(QUEUE_FILE, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fields = reader.fieldnames

    if not fields:
        raise ValueError("CSV file has no headers.")

    required_fields = ["news_id", "title", "description", "url", "ai_done"]

    for field in required_fields:
        if field not in fields:
            raise ValueError(f"Missing required CSV column: {field}")

    for field in CSV_EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
            for row in rows:
                row[field] = ""

    return rows, fields


def save_queue(rows, fields):
    temp_file = QUEUE_FILE.with_suffix(".tmp")
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(temp_file, "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)

    temp_file.replace(QUEUE_FILE)


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def extract_article(url):
    if not url:
        return "", False

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        text = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True
        )

        if not text:
            text = trafilatura.extract(
                response.text,
                include_comments=False,
                include_tables=False,
                favor_recall=True
            )

        if text:
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 300:
                return text[:MAX_ARTICLE_CHARS], True

    except Exception as error:
        print(f"[ARTICLE ERROR] {error}")

    return "", False


# ============================================================
# COUNTRY DETECTION
# ============================================================

US_STATES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS",
    "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE",
    "FLORIDA", "HAWAII", "IDAHO", "ILLINOIS",
    "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA",
    "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN",
    "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA",
    "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY",
    "NEW MEXICO", "NEW YORK", "NORTH CAROLINA",
    "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON",
    "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA",
    "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH",
    "VERMONT", "VIRGINIA", "WEST VIRGINIA",
    "WISCONSIN", "WYOMING"
}

INDIAN_STATES = {
    "ANDHRA PRADESH", "ARUNACHAL PRADESH", "ASSAM",
    "BIHAR", "CHHATTISGARH", "GOA", "GUJARAT",
    "HARYANA", "HIMACHAL PRADESH", "JHARKHAND",
    "KARNATAKA", "KERALA", "MADHYA PRADESH",
    "MAHARASHTRA", "MANIPUR", "MEGHALAYA", "MIZORAM",
    "NAGALAND", "ODISHA", "PUNJAB", "RAJASTHAN",
    "SIKKIM", "TAMIL NADU", "TELANGANA", "TRIPURA",
    "UTTAR PRADESH", "UTTARAKHAND", "WEST BENGAL",
    "DELHI", "JAMMU AND KASHMIR", "LADAKH",
    "PUDUCHERRY", "CHANDIGARH"
}

COUNTRY_NAMES = {
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "USA": "US",
    "UNITED KINGDOM": "UK",
    "GREAT BRITAIN": "UK",
    "INDIA": "INDIA",
    "CANADA": "CANADA",
    "AUSTRALIA": "AUSTRALIA",
    "FRANCE": "FRANCE",
    "GERMANY": "GERMANY",
    "CHINA": "CHINA",
    "JAPAN": "JAPAN",
    "BRAZIL": "BRAZIL",
    "RUSSIA": "RUSSIA",
    "SOUTH KOREA": "SOUTH KOREA",
    "UNITED ARAB EMIRATES": "UNITED ARAB EMIRATES",
    "SINGAPORE": "SINGAPORE",
    "PAKISTAN": "PAKISTAN",
    "BANGLADESH": "BANGLADESH",
    "NEPAL": "NEPAL",
    "SRI LANKA": "SRI LANKA",
    "UKRAINE": "UKRAINE",
    "ISRAEL": "ISRAEL",
    "IRAN": "IRAN",
    "IRAQ": "IRAQ",
    "SAUDI ARABIA": "SAUDI ARABIA",
    "SOUTH AFRICA": "SOUTH AFRICA",
    "NEW ZEALAND": "NEW ZEALAND",
    "MEXICO": "MEXICO",
    "ITALY": "ITALY",
    "SPAIN": "SPAIN",
    "TURKEY": "TURKEY",
    "INDONESIA": "INDONESIA",
    "THAILAND": "THAILAND",
    "PHILIPPINES": "PHILIPPINES",
    "NORTH KOREA": "NORTH KOREA"
}

COUNTRY_IMAGE_NAMES = {
    "US": "United States",
    "UK": "United Kingdom",
    "UNITED ARAB EMIRATES": "UAE"
}


def word_count(text):
    return len(re.findall(r"\b[\w’'-]+\b", str(text).strip()))


def normalize_country(country):
    if not isinstance(country, str):
        return None

    country = re.sub(r"\s+", " ", country.strip()).upper()

    if country in {"", "UNKNOWN", "GLOBAL", "NULL", "NONE", "N/A"}:
        return None

    aliases = {
        "UNITED STATES OF AMERICA": "US",
        "UNITED STATES": "US",
        "USA": "US",
        "U.S.": "US",
        "U.S.A.": "US",
        "UNITED KINGDOM": "UK",
        "GREAT BRITAIN": "UK",
        "U.K.": "UK",
        "UAE": "UNITED ARAB EMIRATES"
    }

    return aliases.get(country, country)


def contains_phrase(text, phrase):
    return bool(
        re.search(
            rf"(?<![A-Z]){re.escape(phrase)}(?![A-Z])",
            text
        )
    )


def detect_country_from_region(region, article_text):
    if not isinstance(region, str) or not region.strip():
        return None

    region_upper = re.sub(r"\s+", " ", region).upper()
    evidence = re.sub(r"\s+", " ", str(article_text)).upper()

    for state in US_STATES:
        if contains_phrase(region_upper, state) and contains_phrase(evidence, state):
            return "US"

    for state in INDIAN_STATES:
        if contains_phrase(region_upper, state) and contains_phrase(evidence, state):
            return "INDIA"

    for location, country in COUNTRY_NAMES.items():
        if contains_phrase(region_upper, location) and contains_phrase(evidence, location):
            return country

    return None


def resolve_country(model_country, region, article_text):
    model_country = normalize_country(model_country)
    detected_country = detect_country_from_region(region, article_text)

    print(f"[COUNTRY CHECK] Model: {model_country}")
    print(f"[COUNTRY CHECK] Region: {region}")
    print(f"[COUNTRY CHECK] Detected: {detected_country}")

    if detected_country:
        if model_country != detected_country:
            print(f"[COUNTRY CORRECTION] {model_country} -> {detected_country}")
        return detected_country

    if model_country and re.fullmatch(r"[A-Z][A-Z .'-]*", model_country):
        return model_country

    return None


# ============================================================
# MAIN SUBJECT VALIDATION
# ============================================================

def normalize_subject_text(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower()
    ).strip()


def subject_is_supported(name, article, article_text=""):
    source = " ".join([
        str(article.get("title") or ""),
        str(article.get("description") or ""),
        str(article_text or "")
    ])

    source = normalize_subject_text(source)
    subject = normalize_subject_text(name)

    if not subject:
        return False

    return bool(
        re.search(
            rf"(?<!\w){re.escape(subject)}(?!\w)",
            source
        )
    )


def determine_main_subject(result, article, article_text=""):
    subject = result.get("main_subject") or {}

    if not isinstance(subject, dict):
        raise ValueError("Invalid main_subject object.")

    name = str(subject.get("name") or "").strip()
    subject_type = str(subject.get("type") or "other").strip().lower()

    if not name:
        raise ValueError("Main subject name is missing.")

    if not subject_is_supported(name, article, article_text):
        raise ValueError(
            f"Main subject '{name}' is not supported by the article."
        )

    allowed_types = {
        "person", "place", "character", "company",
        "team", "event", "product", "organization", "other"
    }

    if subject_type not in allowed_types:
        subject_type = "other"

    return {
        "name": name,
        "type": subject_type,
        "image_search_query": name
    }


def apply_main_subject_to_cover(result, article, article_text=""):
    main_subject = determine_main_subject(
        result, article, article_text
    )

    result["main_subject"] = main_subject

    content = result.get("content", {})
    slides = content.get("slides", [])

    if not slides:
        raise ValueError("No slides available for cover.")

    cover = slides[0]
    cover["slide_number"] = 1
    cover["type"] = "cover"

    # Use the actual news title and caption on the cover.
    heading = str(content.get("title") or "").strip()
    text = str(content.get("caption") or "").strip()

    cover["heading"] = " ".join(heading.split()[:MAX_HEADING_WORDS])
    cover["text"] = " ".join(text.split()[:MAX_SLIDE_WORDS])

    # Only the subject's name is used for image search.
    cover["image_search_query"] = main_subject["name"]

    return main_subject


# ============================================================
# IMAGE QUERY COUNTRY SUFFIX
# ============================================================

def add_country_to_image_queries(content, country):
    if not country:
        return

    country_name = COUNTRY_IMAGE_NAMES.get(
        country,
        country.title()
    )

    for index, slide in enumerate(content.get("slides", [])):
        if index == 0 or slide.get("type") == "cover":
            continue

        query = slide.get("image_search_query", "").strip()

        if not query:
            continue

        if contains_phrase(query.upper(), country_name.upper()):
            continue

        slide["image_search_query"] = f"{query} {country_name}"


# ============================================================
# RESULT VALIDATION
# ============================================================

def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("Local model response is not a JSON object.")

    classification = result.get("classification")
    content = result.get("content")
    main_subject = result.get("main_subject")

    if not isinstance(classification, dict):
        raise ValueError("Invalid classification object.")

    if not isinstance(content, dict):
        raise ValueError("Invalid content object.")

    if not isinstance(main_subject, dict):
        raise ValueError("Missing main_subject object.")

    if not isinstance(main_subject.get("name"), str) or not main_subject["name"].strip():
        raise ValueError("Main subject name is missing.")

    if not isinstance(main_subject.get("type"), str):
        raise ValueError("Main subject type is missing.")

    required_classification = [
        "country", "category", "subcategory",
        "importance_score", "relevance_score",
        "region", "tags", "classification_reason"
    ]

    required_content = [
        "title", "caption", "hashtags", "slides"
    ]

    for field in required_classification:
        if field not in classification:
            raise ValueError(f"Missing classification field: {field}")

    for field in required_content:
        if field not in content:
            raise ValueError(f"Missing content field: {field}")

    country = normalize_country(classification["country"])

    if country and not re.fullmatch(r"[A-Z][A-Z .'-]*", country):
        raise ValueError("Invalid country.")

    classification["country"] = country

    if classification["category"] not in CATEGORIES:
        raise ValueError("Invalid news category.")

    for field in ["importance_score", "relevance_score"]:
        score = classification[field]
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 1 <= score <= 10
        ):
            raise ValueError(f"Invalid {field}.")

    if not isinstance(classification["tags"], list):
        raise ValueError("Invalid classification tags.")

    slides = content["slides"]

    if not isinstance(slides, list) or not MIN_SLIDES <= len(slides) <= MAX_SLIDES:
        raise ValueError(
            f"Carousel must contain {MIN_SLIDES} to {MAX_SLIDES} slides."
        )

    if not isinstance(slides[0], dict) or slides[0].get("type") != "cover":
        raise ValueError("Slide 1 must have type 'cover'.")

    image_queries = set()

    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            raise ValueError(f"Slide {index} is invalid.")

        for field in ["heading", "text", "image_search_query", "type"]:
            value = slide.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Slide {index} is missing {field}.")

        if word_count(slide["text"]) > MAX_SLIDE_WORDS:
            raise ValueError(f"Slide {index} text exceeds {MAX_SLIDE_WORDS} words.")

        if word_count(slide["heading"]) > MAX_HEADING_WORDS:
            raise ValueError(f"Slide {index} heading is too long.")

        query = slide["image_search_query"].strip()

        if index == 1:
            if query.lower() != main_subject["name"].strip().lower():
                raise ValueError(
                    "Cover image query must be only the main subject name."
                )
        else:
            query_words = word_count(query)

            if not MIN_IMAGE_QUERY_WORDS <= query_words <= MAX_IMAGE_QUERY_WORDS:
                raise ValueError(
                    f"Slide {index} image query must contain "
                    f"{MIN_IMAGE_QUERY_WORDS}-{MAX_IMAGE_QUERY_WORDS} words."
                )

        normalized_query = query.lower()

        if normalized_query in image_queries:
            raise ValueError(f"Slide {index} repeats an image query.")

        image_queries.add(normalized_query)
        slide["slide_number"] = index

    validate_slide_uniqueness(slides)

    if not isinstance(content["hashtags"], list) or not content["hashtags"]:
        raise ValueError("No hashtags generated.")

    for field in ["title", "caption"]:
        if not isinstance(content[field], str) or not content[field].strip():
            raise ValueError(f"Invalid content field: {field}")

    return True


# ============================================================
# SLIDE UNIQUENESS
# ============================================================

def validate_slide_uniqueness(slides):
    headings = set()
    texts = set()

    for index, slide in enumerate(slides, start=1):
        heading = re.sub(
            r"\s+",
            " ",
            slide["heading"].strip().lower()
        )
        text = re.sub(
            r"\s+",
            " ",
            slide["text"].strip().lower()
        )

        if heading in headings:
            raise ValueError(f"Slide {index} repeats a heading: {heading}")

        if text in texts:
            raise ValueError(f"Slide {index} repeats slide text.")

        headings.add(heading)
        texts.add(text)


# ============================================================
# AI CONTENT GENERATION
# ============================================================

def generate_content(client, article):
    article_text, article_available = extract_article(
        article.get("url", "")
    )

    input_content = article_text or article.get("description", "") or ""

    if len(input_content.strip()) < 50:
        print("[SKIP] Insufficient article information.")
        return None

    prompt = f"""
You are a professional news editor and social media carousel strategist.

Your job is to understand the ACTUAL article and create accurate news content.

IMPORTANT:
Do not copy names from examples or templates.
Every generated field must belong to the current article.
Do not mix information from different people, companies or news stories.

Return ONLY one valid JSON object with this structure:

{{
  "main_subject": {{
    "name": "Actual main subject from this article",
    "type": "person"
  }},
  "classification": {{
    "country": null,
    "category": "technology",
    "subcategory": "specific subcategory",
    "importance_score": 1,
    "relevance_score": 1,
    "region": "",
    "tags": ["relevant tag"],
    "classification_reason": "brief reason"
  }},
  "content": {{
    "title": "Accurate news headline",
    "caption": "Accurate summary of the actual news",
    "hashtags": ["#RelevantTag"],
    "slides": [
      {{
        "slide_number": 1,
        "type": "cover",
        "heading": "Actual news headline",
        "text": "A concise explanation of the actual news story.",
        "image_search_query": "Actual main subject name"
      }},
      {{
        "slide_number": 2,
        "type": "context",
        "heading": "Relevant context",
        "text": "A different important fact from the article.",
        "image_search_query": "descriptive photographic search phrase"
      }},
      {{
        "slide_number": 3,
        "type": "details",
        "heading": "Important details",
        "text": "Another distinct fact supported by the article.",
        "image_search_query": "descriptive photographic search phrase"
      }}
    ]
  }}
}}

MAIN SUBJECT RULES:

- Read the article before choosing the main subject.
- Identify the single most important subject of THIS article.
- The subject can be a person, place, company, organization, product, event, team, character or other specific entity.
- Choose the subject that is central to the actual news, not simply a famous person mentioned in passing.
- Do not automatically choose a CEO, founder, celebrity or politician just because their name appears in the article.
- Do not automatically choose a company when the actual story is about a person.
- Do not automatically choose a person when the actual story is about a company, product, place or event.
- The main subject must be explicitly supported by the article.
- Do not invent or guess the main subject.
- Use the proper recognizable name of the subject.
- Do not use generic topics such as technology, business, AI or politics when a specific subject exists.
- The type must be one of: person, place, character, company, team, event, product, organization, other.
- Do not copy the example subject or example names from this prompt.

COVER RULES:

- Slide 1 must describe the actual news story.
- Slide 1 heading should be the actual news headline or a concise version of it.
- Slide 1 text should explain the actual news in one or two sentences.
- Never use only a person's name as the cover heading.
- Never use only a job title as the cover text.
- Never create a cover unrelated to the article.
- Slide 1 image_search_query must contain ONLY main_subject.name.
- Do not add a job title, company, country, description, portrait, photo or other keywords to the cover query.
- The cover must visually represent the selected main subject.

ALLOWED CATEGORIES:
{", ".join(CATEGORIES)}

CLASSIFICATION RULES:

- Choose exactly ONE primary category.
- Score importance and audience relevance from 1 to 10.
- Do not inflate scores.
- Use a specific subcategory.
- Tags must relate to the actual article.
- Do not use the main subject from another article.

COUNTRY CLASSIFICATION RULES:

- Determine the country from the actual news event.
- Use the full article text as primary evidence.
- Consider event location, people, organizations, governments and geographical context.
- Do not use the publisher's country as the news country.
- Do not use the target audience's country.
- Never assume the country is INDIA.
- Florida means US; New Delhi means INDIA; London means UK.
- Return the country in uppercase.
- If the country cannot be confidently identified, return JSON null.
- Do not return UNKNOWN or GLOBAL.
- Never guess.
- The region field should contain a real location when known.
- If the region is unknown, return an empty string.

COMPACT CAROUSEL RULES:

- Generate exactly 3 or 4 slides.
- Use 3 slides for straightforward stories.
- Use 4 slides only when an important distinct detail requires it.
- Never generate 5 or more slides.
- Each slide communicates ONE main idea.
- Avoid repetition, filler and unnecessary explanations.
- Each slide text must contain no more than 35 words.
- Prefer 12 to 20 words per slide.
- Headings should contain 2 to 7 words.
- Keep the complete carousel around 50 to 70 words.
- Use slide types only: cover, context, details, impact, takeaway.
- Do not force a separate impact or takeaway slide.
- Preserve important facts, names, figures and dates.
- Caption should summarize the actual story concisely.

SUGGESTED STRUCTURE:

3-SLIDE STORY:
1. Cover: What is the actual main news?
2. Context: What happened?
3. Key detail: Why does it matter?

4-SLIDE STORY:
1. Cover: Main news headline.
2. Context: What happened?
3. Details: Important fact or claim.
4. Impact: Important consequence or response.

Choose the structure that best fits the article.

ACCURACY RULES:

- Use only information supported by the article.
- Do not invent facts, quotes, statistics or consequences.
- Clearly attribute allegations and political claims.
- Do not present allegations as established facts.
- Do not confuse different people with similar names.
- Do not introduce unrelated companies, executives or events.
- If the article does not contain enough information, keep the content conservative.

IMAGE SEARCH QUERY RULES:

- Slide 1 query must be ONLY main_subject.name.
- For slides 2 onward, generate descriptive photographic search phrases.
- Use 3 to 8 words for slides 2 onward.
- Use specific subjects, locations, people or objects.
- Different slides should ideally have different image queries.
- Do not add country names. The system handles country information separately.
- If the exact event cannot be photographed, use a realistic representative scene.
- Prefer broad, recognizable visual subjects.

SLIDE UNIQUENESS RULES:

- Every slide must communicate a different piece of information.
- Never repeat the article title or RSS description as the text of multiple slides.
- Do not repeat the same heading across slides.
- Do not paraphrase the same fact to fill the required slide count.
- Each slide must add new information, context, statistics, background or consequences.
- Use only facts supported by the article or source material.

NEVER:

- Copy names from examples.
- Mix unrelated stories.
- Invent a person's involvement.
- Use comma-separated keywords in image queries.
- Copy the article headline into an image query.
- Use abstract concepts such as impact, takeaway, compliance or controversy.
- Use words such as news, breaking news, social media graphic or infographic.
- Request text, logos, watermarks or captions in photos.

ARTICLE INFORMATION:

Original headline: {article.get("title", "")}
Publisher: {article.get("source", "")}
Published at: {article.get("published_at", "")}
Article URL: {article.get("url", "")}
RSS description: {article.get("description", "")}

ARTICLE TEXT:

{input_content}

Full article text successfully extracted: {article_available}

Return valid JSON only.
Do not use Markdown fences.
Do not add explanations outside the JSON.
"""

    for attempt in range(1, 4):
        try:
            response = client.create_chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise news editor. Analyze only the "
                            "current article. Never copy names from examples. "
                            "Identify the actual main subject, which may be a "
                            "person, place, company, product, event or other "
                            "entity. Every claim must be supported by the article. "
                            "The cover must describe the actual news story."
                        )
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=3500
            )

            response_text = response["choices"][0]["message"]["content"]

            if not response_text:
                raise ValueError("Local model returned an empty response.")

            response_text = response_text.strip()

            if response_text.startswith("```"):
                response_text = re.sub(
                    r"^```(?:json)?\s*|\s*```$",
                    "",
                    response_text,
                    flags=re.I
                )

            result = json.loads(response_text)

            # Validate the actual subject before accepting the result.
            apply_main_subject_to_cover(
                result,
                article,
                input_content
            )

            validate_result(result)

            result["_article_evidence"] = input_content
            result["article_text_available"] = article_available
            result["article_text_note"] = (
                "Full article text extracted."
                if article_available
                else "Full article unavailable. Generated using RSS information only."
            )

            return result

        except Exception as error:
            print(f"[AI ERROR] Attempt {attempt}/3: {error}")

            if attempt < 3:
                time.sleep(attempt * 2)

    return None


# ============================================================
# JSON SAVING
# ============================================================

def content_date_key(value=None):
    date_value = value or datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).isoformat()

    try:
        parsed = datetime.fromisoformat(
            str(date_value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=ZoneInfo("Asia/Kolkata")
            )

        return parsed.astimezone(
            ZoneInfo("Asia/Kolkata")
        ).strftime("%Y-%m-%d")

    except Exception:
        return datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).strftime("%Y-%m-%d")


def content_file_for(news_id, published_at=None):
    return (
        CONTENT_DIR
        / content_date_key(published_at)
        / f"{news_id}.json"
    )


def save_json(output_file, output):
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = output_file.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            output,
            file,
            indent=4,
            ensure_ascii=False
        )

    temp_file.replace(output_file)


# ============================================================
# PROCESS NEWS
# ============================================================

def process_news(client, article):
    news_id = str(article.get("news_id") or "").strip()

    if not news_id:
        print("[SKIP] Article has no news_id.")
        return False

    output_file = content_file_for(
        news_id,
        article.get("published_at")
    )

    generated = None

    if output_file.exists():
        try:
            with open(output_file, "r", encoding="utf-8") as file:
                existing = json.load(file)

            existing_article_text, article_available = extract_article(
                article.get("url", "")
            )

            evidence = (
                existing_article_text
                or article.get("description", "")
                or ""
            )

            # Check existing JSON structure.
            validate_result({
                "main_subject": existing.get("main_subject"),
                "classification": existing.get("classification"),
                "content": existing.get("content")
            })

            # Check that the existing main subject belongs to this article.
            existing["main_subject"] = determine_main_subject(
                existing,
                article,
                evidence
            )

            # Correct the cover using the existing actual news content.
            apply_main_subject_to_cover(
                existing,
                article,
                evidence
            )

            print(f"[EXISTING] Reusing valid JSON: {news_id}")

            generated = {
                "main_subject": existing["main_subject"],
                "classification": existing["classification"],
                "content": existing["content"],
                "_article_evidence": evidence,
                "article_text_available": existing.get(
                    "article_text_available",
                    article_available
                ),
                "article_text_note": existing.get(
                    "article_text_note",
                    "Full article text extracted."
                    if article_available
                    else "Full article unavailable. Generated using RSS information only."
                )
            }

        except Exception as error:
            print(f"[REGENERATE] Existing JSON is invalid: {error}")
            generated = generate_content(client, article)

    else:
        print(f"\n[AI] Processing: {article.get('title', news_id)}")
        generated = generate_content(client, article)

    if not generated:
        print(f"[FAILED] AI generation failed: {news_id}")
        return False

    classification = generated["classification"]
    content = generated["content"]

    try:
        # Validate the subject again before saving.
        main_subject = determine_main_subject(
            generated,
            article,
            generated.get("_article_evidence", "")
        )

        generated["main_subject"] = main_subject

        # Enforce a proper news cover.
        apply_main_subject_to_cover(
            generated,
            article,
            generated.get("_article_evidence", "")
        )

        validate_result(generated)

    except Exception as error:
        print(f"[VALIDATION ERROR] {news_id}: {error}")
        return False

    classification = generated["classification"]
    content = generated["content"]
    main_subject = generated["main_subject"]

    article_evidence = " ".join([
        str(article.get("title") or ""),
        str(article.get("description") or ""),
        str(article.get("source") or ""),
        str(content.get("title") or ""),
        str(content.get("caption") or ""),
        " ".join(
            str(slide.get("text") or "")
            for slide in content.get("slides", [])
        ),
        str(generated.get("_article_evidence") or "")
    ])

    classification["country"] = resolve_country(
        classification.get("country"),
        classification.get("region"),
        article_evidence
    )

    add_country_to_image_queries(
        content,
        classification["country"]
    )

    # Never modify the cover query with country or other keywords.
    content["slides"][0]["image_search_query"] = main_subject["name"]

    classification["selected_channels"] = [
        f"{classification['category']}-news"
    ]

    output = {
        "news_id": news_id,
        "country": classification["country"],
        "main_subject": main_subject,
        "classification": classification,
        "content": content,
        "article_text_available": generated.get(
            "article_text_available",
            False
        ),
        "article_text_note": generated.get(
            "article_text_note",
            ""
        ),
        "source": article.get("source", ""),
        "source_url": article.get("url", ""),
        "published_at": article.get("published_at", "")
    }

    try:
        save_json(output_file, output)

    except Exception as error:
        print(f"[SAVE ERROR] {news_id}: {error}")
        return False

    article["category"] = classification["category"]
    article["subcategory"] = classification["subcategory"]
    article["importance_score"] = str(
        classification["importance_score"]
    )
    article["relevance_score"] = str(
        classification["relevance_score"]
    )
    article["tags"] = json.dumps(
        classification["tags"],
        ensure_ascii=False
    )
    article["selected_channels"] = json.dumps(
        classification["selected_channels"],
        ensure_ascii=False
    )

    article["country"] = classification["country"] or ""
    article["classified"] = "true"
    article["ai_done"] = "true"

    print(
        f"[MAIN SUBJECT] {main_subject['name']} "
        f"({main_subject['type']})"
    )
    print(
        f"[COVER HEADING] "
        f"{content['slides'][0]['heading']}"
    )
    print(
        f"[COVER IMAGE QUERY] "
        f"{content['slides'][0]['image_search_query']}"
    )
    print(f"[CLASSIFIED] {classification['category']}")
    print(
        f"[CHANNELS] "
        f"{', '.join(classification['selected_channels'])}"
    )
    print(f"[SLIDES] {len(content['slides'])}")
    print(f"[COUNTRY] {classification['country'] or 'null'}")
    print(f"[SUCCESS] JSON saved: {output_file.name}")

    return True


# ============================================================
# TOP 20 NEWS
# ============================================================

def keep_top_news(rows, fields):
    if not rows:
        save_queue([], fields)
        return

    def rank_score(row):
        try:
            importance = float(
                row.get("importance_score") or 0
            )
        except (ValueError, TypeError):
            importance = 0

        try:
            relevance = float(
                row.get("relevance_score") or 0
            )
        except (ValueError, TypeError):
            relevance = 0

        return (
            importance + relevance,
            importance,
            relevance
        )

    ranked = sorted(
        rows,
        key=rank_score,
        reverse=True
    )

    top_rows = ranked[:TOP_NEWS_LIMIT]

    save_queue(top_rows, fields)

    print(
        f"[TOP NEWS] Retained {len(top_rows)} best articles."
    )
    print(
        f"[TOP NEWS] Removed "
        f"{max(0, len(rows) - len(top_rows))} lower-ranked articles."
    )


# ============================================================
# MAIN
# ============================================================

def main(filename=None, reprocess=False):
    global QUEUE_FILE

    if filename:
        QUEUE_FILE = Path(filename)

    print("\n========== AI NEWS CONTENT GENERATOR ==========")
    print(f"CSV file: {QUEUE_FILE}")
    print(f"Maximum per run: {MAX_NEWS_PER_RUN}")
    print(f"Model: {MODEL_NAME}")
    print(f"Reprocess mode: {reprocess}")
    print("Engine: Local Qwen GGUF")
    print("Classification + Compact Carousel: Enabled")
    print("Main Subject Validation: Enabled")
    print("News Cover Validation: Enabled")
    print("Direct CSV Processing: Enabled")
    print("Top 20 Ranking: Enabled")
    print("===============================================\n")

    rows, fields = load_queue()

    pending = [
        row for row in rows
        if (
            reprocess
            or str(row.get("ai_done") or "").strip().lower() != "true"
        )
    ]

    batch = pending[:MAX_NEWS_PER_RUN]

    print(f"Total records: {len(rows)}")
    print(f"Pending articles: {len(pending)}")
    print(f"Maximum per run: {MAX_NEWS_PER_RUN}")

    if not batch:
        print("\n[INFO] No pending articles to process.")

        all_classified = all(
            str(row.get("ai_done") or "").strip().lower() == "true"
            for row in rows
        )

        if all_classified and len(rows) > TOP_NEWS_LIMIT:
            keep_top_news(rows, fields)
        else:
            save_queue(rows, fields)

        return

    client = get_client()

    processed = 0
    failed = 0

    for article in batch:
        if process_news(client, article):
            try:
                save_queue(rows, fields)
                processed += 1
                print(
                    f"[CSV UPDATED] {article.get('news_id')}"
                )

            except Exception as error:
                failed += 1
                print(f"[CSV SAVE ERROR] {error}")

        else:
            failed += 1

    remaining = max(0, len(pending) - len(batch))

    # Do not remove unprocessed articles.
    all_classified = all(
        str(row.get("ai_done") or "").strip().lower() == "true"
        for row in rows
    )

    if all_classified and len(rows) > TOP_NEWS_LIMIT:
        keep_top_news(rows, fields)
        print(
            "[CSV CLEANUP] CSV now contains only the top 20 articles."
        )

    else:
        save_queue(rows, fields)
        print(
            f"[CSV QUEUE] Retaining {len(rows)} articles "
            "for further processing."
        )

    print("\n========== GENERATION SUMMARY ==========")
    print(f"Total records: {len(rows)}")
    print(f"Pending articles: {len(pending)}")
    print(f"Successfully processed: {processed}")
    print(f"Failed: {failed}")
    print(f"Remaining: {remaining}")
    print(f"CSV updated: {QUEUE_FILE}")
    print("========================================\n")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    reprocess = "--reprocess" in args
    args = [
        arg for arg in args
        if arg != "--reprocess"
    ]

    main(
        args[0] if args else None,
        reprocess=reprocess
    )
