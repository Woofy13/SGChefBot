import json
import base64
import re
import time
import logging
import html
import html.parser
from datetime import date
from openai import OpenAI
from ddgs import DDGS
from config import GEMINI_API_KEY, GEMINI_MODEL, OWNER_TELEGRAM_ID
import google.genai as genai

client = OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    max_retries=0,
)
genai_client = genai.Client(api_key=GEMINI_API_KEY)

logger = logging.getLogger(__name__)

# Global daily request tracking
_daily_count = 0
_daily_date = date.today()
DAILY_LIMIT = 1500
WARN_AT = 1400

# Per-user daily request tracking
_user_daily_count = {}
_user_daily_date = date.today()
USER_DAILY_LIMIT = 1500


def check_daily_limit(user_id):
    global _user_daily_date, _user_daily_count
    today = date.today()
    if today != _user_daily_date:
        _user_daily_count = {}
        _user_daily_date = today
    if user_id == OWNER_TELEGRAM_ID:
        return
    _user_daily_count.setdefault(user_id, 0)
    _user_daily_count[user_id] += 1
    if _user_daily_count[user_id] > USER_DAILY_LIMIT:
        raise RuntimeError("You've reached your daily request limit (1,500). Try again tomorrow.")


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
    raise ValueError("No JSON array found in response")


def _gemini_call(prompt, system_msg, temperature=0.5, max_tokens=600):
    global _daily_count, _daily_date

    today = date.today()
    if today != _daily_date:
        _daily_count = 0
        _daily_date = today

    if _daily_count >= DAILY_LIMIT:
        raise RuntimeError("Gemini daily request limit reached (~1,500). Try again after midnight UTC.")

    msg = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]

    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=GEMINI_MODEL,
                messages=msg,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            _daily_count += 1
            text = resp.choices[0].message.content
            text = re.sub(r'<think>.*?(</think>|$)', '', text, flags=re.DOTALL).strip()
            return text
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Too Many Requests" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < 3:
                    time.sleep(5)
                    continue
                raise RuntimeError("Gemini daily request limit reached (~1,500). Try again after midnight UTC.")
            if "503" in err_str or "UNAVAILABLE" in err_str or "500" in err_str:
                if attempt < 3:
                    wait = 10 * (attempt + 1)
                    time.sleep(wait)
                    continue
                raise RuntimeError("Gemini is temporarily unavailable (high demand). Please try again in a moment.")
            if attempt < 3:
                time.sleep(3)
                continue
            raise
    return None


CHEF_PERSONA = (
    "You are my AI Expert Chef Assistant. Think and respond like a professional chef, "
    "not a cookbook. You analyze ingredients, techniques, and interactions like a culinary scientist. "
    "Reference modern techniques (like in The Flavor Bible, Salt Fat Acid Heat, or Modernist Cuisine). "
    "Prioritize ingredient synergy, cooking science, and accessibility. "
    "Make sure recipes are concise, without leaving out important steps."
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

def search_web(query, max_results=5, sites=None):
    try:
        search_q = "recipe " + query
        if sites:
            site_filter = " OR ".join(sites)
            search_q = f"({site_filter}) {search_q}"
        with DDGS() as ddgs:
            results = ddgs.text(search_q, max_results=max_results)
            snippets = []
            for r in results:
                snippets.append(f"From: {r.get('href', '')}\nTitle: {r.get('title', '')}\n{r.get('body', '')}")
            return "\n\n".join(snippets) if snippets else None
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
    query = re.sub(r"(suggest|recipe|make|cook|give me|i want|can i|what can)", "", preferences or items_str, flags=re.I).strip()
    # Strip metadata lines (Equipment, Diet, etc.) from search query
    query = re.sub(r'\nEquipment:.*(\n|$)', '', query, flags=re.I | re.DOTALL)
    query = re.sub(r'\nDiet:.*(\n|$)', '', query, flags=re.I | re.DOTALL)
    query = query.strip()
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
        "Return ONLY a JSON array of objects with keys: title (str), description (one line), search_query (short search for this dish). "
        'Example: [{"title":"Chicken Katsu Curry","description":"Crispy panko chicken with Japanese curry sauce","search_query":"chicken katsu curry recipe"}]'
    )

    try:
        text = _gemini_call(prompt, CHEF_PERSONA + "\n\n---\nCRITICAL: Your response must start with `[` and end with `]`. Output ONLY a raw valid JSON array — no greeting, no chef commentary, no markdown, no explanation. If you include any text outside the JSON array, the system will crash.", temperature=0.5, max_tokens=600)
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
        result = _gemini_call(prompt, CHEF_PERSONA + "\n\n---\n\nWrite detailed, practical recipes using the format provided.", temperature=0.5, max_tokens=2500)
        return result or "AI error: No response"
    except Exception as e:
        logger.exception("elaborate_recipe failed")
        return f"AI error: {e}"


def suggest_recipe(user_id, pantry_items, preferences=""):
    items_str = ", ".join(pantry_items) if pantry_items else "nothing specific"
    search_query = re.sub(r"(suggest|recipe|make|cook|give me|i want|can i)", "", preferences, flags=re.I).strip()
    search_query = re.sub(r'\nEquipment:.*(\n|$)', '', search_query, flags=re.I | re.DOTALL).strip()
    search_query = re.sub(r'\nDiet:.*(\n|$)', '', search_query, flags=re.I | re.DOTALL).strip()
    web = search_web(search_query or items_str, 3, ALL_SITES)

    prompt = (
        f"Pantry has: {items_str}.\nCommon staples: {COMMON_STAPLES}.\n"
        f"User request: {preferences}\n"
    )
    if web:
        prompt += f"\nWeb references:\n{web}\n"
    prompt += (
        "\nSuggest 1-3 realistic dishes. Use the standard format with metric measurements. "
        "Include protein (g), calories, sodium (mg). Mark [BUY] for needed ingredients. "
        "No preamble, no introduction, no chef commentary — start directly with the first dish."
    )

    try:
        return _gemini_call(prompt, CHEF_PERSONA + "\n\n---\nSuggest 1-3 realistic dishes. No preamble, no intro, no chef commentary — start directly with the first recipe.", temperature=0.5, max_tokens=1200) or "AI error"
    except Exception as e:
        logger.exception("suggest_recipe failed")
        return "AI error"


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
        text = _gemini_call(prompt, "You are a nutritionist. Respond only with JSON.", temperature=0.3, max_tokens=300)
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
        text = _gemini_call(prompt, "You are a nutritionist. Respond only with JSON.", temperature=0.3, max_tokens=200)
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
    msg = user_message.lower().strip()
    if any(w in msg for w in ["what do i have", "whats in my", "show my", "list my"]):
        msg_tokens = msg.split()
        if any(w in msg_tokens for w in ["pantry", "food", "ingredient", "ingredients", "list", "item"]):
            return {"action": "list_pantry", "items": [], "message": ""}
    if msg in ("help", "what can you do", "what can you", "commands", "/start"):
        return {"action": "help", "items": [], "message": ""}
    if msg.strip("!? ") in ("yes", "no", "ok", "okay", "sure", "thanks", "thank you"):
        return {"action": "chat", "items": [], "message": "You're welcome! Let me know if you need anything."}

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
        text = _gemini_call(prompt, "You are a kitchen assistant. Reply only in JSON.", temperature=0.1, max_tokens=200)
        if not text:
            return {"action": "chat", "items": [], "message": "Sorry, I couldn't process that. Try again."}
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
    try:
        resp = client.chat.completions.create(
            model=GEMINI_MODEL,
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
        return _gemini_call(prompt, CHEF_PERSONA + "\n\n---\n\nAnswer the user's question about the current recipe. Be helpful and specific.", temperature=0.5, max_tokens=600) or "AI error"
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
        text = _gemini_call(prompt, "You are a recipe parser. Respond only with JSON array.", temperature=0.1, max_tokens=300)
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
        return _gemini_call(prompt, "You are a helpful shopping assistant.", temperature=0.5, max_tokens=400) or "AI error"
    except Exception as e:
        logger.exception("generate_shopping_list failed")
        return "AI error"


# --- Substitution ---

def substitute_ingredient(recipe_text, substitution_text):
    prompt = (
        f"Here is the current recipe:\n{recipe_text}\n\n"
        f"The user wants to make this substitution: {substitution_text}\n\n"
        "Return the FULL updated recipe with the substitution applied. "
        "Adjust quantities, cooking times, and techniques where needed. "
        "Add a note explaining the key changes. "
        "Keep the same format as the original recipe with all sections."
    )
    try:
        return _gemini_call(prompt, CHEF_PERSONA + "\n\n---\n\nModify the recipe with the requested substitution. Return the FULL updated recipe.", temperature=0.5, max_tokens=2500) or "AI error"
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
        return _gemini_call(prompt, CHEF_PERSONA + "\n\n---\n\nScale the recipe accurately. Return the FULL updated recipe with all ingredient quantities rescaled.", temperature=0.5, max_tokens=2500) or "AI error"
    except Exception as e:
        logger.exception("scale_recipe failed")
        return "AI error"


# --- Audio Transcription ---

def transcribe_audio(audio_bytes, filename="audio.ogg"):
    try:
        mime = "audio/ogg"
        if filename.endswith(".mp3"):
            mime = "audio/mpeg"
        elif filename.endswith(".wav"):
            mime = "audio/wav"
        elif filename.endswith(".m4a"):
            mime = "audio/mp4"
        response = genai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=["Transcribe this audio exactly as spoken, including any pauses or hesitations:", genai.types.Part.from_bytes(data=audio_bytes, mime_type=mime)]
        )
        _daily_count += 1
        return response.text
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
        text = _gemini_call(prompt, CHEF_PERSONA + "\n\n---\nCRITICAL: Your response must start with `[` and end with `]`. Output ONLY a raw valid JSON array — no greeting, no chef commentary, no markdown, no explanation. If you include any text outside the JSON array, the system will crash.", temperature=0.5, max_tokens=800)
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
        return _gemini_call(prompt, CHEF_PERSONA + "\n\n---\n\nCreate a consolidated cooking plan with detailed recipes and a cooking timeline.", temperature=0.5, max_tokens=4000) or "AI error"
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
        text = _gemini_call(prompt, "You are a kitchen inventory assistant. Respond only with JSON.", temperature=0.1, max_tokens=1000)
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
        text = _gemini_call(prompt, CHEF_PERSONA + "\n\n---\nCRITICAL: Your response must start with `[` and end with `]`. Output ONLY a raw valid JSON array — no greeting, no chef commentary, no markdown, no explanation. If you include any text outside the JSON array, the system will crash.", temperature=0.5, max_tokens=600)
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
        with httpx.Client(verify=False, timeout=15, follow_redirects=True) as client:
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
        return _gemini_call(
            prompt,
            CHEF_PERSONA + "\n\n---\n\nExtract the complete recipe from the web page content. Return only the recipe in the requested format.",
            temperature=0.3, max_tokens=2500,
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

def suggest_random_ingredient(country=""):
    reality_check = (
        "CRITICAL: Only suggest ingredients that are 100% real and verifiable. "
        "Never invent or hallucinate ingredients. If you are unsure whether something exists, do not suggest it. "
        "Stick to well-known real ingredients like gochujang, sumac, yuzu kosho, harissa, black garlic, "
        "kecap manis, shrimp paste, miso, preserved lemon, za'atar, fish sauce, furikake, etc. "
    )
    if country:
        prompt = (
            f"Suggest a single random, unusual ingredient or food item from {country} cuisine. "
            f"{reality_check}"
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
            f"{reality_check}"
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
        return _gemini_call(
            prompt, system,
            temperature=0.9, max_tokens=1200,
        ) or "AI error"
    except Exception as e:
        logger.exception("suggest_random_ingredient failed")
        return "AI error"
