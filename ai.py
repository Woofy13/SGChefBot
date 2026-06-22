import json
import base64
import re
import logging
import html
import html.parser
from datetime import date
from openai import OpenAI
from ddgs import DDGS
from config import GROQ_API_KEY, GROQ_MODEL, GROQ_VISION_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, OWNER_TELEGRAM_ID

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    max_retries=0,
)

groq_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
    max_retries=0,
)

logger = logging.getLogger(__name__)

_daily_count = 0
_daily_date = date.today()
DAILY_LIMIT = 1500


def _extract_json_array(text):
    """Find and parse a JSON array from text, handling prose before/after."""
    text = text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    match = re.search(r'\[\s*\{', text, re.DOTALL)
    if match:
        start = match.start()
        end = text.rfind("]")
        if end > start:
            return json.loads(text[start:end+1])
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start:end+1])
    logger.error("No JSON array found. Raw response (first 500 chars): %s", text[:500])
    raise ValueError("No JSON array found in response")

MAX_TOKENS_CAP = 10000


def _fix_json(text):
    """Fix common JSON issues from AI output."""
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    text = re.sub(r'(?m)//.*$', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return text


def _ai_call(prompt, system_msg, temperature=0.5, max_tokens=600, return_usage=False):
    global _daily_count, _daily_date

    today = date.today()
    if today != _daily_date:
        _daily_count = 0
        _daily_date = today

    if _daily_count >= DAILY_LIMIT:
        raise RuntimeError("AI daily request limit reached (~1,500). Try again after midnight UTC.")

    max_tokens = min(max_tokens, MAX_TOKENS_CAP)

    msg = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]

    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=msg,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "Too Many Requests" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            raise RuntimeError("AI rate limit hit. Please try again in a moment.")
        if "503" in err_str or "UNAVAILABLE" in err_str or "500" in err_str:
            raise RuntimeError("AI is temporarily unavailable (high demand). Please try again in a moment.")
        raise

    _daily_count += 1
    text = resp.choices[0].message.content
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    usage = resp.usage
    cache_hit = 0
    if hasattr(usage, 'prompt_cache_hit_tokens'):
        try:
            cache_hit = int(usage.prompt_cache_hit_tokens or 0)
        except (TypeError, ValueError):
            cache_hit = 0
    elif hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
        try:
            cache_hit = int(getattr(usage.prompt_tokens_details, 'cached_tokens', 0) or 0)
        except (TypeError, ValueError):
            cache_hit = 0
    logger.info("Tokens: %d prompt (+%d cache hit) + %d completion = %d total",
                usage.prompt_tokens, cache_hit, usage.completion_tokens, usage.total_tokens)
    try:
        import database as _db
        _db.record_token_usage(0, usage.prompt_tokens, usage.completion_tokens, cache_hit, DEEPSEEK_MODEL)
    except Exception:
        pass
    if return_usage:
        return text, {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens,
                      "cache_hit": cache_hit, "total": usage.total_tokens}
    return text


def _ai_call_groq(prompt, system_msg, temperature=0.5, max_tokens=600):
    global _daily_count, _daily_date

    today = date.today()
    if today != _daily_date:
        _daily_count = 0
        _daily_date = today

    if _daily_count >= DAILY_LIMIT:
        raise RuntimeError("AI daily request limit reached (~1,500). Try again after midnight UTC.")

    msg = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=msg,
            temperature=temperature,
            max_tokens=min(max_tokens, 10000),
        )
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "Too Many Requests" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            raise RuntimeError("AI rate limit hit. Please try again in a moment.")
        if "503" in err_str or "UNAVAILABLE" in err_str or "500" in err_str:
            raise RuntimeError("AI is temporarily unavailable (high demand). Please try again in a moment.")
        raise

    _daily_count += 1
    text = resp.choices[0].message.content
    text = text.strip()
    usage = resp.usage
    cache_hit = 0
    if hasattr(usage, 'prompt_cache_hit_tokens'):
        try:
            cache_hit = int(usage.prompt_cache_hit_tokens or 0)
        except (TypeError, ValueError):
            cache_hit = 0
    logger.info("Groq Tokens: %d prompt (+%d cache hit) + %d completion = %d total",
                usage.prompt_tokens, cache_hit, usage.completion_tokens, usage.total_tokens)
    try:
        import database as _db
        _db.record_token_usage(0, usage.prompt_tokens, usage.completion_tokens, cache_hit, GROQ_MODEL)
    except Exception:
        pass
    return text


SINGAPORE_NOTE = (
    "Note: The user cooks in Singapore. "
    "Tortilla/wrap size is 20 cm max (not 30 cm). "
    "Fresh corn tortillas, dried Mexican chillies (ancho, guajillo, chipotle), "
    "tomatillos, cotija, queso fresco, and crema are generally unavailable. "
    "Substitute with local alternatives where appropriate.\n"
)

CHEF_PERSONA = (
    "You are my AI Expert Chef Assistant. Think and respond like a professional chef, "
    "not a cookbook. You analyze ingredients, techniques, and interactions like a culinary scientist. "
    "Reference modern techniques (like in The Flavor Bible, Salt Fat Acid Heat, or Modernist Cuisine). "
    "Prioritize ingredient synergy, cooking science, and accessibility. "
    "Make sure recipes are concise, without leaving out important steps. "
    + SINGAPORE_NOTE
)

CHEF_JSON_SYSTEM = (
    "You are my AI Expert Chef Assistant. Reference modern techniques "
    "(The Flavor Bible, Salt Fat Acid Heat, Modernist Cuisine). "
    "Prioritize ingredient synergy, cooking science, and accessibility.\n\n"
    "CRITICAL: Respond ONLY with a raw valid JSON array — no greetings, no prose, "
    "no markdown, no chef commentary. Your culinary expertise should be reflected "
    "in the dish choices and descriptions, not in explanatory text. "
    "If you include any text outside the JSON array, the system will crash. "
    + SINGAPORE_NOTE
)

COMMON_STAPLES = "salt, pepper, sugar, cooking oil, soy sauce, garlic, onion, ginger, eggs, rice, cooking wine, cornstarch, chilli sauce"

CUISINE_SITES = {
    "japanese": ["site:justonecookbook.com"],
    "thai": ["site:marionskitchen.com"],
    "fusion": ["site:marionskitchen.com"],
    "chinese": ["site:thewoksoflife.com", "site:madewithlau.com"],
    "singapore": ["site:themeatmen.sg"],
    "malaysian": ["site:themeatmen.sg"],
    "western": ["site:seriouseats.com", "site:americastestkitchen.com", "site:bonappetit.com", "site:allrecipes.com"],
    "american": ["site:seriouseats.com", "site:americastestkitchen.com", "site:bonappetit.com", "site:allrecipes.com"],
    "mexican": ["site:seriouseats.com", "site:americastestkitchen.com", "site:allrecipes.com"],
    "italian": ["site:seriouseats.com", "site:allrecipes.com"],
    "indian": [],
    "korean": [],
    "vietnamese": [],
}

ALL_SITES = [
    "site:justonecookbook.com",
    "site:marionskitchen.com",
    "site:thewoksoflife.com",
    "site:madewithlau.com",
    "site:themeatmen.sg",
    "site:seriouseats.com",
    "site:americastestkitchen.com",
    "site:bonappetit.com",
    "site:allrecipes.com",
]

DETAILED_FORMAT = """Format every recipe with ALL sections below. Start with the recipe title as a bold heading with an emoji.

**🍴 Recipe Title Here**

**Time Overview**
Prep: X min | Cook: X min | Total: X min | Servings: X

**💡 Why It Works**
Key techniques and science behind the recipe — extract from web references if available (e.g. Serious Eats). Explain why certain steps matter.

**🥩 Ingredients**
List each ingredient with Amount (metric) on its own line using - bullet. Do NOT add emojis next to individual ingredients.

**👨‍🍳 Step-by-Step Instructions**
Number each step. Do NOT add emojis next to individual steps or tips. Only the section heading gets an emoji.

**🍽️ Serving Suggestions**
What to serve with it. No emojis in the body text.

**📦 Storage & Make-Ahead Tips**
Fridge: X days. Freezer: X months. No emojis in body text.

**📝 Recipe Notes & Swaps**
If you don't have X, substitute with Y. No emojis in body text.

**👍 Success Tips**
Key things to get right. No emojis in body text.

IMPORTANT: Only put emojis in section headings. Never add emojis to ingredient lines, step numbers, or body text."""


# --- Web Search ---

from cachetools import TTLCache
_search_cache = TTLCache(maxsize=200, ttl=600)


def search_web(query, max_results=5, sites=None):
    cache_key = (query, max_results, tuple(sites) if sites else None)
    if cache_key in _search_cache:
        return _search_cache[cache_key]
    try:
        search_q = "recipe " + query
        if sites:
            site_filter = " OR ".join(sites)
            search_q = f"({site_filter}) {search_q}"
        with DDGS(timeout=5) as ddgs:
            results = ddgs.text(search_q, max_results=max_results)
            snippets = []
            for r in results:
                snippets.append(f"From: {r.get('href', '')}\nTitle: {r.get('title', '')}\n{r.get('body', '')}")
            result = "\n\n".join(snippets) if snippets else None
            _search_cache[cache_key] = result
            return result
    except Exception:
        logger.exception("search_web failed")
        return None


def detect_cuisine(query):
    q = query.lower()
    for cuisine, sites in CUISINE_SITES.items():
        if cuisine in q:
            return sites if sites else ALL_SITES
    return ALL_SITES


# --- Menu Generation ---

def generate_menu(pantry_items, preferences="", diversity_hint=""):
    items_str = ", ".join(pantry_items) if pantry_items else ""
    query = (preferences or items_str)
    # Strip metadata lines first (before word removal to avoid fragments)
    query = re.sub(r'(?im)^(Equipment|Diet):.*$', '', query)
    query = re.sub(r'\n+', '\n', query).strip()
    # Remove command words (with word boundaries to avoid partial matches like 'recipes' -> 's')
    query = re.sub(r"(?i)\b(?:suggest|recipe|make|cook)\b\s*|\b(?:i want|can i|give me|i can|what can)\b\s*", "", query).strip()
    sites = detect_cuisine(query)
    web = search_web(query, 5, sites)

    prompt = (
        f"User request: {preferences}\n"
        f"Pantry (just for reference, don't feel forced to use these): {items_str}\n"
        f"Common staples (always available): {COMMON_STAPLES}\n"
    )
    if web:
        prompt += f"\nWeb references:\n{web}\n"
    has_diet = "diet:" in preferences.lower()
    bias = (
        "Most suggestions should align with the user's diet preference."
        if has_diet else
        "Lean toward protein-forward dishes unless the user requests otherwise."
    )
    prompt += (
        f"\n{diversity_hint}\n"
        "Suggest exactly 5 dishes based on the request and web references. "
        "The pantry list is just FYI — you can suggest ANY dish the user might enjoy. "
        "Feel free to ignore the pantry entirely. "
        "Only suggest real, well-known dishes. "
        f"{bias} "
        "Return ONLY a JSON array of objects with keys: title (str), description (max 8 words, one line), search_query (short search for this dish). "
        'Example: [{"title":"Chicken Katsu Curry","description":"Crispy panko chicken with Japanese curry sauce","search_query":"chicken katsu curry recipe"}]'
    )

    try:
        text = _ai_call(prompt, CHEF_JSON_SYSTEM, temperature=0.5, max_tokens=4000)
        if not text:
            return None
        return _extract_json_array(text)
    except Exception as e:
        logger.exception("generate_menu failed")
        return None


def elaborate_recipe(search_query, pantry_items=None, preferences=""):
    sites = detect_cuisine(search_query + " " + preferences)
    web = search_web(search_query, 5, sites)
    equip = preferences if "equipment" in preferences.lower() else ""

    prompt = (
        f"Dish: {search_query}\n"
        f"Equipment: {equip}\n"
        f"Pantry (just for reference, don't feel forced to use these): {', '.join(pantry_items) if pantry_items else ''}\n"
        f"Common staples (always available): {COMMON_STAPLES}\n"
    )
    if web:
        prompt += f"\nReference recipes from the web:\n{web}\n"
    prompt += (
        f"\nWrite a FULL detailed recipe for this dish following this format:\n{DETAILED_FORMAT}\n\n"
        "Use metric measurements. Include estimated protein(g), calories, and sodium(mg). "
        "Make it comprehensive but practical. Use emojis appropriately.\n"
        "The pantry list is just FYI — you can suggest ANY ingredients needed for the dish. "
        "You can optionally incorporate up to about 50% of pantry items if they fit, but don't force it. "
        "IMPORTANT: Start with the dish name as a bold heading with an emoji, like: **Dish Name Here**."
    )

    try:
        result, usage = _ai_call(prompt, CHEF_PERSONA + "\n\n---\n\nWrite detailed, practical recipes using the format provided.", temperature=0.5, max_tokens=4000, return_usage=True)
        if not result:
            return "AI error: No response"
        result += f"\n\n*— {usage['total']} tokens used —*"
        return result
    except Exception as e:
        logger.exception("elaborate_recipe failed")
        return f"AI error: {e}"


def generate_recipe_by_name(recipe_name, preferences=""):
    try:
        return elaborate_recipe(recipe_name, pantry_items=None, preferences=preferences)
    except Exception:
        logger.exception("generate_recipe_by_name failed")
        return None


def suggest_recipe(pantry_items, preferences=""):
    """Suggest dishes from pantry + staples, returns JSON array or None."""
    items_str = ", ".join(pantry_items) if pantry_items else "nothing specific"
    search_query = preferences
    # Strip metadata lines first (before word removal to avoid fragments)
    search_query = re.sub(r'(?im)^(Equipment|Diet):.*$', '', search_query)
    search_query = re.sub(r'\n+', '\n', search_query).strip()
    # Remove command words (with word boundaries to avoid partial matches like 'recipes' -> 's')
    search_query = re.sub(r"(?i)\b(?:suggest|recipe|make|cook)\b\s*|\b(?:i want|can i|give me|i can|what can)\b\s*", "", search_query).strip()
    web = search_web(search_query or items_str, 3, ALL_SITES)

    prompt = (
        f"Pantry has: {items_str}.\nCommon staples: {COMMON_STAPLES}.\n"
        f"User request: {preferences}\n"
    )
    if web:
        prompt += f"\nWeb references:\n{web}\n"
    prompt += (
        "\nSuggest exactly 5 dishes the user can cook RIGHT NOW using ONLY the pantry ingredients "
        "plus common staples. If the dish needs an ingredient not in the pantry, mark it with [BUY] "
        "in the description.\n"
        "Return ONLY a JSON array of objects with keys: title (str), description (max 8 words, one line, mark needed items [BUY]), "
        "search_query (short search for this dish).\n"
        'Example: [{"title":"Chicken Soup","description":"Hearty soup [BUY:chicken]","search_query":"chicken soup recipe"}]'
    )

    try:
        text = _ai_call(prompt, CHEF_JSON_SYSTEM, temperature=0.5, max_tokens=4000)
        if not text:
            return None
        return _extract_json_array(text)
    except Exception:
        logger.exception("suggest_recipe failed")
        return None


def parse_recipe_text(text):
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
    title = lines[0].replace("**", "").replace("*", "")

    description = ""
    ingredients = []
    instructions = []
    nutrition = {}
    mode = None

    for line in lines:
        lower = line.lower()
        if "ingredient" in lower and any(line.lstrip().startswith(p) for p in ["**", "*", "-"]):
            mode = "ingredients"
            continue
        if ("instruction" in lower or "step" in lower) and any(line.lstrip().startswith(p) for p in ["**", "*", "-"]):
            mode = "instructions"
            continue
        if any(w in lower for w in ["serving suggestion", "storage", "success tips", "why it works", "recipe notes", "time overview"]):
            mode = None
            continue

        if mode == "ingredients":
            clean = line.replace("-", "").replace("*", "").strip()
            match = re.match(r"^\d+[.)]\s*", clean)
            if match:
                clean = clean[match.end():]
            if "protein" not in lower and "calorie" not in lower:
                ingredients.append(clean)
        elif mode == "instructions":
            step_match = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
            if step_match:
                step_text = step_match.group(2).replace("**", "").replace("*", "").strip()
                if step_text:
                    instructions.append(step_text)

        for key, label in [("protein", "protein_g"), ("calorie", "calories"), ("sodium", "sodium_mg")]:
            if key in lower:
                nums = re.findall(r"(\d+\.?\d*)\s*(?:g|mg|cal|kcal)?", line, re.I)
                if nums:
                    try:
                        nutrition[label] = float(nums[0])
                    except ValueError:
                        pass

    return {
        "title": title.strip(),
        "description": description or title.strip(),
        "ingredients": ingredients or ["No ingredients listed"],
        "instructions": instructions or ["No instructions"],
        "protein_g": int(nutrition.get("protein_g", 0)),
        "calories": int(nutrition.get("calories", 0)),
        "sodium_mg": int(nutrition.get("sodium_mg", 0)),
    }


# --- Nutrition ---

def get_nutrition(food_name):
    prompt = (
        f"Estimated nutrition per 100g for {food_name}. "
        "Return ONLY JSON with keys: name, calories_per_100g (kcal), protein_g, "
        "carbs_g, fat_g, sodium_mg. Use realistic averages."
    )
    try:
        text = _ai_call(prompt, "You are a nutritionist. Respond only with JSON.", temperature=0.3, max_tokens=1000)
        if not text:
            return None
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end+1]
        return json.loads(text)
    except Exception as e:
        logger.exception("get_nutrition failed")
        return None


def meal_nutrition(meal_description):
    prompt = (
        f"Estimate total calories, protein (g), and sodium (mg) for: {meal_description}. "
        "Return ONLY JSON with keys: calories, protein_g, sodium_mg."
    )
    try:
        text = _ai_call(prompt, "You are a nutritionist. Respond only with JSON.", temperature=0.3, max_tokens=200)
        if not text:
            return {"calories": 0, "protein_g": 0, "sodium_mg": 0}
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end+1]
        return json.loads(text)
    except Exception:
        logger.exception("meal_nutrition failed")
        return {"calories": 0, "protein_g": 0, "sodium_mg": 0}


# --- Natural Language Processing ---

def process_natural_language(user_message, pantry_items=None, recipes=None):
    pantry_str = ", ".join(pantry_items) if pantry_items else "empty"

    prompt = (
        f"Pantry: {pantry_str}\n"
        f'Message: "{user_message}"\n\n'
        "Respond with ONLY JSON. No markdown.\n"
        '{"action":"add"|"remove"|"clear_pantry"|"list_pantry"|"expiring"|"suggest"|"elaborate"|"canbake"|"list_recipes"|"view_recipe"|"delete_recipe"|"save_recipe"|"set_preference"|"scan_receipt"|"help"|"chat",'
        '"items":["item1","item2"],'
        '"expiry_date":"DD/MM/YY or empty",'
        '"message":"short reply"}\n\n'
        'Use "elaborate" when user picks a dish: "dish 1", "first one", "the chicken one", "tell me more"\n'
        'When action is "add" and the user mentions a date (e.g. "expires 15/7/25", "use by next Friday", "exp 15 July", "15/07/25"), extract it into expiry_date as DD/MM/YY format. For relative dates like "3 days", "1 week", "2 months", pass them through as-is. Leave empty if no date.\n'
        'Examples:\n'
        '{"action":"add","items":["chicken"],"expiry_date":"15/07/25","message":"Added chicken (exp 15/07/25)"}\n'
        '{"action":"add","items":["milk","eggs"],"expiry_date":"","message":"Added!"}\n'
        '{"action":"suggest","items":[],"message":"Here are some ideas..."}\n'
        '{"action":"elaborate","items":["chicken katsu"],"message":"Let me elaborate..."}'
    )
    try:
        text = _ai_call(prompt, "You are a kitchen assistant. Reply only in JSON.", temperature=0.1, max_tokens=200)
        if not text:
            text = ""
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end+1]
        result = json.loads(text)
        if not isinstance(result.get("items"), list):
            result["items"] = []
        return result
    except Exception:
        logger.exception("process_natural_language failed, using fallback")
        msg = user_message.lower()
        if any(w in msg for w in ["add", "put", "store", "have"]):
            return {"action": "add", "items": [], "message": "What items? Try: add chicken and rice"}
        if any(w in msg for w in ["remove", "delete", "clear", "reset"]):
            return {"action": "remove", "items": [], "message": "What to remove? Try: remove milk"}
        if any(w in msg for w in ["pantry", "what do i have", "list"]):
            return {"action": "list_pantry", "items": [], "message": ""}
        if any(w in msg for w in ["dish", "first", "second", "third", "tell me more", "elaborate", "the "])\
                and any(w in msg for w in ["1", "2", "3", "one", "two", "three", "first", "second", "third"]):
            return {"action": "elaborate", "items": msg.split(), "message": ""}
        if any(w in msg for w in ["recipe", "cook", "make", "suggest", "eat"]):
            return {"action": "suggest", "items": [], "message": ""}
        if any(w in msg for w in ["calorie", "calories", "log", "meal", "ate"]):
            return {"action": "calories", "items": [], "message": ""}
        if any(w in msg for w in ["remember", "i have a", "i have an", "my kitchen", "my equipment"]):
            return {"action": "set_preference", "items": ["equipment", user_message], "message": "Saved!"}
        if any(w in msg for w in ["create", "add", "new", "save"]) and "recipe" in msg:
            return {"action": "save_recipe", "items": msg.split(), "message": ""}
        if any(w in msg for w in ["delete", "remove", "forget"]) and any(w in msg for w in ["recipe", "saved"]):
            return {"action": "delete_recipe", "items": msg.split(), "message": ""}
        if any(w in msg for w in ["show", "view", "expand", "open"]) and any(w in msg for w in ["recipe", "list"]):
            return {"action": "list_recipes", "items": [], "message": ""}
        if any(w in msg for w in ["show", "view", "expand", "open", "read"]) and not any(w in msg for w in ["pantry", "list"]):
            return {"action": "view_recipe", "items": msg.split(), "message": ""}
        if any(w in msg for w in ["help", "what can you"]):
            return {"action": "help", "items": [], "message": ""}
        return {"action": "chat", "items": [], "message": "Hi! Try: add chicken to pantry or suggest a recipe"}


# --- Vision ---

def _vision_call(prompt_text, base64_image):
    global _daily_count
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
            ]}],
            temperature=0.3, max_tokens=500,
        )
        _daily_count += 1
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("_vision_call failed")
        return None


def recognize_food_from_image(base64_image):
    text = _vision_call(
        "Identify this food. Return ONLY JSON with keys: name, description, "
        "calories_per_100g, protein_g_per_100g, carbs_g_per_100g, "
        "fat_g_per_100g, sodium_mg_per_100g.",
        base64_image,
    )
    if not text:
        return {"error": "Could not identify food", "name": "Unknown", "description": "",
                "calories_per_100g": 0, "protein_g_per_100g": 0,
                "carbs_g_per_100g": 0, "fat_g_per_100g": 0, "sodium_mg_per_100g": 0}
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end+1]
        return json.loads(text)
    except Exception:
        return {"error": "Could not identify food", "name": "Unknown", "description": "",
                "calories_per_100g": 0, "protein_g_per_100g": 0,
                "carbs_g_per_100g": 0, "fat_g_per_100g": 0, "sodium_mg_per_100g": 0}


def scan_receipt_from_image(base64_image):
    text = _vision_call(
        "Extract the food and grocery item names from this receipt photo. "
        "Return ONLY a JSON array of strings with just the item names, e.g. "
        '["whole milk", "chicken breast", "white rice", "garlic"]. '
        "Skip non-food items (cleaning products, plastic bags, toiletries, etc.). "
        "Include only edible food and drink items. Normalize names to lower case.",
        base64_image,
    )
    if not text:
        return []
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end+1]
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except Exception:
        logger.exception("scan_receipt_from_image failed")
    return []


# --- Recipe Follow-up ---

def recipe_followup(recipe_text, user_question):
    prompt = (
        f"Here is the current recipe:\n{recipe_text}\n\n"
        f"The user asks: {user_question}\n\n"
        "Answer the user's question based on this recipe. Be helpful and specific. "
        "Reference the recipe details in your answer. Keep it concise."
    )
    try:
        return _ai_call(prompt, CHEF_PERSONA + "\n\n---\n\nAnswer the user's question about the current recipe. Be helpful and specific.", temperature=0.5, max_tokens=4000) or "AI error"
    except Exception as e:
        logger.exception("recipe_followup failed")
        return "AI error"


# --- Ingredient Parsing ---

def parse_ingredients_from_text(recipe_text):
    prompt = (
        f"Extract the ingredient names from this recipe text. Return ONLY a JSON array of strings, each string is just the ingredient name (no quantities).\n\n"
        f"Recipe:\n{recipe_text[:2000]}\n\n"
        'Example: ["chicken breast", "soy sauce", "rice", "garlic"]'
    )
    try:
        text = _ai_call(prompt, "You are a recipe parser. Respond only with JSON array.", temperature=0.1, max_tokens=300)
        if not text:
            return ["Error could not parse ingredients"]
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end+1]
        return json.loads(text)
    except Exception as e:
        logger.exception("parse_ingredients_from_text failed")
        return ["Could not parse ingredients"]


# --- Shopping List ---

def generate_shopping_list(recipe_title, missing_ingredients):
    prompt = (
        f"Shopping list for '{recipe_title}'. Missing: {', '.join(missing_ingredients)}. "
        "Suggest quantities in metric units and estimated prices in SGD. Return as bullet list."
    )
    try:
        return _ai_call(prompt, "You are a helpful shopping assistant.", temperature=0.5, max_tokens=400) or "AI error"
    except Exception as e:
        logger.exception("generate_shopping_list failed")
        return "AI error"


# --- Substitution ---

def substitute_ingredient(recipe_text, substitution_text):
    prompt = (
        f"Here is the current recipe:\n{recipe_text}\n\n"
        f"The user asks: {substitution_text}\n\n"
        "Answer concisely whether the substitution works, what differences it will make "
        "(taste, texture, cook time), and any adjustments needed. "
        "Do NOT reprint the full recipe. Just answer the question."
    )
    try:
        return _ai_call(prompt, CHEF_PERSONA + "\n\n---\n\nAnswer concisely whether the substitution works. Do NOT reprint the recipe.", temperature=0.5, max_tokens=4000) or "AI error"
    except Exception as e:
        logger.exception("substitute_ingredient failed")
        return "AI error"


# --- Scale ---

def scale_recipe(recipe_text, factor, target_servings=None):
    prompt = (
        f"Here is the current recipe:\n{recipe_text}\n\n"
        f"Scale this recipe by a factor of {factor}."
        + (f" Target: {target_servings} servings." if target_servings else "")
        + "\n\nReturn the FULL updated recipe with ALL ingredient quantities rescaled. "
        "Adjust cook times and equipment sizes if needed. "
        "Keep the same format with all sections."
    )
    try:
        return _ai_call(prompt, CHEF_PERSONA + "\n\n---\n\nScale the recipe accurately. Return the FULL updated recipe with all ingredient quantities rescaled.", temperature=0.5, max_tokens=4000) or "AI error"
    except Exception as e:
        logger.exception("scale_recipe failed")
        return "AI error"


# --- Audio Transcription ---

def transcribe_audio(audio_bytes, filename="audio.ogg"):
    global _daily_count
    try:
        mime = "audio/ogg"
        if filename.endswith(".mp3"):
            mime = "audio/mpeg"
        elif filename.endswith(".wav"):
            mime = "audio/wav"
        elif filename.endswith(".m4a"):
            mime = "audio/mp4"
        transcription = groq_client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=(filename, audio_bytes, mime),
        )
        _daily_count += 1
        return transcription.text
    except Exception as e:
        logger.exception("transcribe_audio failed")
        return None


# --- Batch Cooking ---

def generate_batch_menu(count, cuisine, pantry_items, preferences="", diversity_hint=""):
    items_str = ", ".join(pantry_items) if pantry_items else ""
    sites = detect_cuisine(cuisine)
    web = search_web(cuisine + " recipes", 5, sites)
    prompt = (
        f"Cuisine: {cuisine}\n"
        f"Pantry (just for reference, don't feel forced to use these): {items_str}\n"
        f"User request: {preferences}\n"
    )
    if web:
        prompt += f"\nWeb references:\n{web}\n"
    prompt += (
        f"\n{diversity_hint}\n"
        f"Suggest exactly {count} dishes for a {cuisine} meal. "
        "The dishes should be well-balanced with variety in flavors, textures, and cooking methods. "
        "Include a good mix of proteins, vegetables, carbs, and optionally dessert. "
        "Only suggest real, well-known dishes. "
        "Return ONLY a JSON array of objects with keys: title (str), description (one line), search_query (short search for this dish). "
        'Example: [{"title":"Chicken Katsu Curry","description":"Crispy panko chicken with Japanese curry sauce","search_query":"chicken katsu curry recipe"}]'
    )
    try:
        text = _ai_call(prompt, CHEF_JSON_SYSTEM, temperature=0.5, max_tokens=4000)
        if not text:
            return None
        return _extract_json_array(text)
    except Exception:
        logger.exception("generate_batch_menu failed")
        return None


def plan_batch(dish_titles, cuisine):
    dishes_str = "\n".join(f"- {d}" for d in dish_titles)
    prompt = (
        f"Create a consolidated cooking plan for a {cuisine} meal with these dishes:\n{dishes_str}\n\n"
        "Return a comprehensive plan with:\n"
        "1. A FULL detailed recipe for EACH dish (ingredients with metric amounts, step-by-step instructions, cook times)\n"
        "2. A combined ingredients list (grouped by category, merged quantities to avoid duplicates)\n"
        "3. A cooking timeline/timetable so everything finishes at the same time. "
        "   Show what to prep and cook at each minute mark (e.g., T-60min: x, T-30min: y, etc.).\n"
        "   Dessert can be prepared separately if included.\n"
        "4. Serving order and plating tips\n"
        "IMPORTANT FORMATTING RULES:\n"
        "- Number each dish as 1. Dish Name, 2. Dish Name, etc. Do NOT use ### or # headings.\n"
        "- Use **bold** for section headings like Ingredients, Instructions, Timeline.\n"
        "- Use emojis on section headers only.\n"
        "- Use metric measurements."
    )
    try:
        return _ai_call(prompt, CHEF_PERSONA + "\n\n---\n\nCreate a consolidated cooking plan with detailed recipes and a cooking timeline.", temperature=0.5, max_tokens=4000) or "AI error"
    except Exception as e:
        logger.exception("plan_batch failed")
        return "AI error"


# --- Pantry Sorting ---

SORT_CATEGORIES = [
    "Proteins & Prepared Meats",
    "Vegetables & Fruits",
    "Pantry Staples",
    "Sauces, Condiments & Fermented",
    "Spices, Seasonings & Mixes",
    "Other",
]


def categorize_pantry_items(items):
    items_str = "\n".join(f"- {item}" for item in items)
    cats_str = "\n".join(f"{i+1}. {c}" for i, c in enumerate(SORT_CATEGORIES))
    prompt = (
        f"Categorize each pantry item into exactly one of these categories:\n{cats_str}\n\n"
        f"Items:\n{items_str}\n\n"
        "Return ONLY a JSON object mapping each item (lowercase) to a category name from the list above. "
        'Example: {"chicken": "Proteins & Prepared Meats", "broccoli": "Vegetables & Fruits", "soy sauce": "Sauces, Condiments & Fermented"}'
    )
    try:
        text = _ai_call(prompt, "You are a kitchen inventory assistant. Respond only with JSON.", temperature=0.1, max_tokens=1000)
        if not text:
            return {}
        text = text.strip().replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start: end + 1]
        result = json.loads(text)
        if isinstance(result, dict):
            return {k.lower().strip(): v for k, v in result.items()}
        return {}
    except Exception:
        logger.exception("categorize_pantry_items failed")
        return {}


# --- Improvise ---

def generate_improvise_menu(expiring_items, pantry_items, preferences=""):
    items_str = ", ".join(pantry_items) if pantry_items else ""
    expiring_str = ", ".join(expiring_items)

    query = "recipe using " + expiring_str
    web = search_web(query, 5)

    prompt = (
        f"I have these ingredients expiring soon and MUST use at least 75% of them:\n{expiring_str}\n\n"
        f"Other available pantry items: {items_str}\n"
        f"Common staples (always available): {COMMON_STAPLES}\n"
    )
    if web:
        prompt += f"\nWeb references:\n{web}\n"
    prompt += (
        f"\nUser preferences: {preferences}\n"
        "Suggest exactly 5 dishes that use at least 75% of the expiring ingredients listed above. "
        "You can supplement with pantry items and common staples. "
        "Return ONLY a JSON array of objects with keys: title (str), description (one line), search_query (str). "
        'Example: [{"title":"Chicken Soup","description":"Hearty soup with chicken and vegetables","search_query":"chicken soup recipe"}]'
    )

    try:
        text = _ai_call(prompt, CHEF_JSON_SYSTEM, temperature=0.5, max_tokens=4000)
        if not text:
            return None
        return _extract_json_array(text)
    except Exception:
        logger.exception("generate_improvise_menu failed")
        return None


# --- Recipe Import ---

class _HTMLStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = []
    def handle_data(self, data):
        self._text.append(data)
    def get_text(self):
        return " ".join(self._text)


def _extract_jsonld_recipe(html):
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1))
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for candidate in [item] + item.get("@graph", []):
                    if not isinstance(candidate, dict):
                        continue
                    types = candidate.get("@type", [])
                    if isinstance(types, str):
                        types = [types]
                    if "Recipe" in types:
                        return candidate
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def _format_iso_duration(dur):
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?$', dur)
    if not m:
        return dur
    hrs = m.group(1)
    mins = m.group(2)
    parts = []
    if hrs: parts.append(f"{hrs} hr")
    if mins: parts.append(f"{mins} min")
    return " ".join(parts) if parts else dur


def _clean_jsonld_text(text):
    text = html.unescape(text)
    text = text.replace('\u2001', "'")
    return text.strip()


def _flatten_instructions(instructions):
    if isinstance(instructions, str):
        return [_clean_jsonld_text(instructions)]
    if not isinstance(instructions, list):
        return []
    steps = []
    for entry in instructions:
        if not isinstance(entry, dict):
            steps.append(_clean_jsonld_text(str(entry)))
            continue
        etype = entry.get("@type", "")
        if etype == "HowToSection":
            name = entry.get("name", "")
            if name:
                steps.append(f"── {_clean_jsonld_text(name)} ──")
            subs = entry.get("itemListElement", [])
            if subs:
                steps.extend(_flatten_instructions(subs))
        else:
            text = entry.get("text", "") or ""
            steps.append(_clean_jsonld_text(text))
    return steps


def _format_jsonld_recipe(recipe):
    title = _clean_jsonld_text(recipe.get("name", "Imported Recipe"))

    ingredients = recipe.get("recipeIngredient", [])
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    ingredients = [_clean_jsonld_text(i) for i in ingredients]

    steps = _flatten_instructions(recipe.get("recipeInstructions", []))

    nutrition = recipe.get("nutrition") or {}
    n_cal = _clean_jsonld_text(nutrition.get("calories", ""))
    n_protein = _clean_jsonld_text(nutrition.get("proteinContent", ""))
    n_sodium = _clean_jsonld_text(nutrition.get("sodiumContent", ""))
    total_time = recipe.get("totalTime", "")
    desc = _clean_jsonld_text(recipe.get("description", ""))

    lines = [f"**🍴 {title}**"]
    if total_time:
        lines.append(f"\n**Time Overview**\nTotal: {_format_iso_duration(total_time)}")
    if desc:
        lines.append(f"\n**💡 About**\n{desc}")
    if ingredients:
        lines.append("\n**🥩 Ingredients**")
        for ing in ingredients:
            lines.append(f"- {ing}")
    if steps:
        lines.append("\n**👨‍🍳 Step-by-Step Instructions**")
        counter = 0
        for step in steps:
            if step.startswith("── ") and step.endswith(" ──"):
                lines.append(f"\n{step}")
            else:
                counter += 1
                lines.append(f"{counter}. {step}")
    if n_cal or n_protein or n_sodium:
        parts = []
        if n_cal: parts.append(f"Calories: {n_cal}")
        if n_protein: parts.append(f"Protein: {n_protein}")
        if n_sodium: parts.append(f"Sodium: {n_sodium}")
        lines.append(f"\n**📊 Nutrition**\n{' | '.join(parts)}")

    return "\n".join(lines)


def import_recipe_from_url(url):
    try:
        import httpx
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPStatusError:
        logger.warning("import_recipe_from_url: HTTP %d for %s", resp.status_code, url)
        return None
    except Exception:
        logger.exception("import_recipe_from_url: fetch failed for %s", url)
        return None

    recipe = _extract_jsonld_recipe(html)
    if recipe:
        formatted = _format_jsonld_recipe(recipe)
        if formatted:
            return formatted

    stripper = _HTMLStripper()
    stripper.feed(html)
    text = stripper.get_text()
    prompt = (
        "Extract the complete recipe from the following webpage content. "
        "Ignore all stories, advertisements, navigation, comments, and unrelated text. "
        "Return ONLY the recipe.\n\n"
        f"Webpage content:\n{text[:15000]}\n\n"
        f"{DETAILED_FORMAT}"
    )
    try:
        return _ai_call(
            prompt,
            CHEF_PERSONA + "\n\n---\n\nExtract the complete recipe from the web page content. Return only the recipe in the requested format.",
            temperature=0.3, max_tokens=4000,
        )
    except Exception:
        logger.exception("import_recipe_from_url: LLM extraction failed")
        return None


# --- Cooking Mode ---

def parse_cook_recipe(recipe_text):
    lines = recipe_text.split("\n")
    title = ""
    ingredients = []
    steps = []
    mode = None

    for line in lines:
        clean = line.strip()
        lower = clean.lower()

        if not title and clean and not clean.startswith("**") and not clean.startswith("*"):
            candidate = clean.replace("**", "").replace("*", "").strip()
            if candidate and len(candidate) < 100:
                title = candidate

        if "ingredient" in lower and (clean.startswith("**") or clean.startswith("*")):
            mode = "ingredients"
            continue
        if ("instruction" in lower or "step" in lower) and (clean.startswith("**") or clean.startswith("*")):
            mode = "instructions"
            continue
        if "time overview" in lower or "serving suggestion" in lower or "storage" in lower or "recipe notes" in lower or "success tips" in lower:
            mode = None
            continue

        if mode == "ingredients":
            if clean and not clean.startswith("**") and not clean.startswith("*"):
                ingredients.append(clean)
        elif mode == "instructions":
            step_match = re.match(r"\s*(\d+)[.)]\s*(.*)", clean)
            if step_match:
                step_text = step_match.group(2).replace("**", "").replace("*", "").strip()
                if step_text:
                    steps.append(step_text)

    return {
        "title": title or "Recipe",
        "ingredients": ingredients,
        "steps": steps,
    }


# --- Random Ingredient ---

def suggest_random_ingredient(country="", exclude=None):
    return suggest_random_item(country, exclude, item_type="ingredient")


def suggest_random_item(country="", exclude=None, item_type="any"):
    if item_type == "any":
        import random as _random
        item_type = _random.choice(["ingredient", "equipment"])

    reality_check_ingredient = (
        "CRITICAL: Only suggest ingredients that are 100% real and verifiable. "
        "Never invent or hallucinate ingredients. If you are unsure whether something exists, do not suggest it. "
        "Stick to well-known real ingredients like gochujang, sumac, yuzu kosho, harissa, black garlic, "
        "kecap manis, shrimp paste, miso, preserved lemon, za'atar, fish sauce, furikake, etc. "
    )
    reality_check_equipment = (
        "CRITICAL: Only suggest kitchen tools/equipment that are 100% real and verifiable. "
        "Never invent or hallucinate tools. If you are unsure whether something exists, do not suggest it. "
        "Think of real tools like molcajete, suribachi, mezzaluna, mandoline, spiralizer, donabe, tagine, "
        " tajine, comal, paella pan, wok spatula, spider skimmer, potato ricer, food mill, microplane, "
        "gnocchi board, avocado peeler, cherry pitter, garlic press, mortar and pestle, tamagoyaki pan, "
        "oyakatta, takoyaki pan, taiyaki mold, bench scraper, dough whisk, lame, banetton, couche, "
        "chinois, tamis, drum sieve, cheese grater, box grater, mandoline, benriner, katsuobushi kezuriki, "
        "suribachi, usukuchi, otoshibuta, otoshibuta, donabe, kama, hangiri, sushi oke, makisu, "
        "carbon steel wok, clay pot, sand pot, tandoor, cocotte, dutch oven, tagine, etc. "
    )

    exclude_block = ""
    if exclude:
        exclude_block = "The following items have already been shown. You MUST NOT suggest any of them:\n" + \
            "\n".join(f"- {x}" for x in exclude) + \
            "\n\nPick something completely different.\n\n"
        exclude_block += (
            "If you cannot think of ANY new item that isn't in the exclude list, "
            "respond ONLY with exactly: NO_INGREDIENTS_LEFT\n\n"
        )

    if item_type == "equipment":
        if country:
            prompt = (
                f"Suggest a single random, unusual kitchen tool or food preparation equipment from {country} cuisine. "
                f"{reality_check_equipment}"
                f"{exclude_block}"
                "It should be authentic, interesting, and something a home cook might not own yet. "
                "Lean toward traditional, specialty, or craft tools — the more unique and specific, the better. "
                "Avoid basic generic items like 'knife' or 'cutting board'. "
                "Return ONLY the equipment info in exactly this format — no extra commentary:\n\n"
                "**🧰 Equipment:** name\n\n"
                "**📖 What It Is:** brief description of the tool and its origin\n\n"
                "**🍳 How to Use It:** what dishes or techniques it's best for, and why it's better than the generic alternative\n\n"
                "**💡 Pro Tip:** a practical tip, interesting fact, or what to look for when buying one\n"
            )
        else:
            prompt = (
                "Suggest a single random, unusual, quirky kitchen tool or food preparation equipment. "
                f"{reality_check_equipment}"
                f"{exclude_block}"
                "Think outside the box — traditional tools, specialty gadgets, craft utensils, "
                "and equipment from various world cuisines. Avoid basic generic items. "
                "Be creative: consider ancient tools, modern gadgets, niche specialty tools, "
                "and things most home cooks have never heard of. "
                "Return ONLY the equipment info in exactly this format — no extra commentary:\n\n"
                "**🧰 Equipment:** name\n\n"
                "**📖 What It Is:** brief description of the tool and its origin\n\n"
                "**🍳 How to Use It:** what dishes or techniques it's best for, and why it's better than the generic alternative\n\n"
                "**💡 Pro Tip:** a practical tip, interesting fact, or what to look for when buying one\n"
            )
        system = (
            CHEF_PERSONA + "\n\n---\n\nSuggest a single random, unusual kitchen tool or equipment. "
            "Be surprising and educational. Never make up tools."
            if not country else
            CHEF_PERSONA + f"\n\n---\n\nSuggest a single random, unusual kitchen tool or equipment from {country} cuisine. "
            "Be authentic, surprising, and educational. "
            "Never make up tools."
        )
    else:
        if country:
            prompt = (
                f"Suggest a single random, unusual ingredient or food item from {country} cuisine. "
                f"{reality_check_ingredient}"
                f"{exclude_block}"
                "It should be authentic, interesting, and something a home cook might not have tried before. "
                "Lean toward unique spices, fermented items, pastes, condiments, and seasonings — "
                "the more aromatic and flavourful, the better. "
                "Return ONLY the ingredient info in exactly this format — no extra commentary:\n\n"
                "**🛒 Ingredient:** name\n\n"
                "**📖 What It Is:** brief description\n\n"
                "**🍳 How to Use It:** cooking ideas and dishes it elevates\n\n"
                "**🔬 The Science Behind It:** interesting food science fact\n\n"
                "Vary the category each time — spices, pastes, fermented items, sauces, condiments, preserved things."
            )
        else:
            prompt = (
                "Suggest a single random, unusual, quirky ingredient available in Singapore. "
                f"{reality_check_ingredient}"
                f"{exclude_block}"
                "Think outside the box — unique spices, pastes, fermented items, sauces, condiments, "
                "preserved ingredients, and aromatic seasonings. Avoid basic fruits and vegetables. "
                "Return ONLY the ingredient info in exactly this format — no extra commentary:\n\n"
                "**🛒 Ingredient:** name\n\n"
                "**📖 What It Is:** brief description\n\n"
                "**🍳 How to Use It:** cooking ideas and dishes it elevates\n\n"
                "**🔬 The Science Behind It:** interesting food science fact\n\n"
                "Vary the category each time — spices, pastes, fermented items, sauces, condiments, preserved things."
            )
        system = (
            CHEF_PERSONA + "\n\n---\n\nSuggest a single random, unusual ingredient. Be surprising and educational. "
            "Never make up ingredients."
            if not country else
            CHEF_PERSONA + f"\n\n---\n\nSuggest a single random, unusual ingredient from {country} cuisine. "
            "Be authentic, surprising, and educational. "
            "Never make up ingredients."
        )
    try:
        return _ai_call(
            prompt, system,
            temperature=0.9, max_tokens=4000,
        ) or "AI error"
    except Exception as e:
        logger.exception("suggest_random_item failed")
        return "AI error"


# --- Bank Statement Parser ---


def _clean_date(d):
    if not d:
        return ""
    d = d.strip()
    parts = re.split(r'[/\-\.]', d)
    if len(parts) < 3:
        return d
    # YYYY-MM-DD
    if len(parts[0]) == 4:
        yy = parts[0][2:]
        mm = parts[1].zfill(2)
        dd = parts[2].zfill(2)
        if len(yy) == 4:
            yy = yy[2:]
        return f"{dd}/{mm}/{yy}"
    # DD/MM/YY — trust this order, pad and clean
    dd = parts[0].zfill(2)
    mm = parts[1].zfill(2)
    yy = parts[2]
    if len(yy) == 4:
        yy = yy[2:]
    elif len(yy) == 1:
        yy = "0" + yy
    return f"{dd}/{mm}/{yy}"


def parse_statement(statement_text, rulebook_text):
    system_msg = (
        "You extract transactions from bank/credit card statements.\n"
        "Output each transaction on one tab-separated line:\n"
        "Date\tMerchant\tAmount\tCategory\n\n"
        "Rules:\n"
        "- Date: YYYY-MM-DD (use 15th if day unknown)\n"
        "- Amount: positive number, no currency symbol\n"
        "- Category: e.g. Food/Eating Out, Transportation/Taxi, Health, Shopping\n"
        "- Merchant: full merchant name, proper case\n\n"
        "EVERY single charge gets its own line — never consolidate or skip.\n"
        "If same merchant appears 10 times, write 10 separate lines.\n\n"
        "After the list, on their own line:\n"
        "# UNCLEAR: merchant1, merchant2\n"
        "# DUE_DATE: YYYY-MM-DD\n\n"
        "No other text. No headers. No markdown."
    )
    prompt = (
        f"RULEBOOK:\n{rulebook_text}\n\n"
        f"---\n\n"
        f"STATEMENT:\n{statement_text}\n\n"
        f"---\n\n"
        "Extract every single transaction. Each on its own tab-separated line."
    )
    try:
        text = _ai_call_groq(prompt, system_msg, temperature=0.1, max_tokens=20000)
        logger.info("parse_statement raw (first 500): %s", (text or "")[:500])
        if not text:
            return {"transactions": [], "unclear_items": [], "due_date": ""}
        text = text.strip()
        text = text.replace("```", "").strip()

        transactions = []
        unclear_items = []
        due_date = ""

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.upper().startswith("# UNCLEAR"):
                    items = line.split(":", 1)[1] if ":" in line else ""
                    for item in items.split(","):
                        item = item.strip()
                        if item:
                            unclear_items.append(item)
                elif line.upper().startswith("# DUE_DATE"):
                    raw = line.split(":", 1)[1].strip() if ":" in line else ""
                    due_date = _clean_date(raw)
                continue

            parts = line.split("\t")
            if len(parts) < 3:
                continue
            if parts[0].lower() in ("date", "date\tmerchant"):
                continue

            date_raw = parts[0].strip()
            merchant = parts[1].strip() if len(parts) > 1 else ""
            try:
                amount = float(parts[2].replace(",", "").strip())
            except (ValueError, TypeError):
                amount = 0.0
            category_raw = parts[3].strip() if len(parts) > 3 else ""

            category = category_raw
            subcategory = ""
            if "/" in category_raw:
                cat_parts = category_raw.split("/", 1)
                category = cat_parts[0].strip()
                subcategory = cat_parts[1].strip()

            date_internal = _clean_date(date_raw)

            transactions.append({
                "date": date_internal,
                "merchant": merchant,
                "amount": amount,
                "category": category,
                "subcategory": subcategory,
                "account": "",
                "tx_type": "Expense",
                "confidence": "high",
                "notes": merchant,
            })

        parsed_merchants = {t["merchant"].lower() for t in transactions if t["merchant"]}
        unclear_items = [i for i in unclear_items if i.lower() not in parsed_merchants]

        return {
            "transactions": transactions,
            "unclear_items": unclear_items,
            "due_date": due_date,
        }
    except Exception:
        logger.exception("parse_statement failed")
        return {"transactions": [], "unclear_items": [], "due_date": "", "error": "Failed to parse statement"}

# --- Bill Input Parser ---


def parse_bill_input(user_text):
    system_msg = (
        "Parse a bill input. Users type things like \"add bill OCBC 2097 400 6 May\" or \"OCBC $400 due 6/5\".\n"
        "Return ONLY JSON: {\"card_name\": \"...\", \"card_last4\": \"...\", \"amount\": 123.45, \"due_date\": \"DD/MM/YY\"}\n"
        "If you cannot parse it, return {\"error\": \"Could not parse. Try: add bill OCBC 2097 400 6 May\"}\n"
        "card_name should be a short bank/card name (OCBC, UOB, Citi, etc.). card_last4 is the last 4 digits if provided, else empty string.\n"
        "amount is a positive number. due_date is DD/MM/YY format. Assume the current year if none given.\n"
        "Do not include any text outside the JSON object."
    )
    prompt = f'User input: "{user_text}"\n\nReturn ONLY the JSON object.'
    try:
        text = _ai_call_groq(prompt, system_msg, temperature=0.1, max_tokens=200)
        if not text:
            return {"error": "Could not parse. Try: add bill OCBC 2097 400 6 May"}
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        result = json.loads(text)
        if not isinstance(result, dict):
            return {"error": "Could not parse. Try: add bill OCBC 2097 400 6 May"}
        if "error" in result:
            return result
        for k in ("card_name", "card_last4", "amount", "due_date"):
            if k not in result:
                return {"error": "Could not parse. Try: add bill OCBC 2097 400 6 May"}
        return result
    except Exception:
        logger.exception("parse_bill_input failed")
        return {"error": "Could not parse. Try: add bill OCBC 2097 400 6 May"}


# --- Merchant Identification via Web Search ---


_COUNTRY_HINTS = {
    "japan": ["japan", "jp", "jpy", "yen", "¥", "you trip", "wise", "revolut", "tokyo", "osaka", "kyoto"],
    "thailand": ["thailand", "th", "thb", "baht", "฿", "bangkok", "phuket", "pattaya"],
    "china": ["china", "cn", "cny", "yuan", "renminbi", "beijing", "shanghai", "shenzhen", "guangzhou"],
    "korea": ["korea", "kr", "krw", "won", "₩", "seoul", "busan"],
    "malaysia": ["malaysia", "my", "myr", "ringgit", "rm", "kl", "kuala lumpur", "jb", "johor"],
    "vietnam": ["vietnam", "vn", "vnd", "dong", "₫", "hanoi", "ho chi minh", "saigon"],
    "indonesia": ["indonesia", "id", "idr", "rupiah", "rp", "jakarta", "bali"],
}


def _detect_statement_country(statement_text):
    if not statement_text:
        return ""
    low = statement_text.lower()
    # Check for multi-currency/overseas card names first — these suggest non-SG spending
    overseas_cards = ["youtrip", "wise", "revolut", "instarem", "you trip"]
    has_overseas_card = any(c in low for c in overseas_cards)
    for country, hints in _COUNTRY_HINTS.items():
        for hint in hints:
            if hint in low:
                return country
    # If no country hint but has an overseas card, return empty (generic search)
    if has_overseas_card:
        return ""
    return "singapore"  # default


def _search_merchant(merchant_name, country=""):
    snippets = None
    try:
        with DDGS(timeout=5) as ddgs:
            if country:
                query = f'"{merchant_name}" {country} merchant'
            else:
                query = f'"{merchant_name}" merchant'
            results = ddgs.text(query, max_results=4)
            snippets = []
            for r in results:
                snippets.append(f"Title: {r.get('title', '')}\n{r.get('body', '')}")
            snippets = "\n\n".join(snippets) if snippets else None
    except Exception:
        logger.exception("merchant web search failed")
        snippets = None
    return snippets or "(no results)"


def batch_identify_merchants(merchant_names, rulebook_text, full_statement_text=""):
    if not merchant_names:
        return {"identifications": [], "still_unclear": []}
    country = _detect_statement_country(full_statement_text)
    search_results = {}
    for m in merchant_names:
        m = m.strip()
        if m:
            search_results[m] = _search_merchant(m, country)
    search_block = "\n\n---\n\n".join(
        f"MERCHANT: \"{m}\"\nWeb results:\n{search_results[m]}"
        for m in merchant_names
    )
    geo_note = (
        f"The statement appears to be from {country.upper()}. " if country else
        "Merchants may be from any country. "
    )
    system_msg = (
        "You identify unknown merchants from bank/credit card statements.\n"
        "Given merchant names, web search results, and the full statement context, identify ALL merchants.\n"
        f"{geo_note}"
        "Merchants may be from Singapore, Malaysia, Japan, Thailand, China, Korea, or other countries.\n"
        "Return ONLY JSON: {\"identifications\": [{\"original_name\": \"...\", \"merchant\": \"cleaned name\", \"category\": \"...\", \"subcategory\": \"...\", \"account\": \"...\", \"tx_type\": \"Expense\"|\"Income\", \"confidence\": \"high\"|\"medium\"|\"low\", \"description\": \"one-line what it is\"}], \"still_unclear\": []}\n"
        "For confident identifications, include all fields. For truly uncertain ones, put the original_name in still_unclear.\n"
        "category is main category (Food, Transportation, Health, Shopping, etc.).\n"
        "subcategory can be: Eating Out, Groceries, Snacks, Dessert, Taxi, Medicine, Optical, etc.\n"
        "tx_type is Expense for purchases.\n"
        "Use the statement context (dates, amounts, neighboring transactions) and your knowledge of global merchants to fill in truncated names.\n"
        "Do not include any text outside the JSON object."
    )
    prompt = (
        f"Full statement text:\n{full_statement_text[:3000]}\n\n"
        f"---\n\n"
        f"Rulebook:\n{rulebook_text[:2000] if rulebook_text else '(none)'}\n\n"
        f"---\n\n"
        f"Merchants to identify:\n{search_block}\n\n"
        "Identify each merchant. Return ONLY the JSON object."
    )
    try:
        text = _ai_call_groq(prompt, system_msg, temperature=0.1, max_tokens=2000)
        if not text:
            return {"identifications": [], "still_unclear": merchant_names}
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        result = json.loads(text)
        if not isinstance(result, dict):
            return {"identifications": [], "still_unclear": merchant_names}
        return result
    except Exception:
        logger.exception("batch_identify_merchants failed")
        return {"identifications": [], "still_unclear": merchant_names}


def identify_merchant(merchant_name, rulebook_text=""):
    result = batch_identify_merchants([merchant_name], rulebook_text)
    ids = result.get("identifications", [])
    return ids[0] if ids else None


# --- Natural Language Edit Parser ---


def parse_edit_natural(user_message, current_tx, rulebook_text=""):
    system_msg = (
        "You extract structured corrections from a user's natural language response about a transaction.\n"
        "Return ONLY JSON: {\"merchant\": \"\", \"category\": \"\", \"subcategory\": \"\", \"account\": \"\", \"tx_type\": \"\", \"amount\": null, \"notes\": \"\", \"add_rule\": false}\n"
        "Only include fields the user explicitly changes. Leave unchanged fields as empty string or null.\n"
        "Set add_rule=true if the user is identifying a merchant (not just correcting a typo).\n"
        "tx_type is Expense or Income. amount should be a number or null.\n"
        'Examples:\n'
        '- "it\'s Si Si Nan Chun, Food/Eating Out" → {"merchant":"Si Si Nan Chun","category":"Food","subcategory":"Eating Out","add_rule":true}\n'
        '- "yes that\'s correct" → {"add_rule":true}\n'
        '- "amount is 12.34" → {"amount":12.34}\n'
        '- "it\'s a taxi, Transportation/Taxi" → {"category":"Transportation","subcategory":"Taxi","add_rule":false}\n'
        "Do not include any text outside the JSON object."
    )
    tx_info = (
        f"Current transaction: merchant=\"{current_tx.get('merchant','')}\", "
        f"amount={current_tx.get('amount',0)}, "
        f"category=\"{current_tx.get('category','')}\", "
        f"subcategory=\"{current_tx.get('subcategory','')}\", "
        f"account=\"{current_tx.get('account','')}\", "
        f"tx_type=\"{current_tx.get('tx_type','Expense')}\""
    )
    prompt = (
        f"{tx_info}\n\n"
        f"Rulebook:\n{rulebook_text[:1000] if rulebook_text else '(none)'}\n\n"
        f'User says: "{user_message}"\n\n'
        "Extract the corrections and return ONLY the JSON object."
    )
    try:
        text = _ai_call_groq(prompt, system_msg, temperature=0.1, max_tokens=300)
        if not text:
            return None
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        result = json.loads(text)
        if not isinstance(result, dict):
            return None
        return result
    except Exception:
        logger.exception("parse_edit_natural failed")
        return None
