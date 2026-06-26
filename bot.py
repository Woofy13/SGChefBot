import logging
logging.getLogger("duckduckgo_search").setLevel(logging.WARNING)
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)
import base64
import os
import tempfile
import re
import zipfile
import io
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from cachetools import TTLCache

import database as db
import ai
from config import OWNER_TELEGRAM_ID

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

_TWO_WEEKS = 60 * 60 * 24 * 14

_last_suggestion = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)
_last_preference = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)
_last_menu = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)
_menu_history = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)

_batch_state = TTLCache(maxsize=500, ttl=_TWO_WEEKS)
_batch_history = TTLCache(maxsize=500, ttl=_TWO_WEEKS)

_cook_state = TTLCache(maxsize=500, ttl=_TWO_WEEKS)

_pending_cooked = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)

_batch_export = TTLCache(maxsize=500, ttl=3600)

_random_country = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)
_random_item_type = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)

_photo_counts = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)
PHOTO_DAILY_LIMIT = 30
MAX_PHOTO_SIZE = 10 * 1024 * 1024

_is_saved_recipe = TTLCache(maxsize=1000, ttl=_TWO_WEEKS)

_asking_question = TTLCache(maxsize=500, ttl=3600)

_receipt_items = TTLCache(maxsize=500, ttl=3600)

_pref_cache = TTLCache(maxsize=1000, ttl=300)

_calc_state = TTLCache(maxsize=100, ttl=3600)


def _get_prefs(user_id):
    cached = _pref_cache.get(user_id)
    if cached is not None:
        return cached
    equip = db.get_user_preference(user_id, "equipment")
    diet = db.get_user_preference(user_id, "diet_profile")
    cached = {"equipment": equip, "diet": diet}
    _pref_cache[user_id] = cached
    return cached


def _check_ai_limit(user_id):
    pass


RANKS = [
    (0, "Raw Egg", "🥚"),
    (15, "Novice Chef", "🍳"),
    (30, "Line Cook", "👨‍🍳"),
    (45, "Sous Chef", "👩‍🍳"),
    (60, "Head Chef", "🍲"),
    (75, "Executive Chef", "🏆"),
    (90, "Master Chef", "🌟"),
    (105, "Iron Chef", "👑"),
    (120, "Culinary Legend", "🎖️"),
    (135, "Grill Master", "🔥"),
    (150, "Kitchen God", "🏅"),
    (165, "Michelin Star", "⭐"),
    (180, "Five-Star General", "🚀"),
    (195, "Pantry Overlord", "🗿"),
    (210, "The Food Lord", "👁️"),
    (225, "Gordon RAMSES", "🤖"),
    (240, "Quantum Cook", "🌌"),
    (255, "Infinite Sous", "♾️"),
    (270, "The Final Boss", "⚡"),
    (999999, "Just a Person Who Cooks", "🍳"),
]


def _get_rank(total):
    for threshold, title, emoji in RANKS:
        if total <= threshold:
            return f"{emoji} {title}"
    return RANKS[-1][1]  # fallback


def _get_badges(stats, dishes):
    badges = []
    total = stats.get("total", 0)
    unique = stats.get("unique_dishes", 0)
    streak = stats.get("streak", 0)
    weekend_pct = stats.get("weekend_pct", 0)
    top_dish_count = dishes[0][1] if dishes else 0

    if top_dish_count >= 5:
        badges.append("🐔 Serial Cooker")
    if unique >= 20:
        badges.append("🌍 Explorer")
    if streak >= 30:
        badges.append("🔥 Inferno")
    elif streak >= 7:
        badges.append("🔥 On Fire")
    if weekend_pct >= 70:
        badges.append("☕ Weekend Warrior")
    if total > 0 and top_dish_count / total >= 0.8:
        badges.append("🎯 One-Trick Pony")
    return badges


# Finance: statement parser state
_pending_statement = TTLCache(maxsize=500, ttl=3600)
_parse_edit = TTLCache(maxsize=500, ttl=3600)
_parse_msg = TTLCache(maxsize=500, ttl=_TWO_WEEKS)
_pending_bill = TTLCache(maxsize=500, ttl=3600)
_pending_bill_from_parse = TTLCache(maxsize=500, ttl=3600)
_merge_buffer = TTLCache(maxsize=500, ttl=3600)


def _fmt_date(iso_str):
    if not iso_str or len(iso_str) < 10:
        return iso_str or ""
    return iso_str[8:10] + "/" + iso_str[5:7] + "/" + iso_str[2:4]


_DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_MONTH_NAMES = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}


def _parse_expiry(text):
    if not text:
        return ""
    text = text.strip().lower()
    today = date.today()

    m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if month > 12:
            day, month = month, day
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    m = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+(\d{2,4}))?', text)
    if m:
        day, month_name = int(m.group(1)), m.group(2)
        month = _MONTH_NAMES.get(month_name[:3], today.month)
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    m = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:\s*,?\s*(\d{2,4}))?', text)
    if m:
        month = _MONTH_NAMES.get(m.group(1)[:3], today.month)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    m = re.search(r'next\s+(\w+)', text)
    if m:
        key = m.group(1).lower()
        if key in _DAY_NAMES:
            target = _DAY_NAMES[key]
            days_ahead = target - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return (today + timedelta(days=days_ahead)).isoformat()
        elif key == "week":
            return (today + timedelta(days=7)).isoformat()
        elif key == "month":
            return (today + timedelta(days=30)).isoformat()

    m = re.search(r'(?:in\s+)?(\d+)\s*(day|days|week|weeks|month|months)', text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("day"):
            return (today + timedelta(days=amount)).isoformat()
        elif unit.startswith("week"):
            return (today + timedelta(days=amount * 7)).isoformat()
        elif unit.startswith("month"):
            return (today + timedelta(days=amount * 30)).isoformat()

    return ""


def _render_receipt(user_id):
    data = _receipt_items.get(user_id)
    if not data or not data["items"]:
        return None, None
    lines = ["\U0001f9fe Scanned receipt \u2014 found these items:", ""]
    for i, item in enumerate(data["items"], 1):
        name = item["name"].title()
        exp = item.get("expiry", "")
        if exp:
            remaining = (date.fromisoformat(exp) - date.today()).days
            lines.append(f"{i}. {name} \u2014 exp {_fmt_date(exp)} ({remaining}d)")
        else:
            lines.append(f"{i}. {name}")
    lines.append("")
    lines.append("Reply:")
    lines.append("  'remove <name>' to remove item")
    lines.append("  'expiry <number> <date>' to set expiry")
    lines.append("  or tap \u2705 Confirm")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Confirm", callback_data="receipt_confirm")]
    ])
    return "\n".join(lines), keyboard


def _format_recipe(recipe):
    if recipe.get("full_text"):
        return recipe["full_text"]
    lines = [f"*{recipe['title']}*"]
    if recipe.get("description"):
        lines.append(f"_{recipe['description']}_")
    lines.append("")
    lines.append("*Ingredients*")
    for ing in recipe.get("ingredients", []):
        lines.append(f"• {ing}")
    lines.append("")
    lines.append("*Instructions*")
    for i, step in enumerate(recipe.get("instructions", []), 1):
        lines.append(f"{i}. {step}")
    if recipe.get("protein_g") or recipe.get("calories"):
        lines.append("")
        p = recipe.get("protein_g", 0)
        c = recipe.get("calories", 0)
        s = recipe.get("sodium_mg", 0)
        lines.append(f"⚡ {c} cal | 🥩 {p}g protein | 🧂 {s}mg sodium")
    return "\n".join(lines)


def _create_recipe_export_zip(recipes):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, recipe in enumerate(recipes, 1):
            md = _format_recipe(recipe)
            safe_name = re.sub(r'[^\w\s-]', '', recipe['title']).strip().replace(' ', '_')[:50]
            zf.writestr(f"{i:02d}_{safe_name}.md", md)
    buffer.seek(0)
    return buffer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.store_chat_id(update.effective_user.id, update.effective_chat.id)
    text = (
        "SG Chef Bot \u2014 your personal kitchen assistant\n\n"
        "Just chat naturally:\n\n"
        '  "add chicken and rice to my pantry"\n'
        '  "add chicken expiring 15/07/25" (or "add beef 3 days")\n'
        '  "whats in my pantry?"\n'
        '  "suggest fried chicken for air fryer"\n'
        '  "swap chicken for tofu" (on a recipe)\n'
        '  "make this for 2 people" (scale a recipe)\n'
        '  "log chicken rice for lunch"\n'
        '  "how many calories today?"\n'
        '  "save recipe for braised pork rice"\n\n'
        "*Pantry*\n\n"
        "  /add chicken, rice, 15/07/25     add items (with optional expiry)\n"
        "  /remove milk                     remove an item\n"
        "  /pantry                          show your pantry\n"
        "  /expiring [days]                 items expiring soon\n"
        "  /recategorize                    fix item categories (basic)\n"
        "  /sort                           AI-powered pantry sorting\n"
        "  /expiryremind on/off             toggle monthly expiry reminders\n"
        "  photo + \"receipt\" caption           scan items into pantry\n\n"
        "*Recipes & Cooking*\n\n"
        "  /suggest [preference]            AI recipe suggestions\n"
        "  /canbake                         cook with only what you have\n"
        "  /save <name>                     save a new recipe\n"
        "  /recipes                         list saved recipes\n"
        "  /view <id>                       view a saved recipe\n"
        "  /delete <id>                     delete a recipe\n"
        "  /export <name>                   export as a file\n"
        "  /import <url>                    import a recipe from a website\n"
        "  /batch [count]                   plan a multi-dish meal\n"
        "  /cookmode                        step-by-step cooking mode\n"
        "  /random <country>                generate a random, quirky ingredient\n\n"
        "*Shopping List*\n\n"
        "  /shopping <recipe>               auto-add missing ingredients\n"
        "  /shop milk, eggs                 add items to your list\n"
        "  /list                            view & check off items\n"
        "  /shopremove milk                 remove an item\n"
        "  /shopclear                       clear the entire list\n\n"
        "*Nutrition & Tracking*\n\n"
        "  /log chicken rice, lunch         log a meal\n"
        "  /daily                          today's nutrition totals\n"
        "  /remove <number>                 remove an entry from /daily\n"
        "  /nutrition chicken               lookup per 100g\n"
        "  /goal 1900 120 2300              set daily targets\n"
        "  /weekly                          weekly summary\n"
        "  food photo                       get nutrition breakdown\n"
        "  food photo + Breakfast/Lunch/    auto-log to /daily\n"
        "       Dinner/Snack caption\n\n"
        "*Settings*\n\n"
        "  /equipment air fryer, oven       save your kitchen gear\n"
        "  /addequipment air fryer          add to existing gear\n"
        "  /diet keto                       set a diet profile\n"
        "  /diet all                        reset to normal\n\n"
        "*Household Sharing*\n\n"
        "  /addmember <id>                  add a Telegram user to share pantry & shopping\n"
        "  /removemember <id>               remove a household member\n"
        "  /members                         list your household members\n\n"
        "You can also send voice messages and photos!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# --- Pantry ---

async def add_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/add chicken, rice, broccoli`\nYou can also add expiry: `/add chicken 15/07/25`")
        return
    raw = " ".join(args)
    expiry = _parse_expiry(raw)
    items = [x.strip() for x in raw.split(",") if x.strip()]
    clean_items = []
    for item in items:
        cleaned = re.sub(r'\b\d{1,2}\s*/\s*\d{1,2}(?:\s*/\s*\d{2,4})?\b', '', item).strip()
        cleaned = re.sub(r'\b(expires?|use\s+by|best\s+before|bb)[:\s]*', '', cleaned, flags=re.I).strip()
        cleaned = re.sub(r'\b(next\s+\w+)', '', cleaned, flags=re.I).strip()
        cleaned = re.sub(r'\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*', '', cleaned, flags=re.I).strip()
        cleaned = re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}', '', cleaned, flags=re.I).strip()
        cleaned = cleaned.strip(", \t")
        if cleaned:
            clean_items.append(cleaned)
    if not clean_items:
        await update.message.reply_text("Could not identify items to add. Try: `/add chicken, rice`")
        return
    db.add_pantry_items(user_id, clean_items, expiry=expiry)
    reply = f"Added {len(clean_items)} item(s): {', '.join(clean_items)}"
    if expiry:
        reply += f" (exp {_fmt_date(expiry)})"
    await update.message.reply_text(reply)


async def recategorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.recategorize_pantry(_effective_user_id(update.effective_user.id))
    await update.message.reply_text("Recategorized all pantry items.")


async def sort_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    items = db.get_pantry(user_id)
    if not items:
        await update.message.reply_text("Pantry is empty. Add items first.")
        return
    names = [item["name"] for item in items]
    msg = await update.message.reply_text("🧹 Sorting pantry with AI...")
    cat_map = ai.categorize_pantry_items(names)
    if not cat_map:
        await msg.edit_text("Could not sort pantry with AI. Try /recategorize instead.")
        return
    db.recategorize_by_map(user_id, cat_map)
    groups = db.get_pantry_grouped(user_id)
    formatted = _format_pantry_grouped(groups)
    await msg.edit_text(f"*Sorted Pantry*\n\n{formatted}", parse_mode="Markdown")


async def set_pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/set equipment air fryer, rice cooker, oven`"
        )
        return
    key = args[0].lower()
    value = " ".join(args[1:])
    db.set_user_preference(_effective_user_id(update.effective_user.id), key, value)
    await update.message.reply_text(f"Saved your {key}: {value}")


async def equipment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    if args:
        value = " ".join(args)
        db.set_user_preference(user_id, "equipment", value)
        _pref_cache.pop(user_id, None)
        await update.message.reply_text(f"Saved your equipment: {value}")
    else:
        current = db.get_user_preference(user_id, "equipment")
        if current:
            await update.message.reply_text(f"Your saved equipment: {current}")
        else:
            await update.message.reply_text(
                "No equipment saved. Use: /equipment air fryer, rice cooker, oven"
            )


async def addequipment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Example: /addequipment air fryer, rice cooker")
        return
    new_items = " ".join(context.args)
    current = db.get_user_preference(user_id, "equipment") or ""
    if current:
        merged = current.rstrip(",.; ") + ", " + new_items
    else:
        merged = new_items
    db.set_user_preference(user_id, "equipment", merged)
    _pref_cache.pop(user_id, None)
    await update.message.reply_text(f"Added! Equipment: {merged}")


async def diet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        current = db.get_user_preference(user_id, "diet_profile")
        if current:
            await update.message.reply_text(f"Your diet profile: {current}\nUse /diet all to reset.")
        else:
            await update.message.reply_text("No diet profile set. Examples:\n/diet keto\n/diet low-carb high-protein\n/diet vegetarian\n/diet all (reset)")
        return
    value = " ".join(args).strip().lower()
    if value in ("all", "reset", "none"):
        db.set_user_preference(user_id, "diet_profile", "")
        _pref_cache.pop(user_id, None)
        _last_preference.pop(user_id, None)
        await update.message.reply_text("Diet profile reset to normal.")
    else:
        db.set_user_preference(user_id, "diet_profile", value)
        _pref_cache.pop(user_id, None)
        await update.message.reply_text(f"Diet profile set to: {value}\nAll suggestions will adapt to this diet.")


async def export_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args

    if args:
        name = " ".join(args)
        recipe = db.get_recipe_by_title(user_id, name)
        if not recipe:
            recipes = db.get_recipes(user_id)
            for r in recipes:
                if any(w.lower() in r["title"].lower() for w in name.split()):
                    recipe = db.get_recipe(user_id, r["id"])
                    break
        if not recipe:
            await update.message.reply_text(f"Recipe '{name}' not found.")
            return
        text = _format_recipe(recipe)
    else:
        text = _last_suggestion.get(user_id)
        if not text:
            await update.message.reply_text("No recipe to export. Run /suggest or /view <recipe> first.")
            return

    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="recipe.md", caption="Here's your recipe!")
    finally:
        os.unlink(path)


async def remove_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/remove milk` or `/remove 1` (from /daily)", parse_mode="Markdown")
        return
    first = args[0]
    if first.isdigit():
        idx = int(first) - 1
        logs = db.get_today_logs(user_id)
        if idx < 0 or idx >= len(logs):
            await update.message.reply_text("Invalid number. Check `/daily` for the list.", parse_mode="Markdown")
            return
        removed = logs[idx]
        db.delete_meal_log(user_id, removed["id"])
        await update.message.reply_text(
            f"Removed #{first}: _{removed['meal_name']}_ ({removed['calories']} cal)",
            parse_mode="Markdown",
        )
        return
    item = " ".join(args).strip().lower()
    db.remove_pantry_item(user_id, item)
    await update.message.reply_text(f"🗑️ Removed `{item}` from pantry", parse_mode="Markdown")


def _fmt_item_line(item):
    name = item["name"].title()
    exp = item.get("expiry_date")
    if exp:
        name += f" (exp {_fmt_date(exp)})"
    return f"  - {name}"


def _format_pantry_grouped(groups):
    lines = ["Pantry", ""]
    cat_order = [
        "Proteins & Prepared Meats",
        "Vegetables & Fruits",
        "Pantry Staples",
        "Sauces, Condiments & Fermented",
        "Spices, Seasonings & Mixes",
    ]
    seen = set()
    for cat in cat_order:
        subs = {}
        for full_cat, items in groups.items():
            if full_cat.startswith(cat):
                sub = full_cat.replace(cat, "").strip(" /")
                subs.setdefault(sub, []).extend(items)
        if not subs:
            continue
        seen.add(cat)
        lines.append(f"*{cat}*")
        for sub, items in subs.items():
            if sub:
                lines.append(f"  *{sub}:*")
            for item in sorted(items, key=lambda x: x["name"]):
                lines.append(_fmt_item_line(item))
        lines.append("")

    for full_cat, items in groups.items():
        top = full_cat.split("/")[0].strip()
        if top in seen:
            continue
        seen.add(top)
        lines.append(f"*{top}*")
        for item in sorted(items, key=lambda x: x["name"]):
            lines.append(_fmt_item_line(item))
        lines.append("")

    return "\n".join(lines).strip()


async def show_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = db.get_pantry_grouped(_effective_user_id(update.effective_user.id))
    if not groups:
        await update.message.reply_text("Pantry is empty. Tell me what to add!")
        return
    await _reply_chunked(update.message, _format_pantry_grouped(groups), parse_mode="Markdown")


async def expiring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    days = int(args[0]) if args else 30
    items = db.get_expiring_items(user_id, days)
    if not items:
        await update.message.reply_text(f"✅ Nothing expiring in {days} days!")
        return
    lines = [f"⚠️ *Items expiring within {days} days:*"]
    for item in items:
        d = _fmt_date(item['expiry_date'])
        remaining = (datetime.strptime(item['expiry_date'], "%Y-%m-%d").date() - date.today()).days
        days_str = f" ({remaining} days left)" if remaining >= 0 else ""
        lines.append(f"• {item['name'].title()} — expires {d}{days_str}")
    await _reply_chunked(update.message, "\n".join(lines), parse_mode="Markdown")


async def expiry_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0].lower() in ("off", "disable", "no"):
        db.set_user_preference(user_id, "expiry_reminder", "off")
        await update.message.reply_text("Monthly expiry reminders turned off.")
    else:
        db.set_user_preference(user_id, "expiry_reminder", "")
        await update.message.reply_text("Monthly expiry reminders turned on. You'll be notified on the 1st of each month about items expiring that month.")


async def send_monthly_reminders(token):
    from telegram import Bot
    bot = Bot(token)
    today = date.today()
    users = db.get_users_for_reminder(today.year, today.month)
    for user_id, chat_id in users:
        items = db.get_expiring_items_in_month(user_id, today.year, today.month)
        if not items:
            continue
        month_name = today.strftime("%B %Y")
        lines = [f"⚠️ *Items expiring this month ({month_name}):*"]
        for item in items:
            d = _fmt_date(item['expiry_date'])
            remaining = (datetime.strptime(item['expiry_date'], "%Y-%m-%d").date() - today).days
            days_str = f" ({remaining} days left)" if remaining >= 0 else ""
            lines.append(f"• {item['name'].title()} — expires {d}{days_str}")
        try:
            await bot.send_message(chat_id=int(chat_id), text="\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to send expiry reminder to user {user_id}: {e}")


# --- Recipes ---

async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    pantry = db.get_pantry_names(user_id)
    pref = " ".join(context.args) if context.args else ""
    await _do_suggest(update, context, user_id, pantry, pref)


async def improvise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    expiring = db.get_expiring_items(user_id, 30)
    if not expiring:
        await update.message.reply_text("✅ Nothing expiring in the next month! Your pantry is fresh.")
        return
    expiring_names = [e["name"] for e in expiring]
    pantry = db.get_pantry_names(user_id)
    pref = " ".join(context.args) if context.args else ""

    msg = await update.message.reply_text("♻️ Finding recipes for your expiring items...")
    menu = ai.generate_improvise_menu(expiring_names, pantry, pref)
    _last_menu[user_id] = menu
    _last_preference[user_id] = pref

    if not menu:
        await msg.edit_text("Couldn't find recipes for those ingredients. Try adding more to your pantry.")
        return

    _menu_history[user_id] = {d["title"] for d in menu if d.get("title")}

    lines = ["♻️ *Using expiring ingredients:*"]
    for i, dish in enumerate(menu, 1):
        lines.append(f"\n{i}. {dish.get('title', '?')}")
        lines.append(f"   {dish.get('description', '')}")

    buttons = [[
        InlineKeyboardButton(str(i), callback_data=f"elaborate_{i-1}")
        for i in range(1, len(menu) + 1)
    ]]
    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _do_suggest(update, context, user_id, pantry, pref):
    _check_ai_limit(user_id)
    prefs = _get_prefs(user_id)
    equip = prefs["equipment"]
    diet = prefs["diet"]
    extra = f"Equipment: {equip}." if equip else ""
    if diet:
        extra += f" Diet: {diet}."
    prompt = f"{pref}\n{extra}".strip()

    target = update.effective_message
    msg = await target.reply_text("Thinking... (ᵕ—ᴗ—)")
    menu = ai.generate_menu(pantry, prompt)
    _last_menu[user_id] = menu
    _last_preference[user_id] = pref

    if not menu:
        await msg.edit_text("Could not generate suggestions. Try being more specific.")
        return

    _menu_history[user_id] = {d["title"] for d in menu if d.get("title")}

    lines = ["Here are some ideas ⸜(｡˃ ᵕ ˂ )⸝♡"]
    for i, dish in enumerate(menu, 1):
        lines.append(f"\n{i}. {dish.get('title', '?')}")
        lines.append(f"   {dish.get('description', '')}")

    buttons = [[
        InlineKeyboardButton("1", callback_data="elaborate_0"),
        InlineKeyboardButton("2", callback_data="elaborate_1"),
        InlineKeyboardButton("3", callback_data="elaborate_2"),
        InlineKeyboardButton("4", callback_data="elaborate_3"),
        InlineKeyboardButton("5", callback_data="elaborate_4"),
    ], [
        InlineKeyboardButton("🔄 Regenerate", callback_data="suggest_again"),
    ]]
    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


def _effective_user_id(user_id):
    primary = db.get_household_primary(user_id)
    return primary if primary is not None else user_id


def _sanitize_markdown(text):
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.lstrip()
        # Convert bullet * text to - text (markdown-safe)
        if stripped.startswith("* ") or stripped.startswith("*  "):
            indent = line[:len(line) - len(stripped)]
            result.append(indent + "- " + stripped[2:])
        else:
            result.append(line)
    return "\n".join(result)


MAX_MSG_LEN = 4000

async def _send_long_message(update, busy, text, keyboard):
    """Split long text across multiple messages, last one gets buttons."""
    target = busy if busy else (update.effective_message if update else None)
    if not target:
        return
    if len(text) <= MAX_MSG_LEN:
        if busy:
            await busy.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await target.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_MSG_LEN:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = remaining.rfind("\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = MAX_MSG_LEN
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if busy:
        await busy.edit_text(chunks[0], parse_mode="Markdown")
    else:
        await target.reply_text(chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:-1]:
        await target.reply_text(chunk, parse_mode="Markdown")
    await target.reply_text(chunks[-1], parse_mode="Markdown", reply_markup=keyboard)


async def _reply_chunked(update_or_msg, text, parse_mode=None, reply_markup=None):
    """Send text, splitting into multiple messages if it exceeds MAX_MSG_LEN."""
    if len(text) <= MAX_MSG_LEN:
        await update_or_msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_MSG_LEN:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = remaining.rfind("\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = MAX_MSG_LEN
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    for chunk in chunks[:-1]:
        await update_or_msg.reply_text(chunk, parse_mode=parse_mode)
    await update_or_msg.reply_text(chunks[-1], parse_mode=parse_mode, reply_markup=reply_markup)


async def _elaborate_dish(update, context, user_id, text, dish_index, pantry_items):
    _check_ai_limit(user_id)
    menu = _last_menu.get(user_id, [])
    if not menu or dish_index is None or dish_index >= len(menu):
        await update.effective_message.reply_text("Could not find that dish. Try /suggest first.")
        return

    dish = menu[dish_index]
    prefs = _get_prefs(user_id)
    equip = prefs["equipment"]
    diet = prefs["diet"]
    prefs_str = f"{_last_preference.get(user_id, '')}\nEquipment: {equip}.".strip()
    if diet:
        prefs_str += f" Diet: {diet}."
    busy = await update.effective_message.reply_text(f"Getting details for {dish['title']}...")
    result = ai.elaborate_recipe(dish.get("search_query", dish["title"]), pantry_items, prefs_str)
    _last_suggestion[user_id] = result
    _is_saved_recipe[user_id] = False
    _pending_cooked[user_id] = dish["title"]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
         InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
        [InlineKeyboardButton("📤 Export", callback_data="export_last"),
         InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
        [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
    ])
    sanitized = _sanitize_markdown(result)
    await _send_long_message(update, busy, sanitized, keyboard)


async def suggest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)

    if query.data == "save_last":
        text = _last_suggestion.get(user_id, "")
        if not text:
            text = _last_suggestion.get(query.from_user.id, "")
        if not text:
            await query.edit_message_text("No suggestion to save. Run /suggest first.")
            return
        parsed = ai.parse_recipe_text(text)
        if parsed and parsed["title"]:
            db.save_recipe(
                user_id, parsed["title"], parsed["description"],
                parsed["ingredients"], parsed["instructions"],
                full_text=text,
                protein_g=parsed["protein_g"], calories=parsed["calories"],
                sodium_mg=parsed["sodium_mg"],
            )
            await query.edit_message_text(
                f"✅ Saved *{parsed['title']}* to your recipes!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
                     InlineKeyboardButton("📤 Export", callback_data="export_last"),
                     InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                    [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
                ]),
            )
        else:
            await query.edit_message_text(
                "Couldn't parse the recipe. Use `/save <title>` to save manually.",
                parse_mode="Markdown",
            )
    elif query.data == "export_last":
        import tempfile
        text = _last_suggestion.get(user_id, "")
        if not text:
            text = _last_suggestion.get(query.from_user.id, "")
        if not text:
            await query.message.reply_text("No recipe to export. Run /suggest first.")
            return
        fd, path = tempfile.mkstemp(suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            with open(path, "rb") as f:
                await query.message.reply_document(document=f, filename="recipe.md", caption="Here's your recipe!")
        finally:
            try:
                os.unlink(path)
            except Exception:
                logger.exception("Failed to clean up temp file")
                pass
    elif query.data == "suggest_again":
        user_id = query.from_user.id
        seen = _menu_history.get(user_id, set())
        if len(seen) >= 50:
            _menu_history.pop(user_id, None)
            await query.edit_message_text(
                "🔄 Too many dishes have been generated. Please try again with /suggest!"
            )
            return
        prefs = _get_prefs(user_id)
        equip = prefs["equipment"]
        diet = prefs["diet"]
        pantry = db.get_pantry_names(user_id)
        prev_pref = _last_preference.get(user_id, "")
        extra = f"Equipment: {equip}." if equip else ""
        if diet:
            extra += f" Diet: {diet}."
        prompt = f"{prev_pref}\n{extra}".strip()
        await query.edit_message_text("Thinking... (ᵕ—ᴗ—)")
        seen = _menu_history.get(user_id, set())
        hint = f"Absolutely do NOT suggest any of these dishes (already suggested before): {list(seen)}" if seen else ""
        menu = ai.generate_menu(pantry, prompt, diversity_hint=hint)
        _last_menu[user_id] = menu
        if not menu:
            await query.edit_message_text("Could not generate suggestions. Try being more specific.")
            return
        _menu_history[user_id] = seen | {d["title"] for d in menu if d.get("title")}
        lines = ["Here are some ideas ⸜(｡˃ ᵕ ˂ )⸝♡"]
        for i, dish in enumerate(menu, 1):
            lines.append(f"\n{i}. {dish.get('title', '?')}")
            lines.append(f"   {dish.get('description', '')}")
        buttons = [[
            InlineKeyboardButton("1", callback_data="elaborate_0"),
            InlineKeyboardButton("2", callback_data="elaborate_1"),
            InlineKeyboardButton("3", callback_data="elaborate_2"),
            InlineKeyboardButton("4", callback_data="elaborate_3"),
            InlineKeyboardButton("5", callback_data="elaborate_4"),
        ], [
            InlineKeyboardButton("🔄 Regenerate", callback_data="suggest_again"),
        ]]
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data == "canbake_again":
        user_id = query.from_user.id
        seen = _menu_history.get(user_id, set())
        if len(seen) >= 50:
            _menu_history.pop(user_id, None)
            await query.edit_message_text(
                "🔄 Too many dishes have been generated. Please try again with /canbake!"
            )
            return
        pantry = db.get_pantry_names(user_id)
        if not pantry:
            await query.edit_message_text("Your pantry is empty! Add items first.")
            return
        prefs = _get_prefs(user_id)
        equip = prefs["equipment"]
        prev_pref = _last_preference.get(user_id, "")
        pref = f"Equipment: {equip}.\n{prev_pref}" if equip else prev_pref
        await query.edit_message_text("Thinking... (ᵕ—ᴗ—)")
        seen = _menu_history.get(user_id, set())
        hint = f"Absolutely do NOT suggest any of these dishes (already suggested before): {list(seen)}" if seen else ""
        pref_with_hint = f"{pref}\n{hint}" if hint else pref
        menu = ai.suggest_recipe(pantry, pref_with_hint)
        _last_menu[user_id] = menu
        if not menu:
            await query.edit_message_text("Couldn't find recipes for your pantry. Try adding more items.")
            return
        _menu_history[user_id] = seen | {d["title"] for d in menu if d.get("title")}
        lines = ["From your pantry:"]
        for i, dish in enumerate(menu, 1):
            lines.append(f"\n{i}. {dish.get('title', '?')}")
            lines.append(f"   {dish.get('description', '')}")
        buttons = [[
            InlineKeyboardButton(str(i), callback_data=f"elaborate_{i-1}")
            for i in range(1, len(menu) + 1)
        ], [
            InlineKeyboardButton("🔄 Regenerate", callback_data="canbake_again"),
        ]]
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def elaborate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)
    dish_index = int(query.data.split("_")[1])
    pantry = db.get_pantry_names(user_id)
    await _elaborate_dish(update, context, user_id, "", dish_index, pantry)


# --- Batch Cooking ---

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args:
        try:
            count = int(args[0])
            _batch_state[user_id] = {"count": count, "cuisine": "", "menu": [], "selected": set(), "msg_id": None}
            await update.message.reply_text(f"How many dishes (currently {count})? What cuisine?")
            return
        except ValueError:
            pass
    _batch_state[user_id] = {"count": 0, "cuisine": "", "menu": [], "selected": set(), "msg_id": None}
    await update.message.reply_text("How many dishes do you want to make?")


async def batch_handle_count(user_id, text):
    try:
        count = int(text.strip())
        if count < 1 or count > 10:
            return None
        return count
    except ValueError:
        return None


def _batch_generate_menu(user_id, keep_selected=False):
    state = _batch_state.get(user_id)
    if not state:
        return None
    pantry = db.get_pantry_names(user_id)
    count = state["count"]

    if keep_selected and state["selected"]:
        # Keep selected dishes, regenerate only unselected slots
        kept = {i: state["menu"][i] for i in state["selected"]}
        needed = count - len(kept)
        if needed <= 0:
            return state["menu"]
        seen = _batch_history.get(user_id, set())
        # Don't exclude kept dish titles from regeneration (they're already selected)
        hint = f"Absolutely do NOT suggest any of these dishes: {list(seen)}" if seen else ""
        new_menu = ai.generate_batch_menu(needed, state["cuisine"], pantry, diversity_hint=hint)
        if not new_menu:
            return None
        # Merge: keep positions for selected, fill unselected with new dishes
        result = []
        new_idx = 0
        for i in range(count):
            if i in kept:
                result.append(kept[i])
            else:
                result.append(new_menu[new_idx])
                new_idx += 1
        state["menu"] = result
        _batch_history[user_id] = seen | {d["title"] for d in new_menu if d.get("title")}
        return result
    else:
        # Fresh generation
        menu = ai.generate_batch_menu(count, state["cuisine"], pantry)
        if not menu:
            return None
        state["menu"] = menu
        state["selected"] = set()
        _batch_history[user_id] = {d["title"] for d in menu if d.get("title")}
        return menu


async def batch_show_menu(update, context, user_id):
    state = _batch_state.get(user_id)
    if not state or not state["menu"]:
        return
    menu = state["menu"]
    selected = state["selected"]
    lines = [f"Suggested dishes for {state['cuisine'].title()}:", ""]
    for i, dish in enumerate(menu, 1):
        check = "✓" if (i - 1) in selected else " "
        lines.append(f"[{check}] {i}. {dish.get('title', '?')}")
        lines.append(f"     {dish.get('description', '')}")
    lines.append(f"\nSelected: {len(selected)}/{state['count']} dishes")
    buttons = []
    row = []
    for i in range(len(menu)):
        row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"batch_toggle_{i}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("🔄 Regenerate", callback_data="batch_regenerate"),
        InlineKeyboardButton("Done", callback_data="batch_done"),
    ])
    if state.get("msg_id"):
        try:
            await context.bot.edit_message_text(
                "\n".join(lines),
                chat_id=update.effective_chat.id,
                message_id=state["msg_id"],
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            logger.exception("Failed to edit message in batch callback, sending new")
            msg = await update.effective_message.reply_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
            )
            state["msg_id"] = msg.message_id
    else:
        msg = await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
        )
        state["msg_id"] = msg.message_id


async def batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = _batch_state.get(user_id)

    if not state:
        await query.edit_message_text("Batch session expired. Start again with /batch.")
        return

    if data == "batch_regenerate":
        _batch_generate_menu(user_id, keep_selected=True)
        await batch_show_menu(update, context, user_id)

    elif data == "batch_done":
        selected = state.get("selected", set())
        if not selected:
            await query.edit_message_text("Select at least one dish before Done.")
            return
        menu = state["menu"]
        titles = [menu[i]["title"] for i in sorted(selected)]
        cuisine = state.get("cuisine", "mixed")
        await query.edit_message_text("Creating consolidated cooking plan...")
        plan = ai.plan_batch(titles, cuisine)
        # Strip markdown headings
        plan = re.sub(r"^#{1,6}\s*", "", plan, flags=re.MULTILINE)
        text = f"**Batch Cooking Plan**\n\n{plan}"
        sanitized = _sanitize_markdown(text)
        _last_suggestion[user_id] = text
        _pending_cooked[user_id] = titles
        _batch_state.pop(user_id, None)
        dish_buttons = [[InlineKeyboardButton(f"✅ {i+1}. {t[:30]}", callback_data=f"cooked_{i}")] for i, t in enumerate(titles)]
        keyboard = InlineKeyboardMarkup(
            dish_buttons + [[InlineKeyboardButton("📤 Export", callback_data="export_last")]]
        )
        msg = await query.message.reply_text("Generating batch plan...")
        await _send_long_message(None, msg, sanitized, keyboard)

    elif data.startswith("batch_toggle_"):
        idx = int(data.split("_")[2])
        if idx in state["selected"]:
            state["selected"].discard(idx)
        else:
            if len(state["selected"]) < state["count"]:
                state["selected"].add(idx)
        await batch_show_menu(update, context, user_id)


# --- Cooking Mode ---

async def cookmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _check_ai_limit(user_id)
    recipe_text = _last_suggestion.get(user_id)
    if not recipe_text:
        await update.message.reply_text("No recipe to cook. Run /suggest first, then tap a dish.")
        return

    parsed = ai.parse_cook_recipe(recipe_text)
    if not parsed["steps"]:
        await update.message.reply_text("Could not parse steps from the recipe. Try a different recipe.")
        return

    title = parsed["title"]
    ingredients = parsed["ingredients"]
    steps = parsed["steps"]

    _cook_state[user_id] = {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "shown": 0,
        "msg_id": None,
        "overflow": False,
    }

    # Show title + ingredients + first batch of steps
    lines = [f"**Cooking Mode: {title}**", "", "**Ingredients**"]
    for ing in ingredients:
        lines.append(f"- {ing}")
    lines.append("")
    lines.append(f"*{len(steps)} steps total*")

    shown_now = min(3, len(steps))
    for i in range(shown_now):
        lines.append(f"{i+1}. {steps[i]}")
    _cook_state[user_id]["shown"] = shown_now

    remaining = len(steps) - shown_now
    if remaining <= 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data="cook_done")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Step", callback_data="cook_next")],
        ])
    text = "\n".join(lines)
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    _cook_state[user_id]["msg_id"] = msg.message_id


async def cook_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = _cook_state.get(user_id)

    if not state:
        await query.edit_message_text("Cooking session expired. Run /cookmode again.")
        return

    steps = state["steps"]
    shown = state["shown"]
    remaining = len(steps) - shown
    batch = min(3, remaining)
    new_shown = shown + batch

    if state.get("overflow"):
        # Already hit the limit — send each batch as new step-only message
        step_lines = []
        for i in range(shown, new_shown):
            step_lines.append(f"{i+1}. {steps[i]}")
        state["shown"] = new_shown
        remaining = len(steps) - new_shown
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data="cook_done")],
        ]) if remaining <= 0 else InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Step", callback_data="cook_next")],
        ])
        await query.message.reply_text("\n".join(step_lines), parse_mode="Markdown", reply_markup=keyboard)
        return

    # Build accumulating message: title + ingredients + all steps so far
    lines = [f"**Cooking Mode: {state['title']}**", "", "**Ingredients**"]
    for ing in state["ingredients"]:
        lines.append(f"- {ing}")
    lines.append("")
    for i in range(new_shown):
        lines.append(f"{i+1}. {steps[i]}")

    state["shown"] = new_shown
    remaining = len(steps) - new_shown

    if remaining <= 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data="cook_done")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Step", callback_data="cook_next")],
        ])

    full_text = "\n".join(lines)
    if len(full_text) <= MAX_MSG_LEN - 200:
        await query.edit_message_text(full_text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        # Message too long — spill current batch as new step-only message
        state["overflow"] = True
        step_lines = []
        for i in range(shown, new_shown):
            step_lines.append(f"{i+1}. {steps[i]}")
        await query.message.reply_text("\n".join(step_lines), parse_mode="Markdown", reply_markup=keyboard)


async def cook_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _cook_state.pop(user_id, None)
    recipe_text = _last_suggestion.get(user_id, "")
    if recipe_text:
        saved = _is_saved_recipe.get(user_id, False)
        title = recipe_text.split("\n")[0].replace("**", "").replace("*", "").strip()
        _pending_cooked[user_id] = title
        if saved:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
                 InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
                 InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
                [InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
            ])
        sanitized = _sanitize_markdown(recipe_text)
        # Try to edit the last step message in place (no extra message)
        if len(sanitized) <= MAX_MSG_LEN:
            try:
                await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)
                return
            except Exception:
                pass
        # Fall back to new message if editing fails (too long or other error)
        await _reply_chunked(query.message, sanitized, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query.edit_message_text("Happy cooking! 🍳")


async def cook_from_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user taps Cook Mode button on an elaborated recipe."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    recipe_text = _last_suggestion.get(user_id)
    if not recipe_text:
        await query.edit_message_text("No recipe found. Run /suggest first.")
        return

    parsed = ai.parse_cook_recipe(recipe_text)
    if not parsed["steps"]:
        await query.edit_message_text("Could not parse steps. Try /cookmode.")
        return

    title = parsed["title"]
    ingredients = parsed["ingredients"]
    steps = parsed["steps"]

    _cook_state[user_id] = {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "shown": 0,
        "msg_id": None,
        "overflow": False,
    }

    lines = [f"**Cooking Mode: {title}**", "", "**Ingredients**"]
    for ing in ingredients:
        lines.append(f"- {ing}")
    lines.append("")
    lines.append(f"*{len(steps)} steps total*")

    shown_now = min(3, len(steps))
    for i in range(shown_now):
        lines.append(f"{i+1}. {steps[i]}")
    _cook_state[user_id]["shown"] = shown_now

    remaining = len(steps) - shown_now
    if remaining <= 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data="cook_done")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Step", callback_data="cook_next")],
        ])
    msg = await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    _cook_state[user_id]["msg_id"] = msg.message_id


async def save_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    args = context.args
    text = _last_suggestion.get(update.effective_user.id) or _last_suggestion.get(user_id, "")

    if text:
        parsed = ai.parse_recipe_text(text)
        if parsed and parsed["title"]:
            title = " ".join(args).strip() if args else parsed["title"]
            db.save_recipe(
                user_id, title, parsed["description"],
                parsed["ingredients"], parsed["instructions"],
                protein_g=parsed["protein_g"], calories=parsed["calories"],
                sodium_mg=parsed["sodium_mg"],
            )
            await update.message.reply_text(f"Saved {title} to your recipes!")
            return
        await update.message.reply_text("Couldnt parse the last suggestion. Try /suggest again.")
        return

    # No last suggestion — generate recipe from name
    if args:
        name = " ".join(args)
        msg = await update.message.reply_text(f"Creating recipe for {name}...")
        result = ai.generate_recipe_by_name(name)
        if result:
            parsed = ai.parse_recipe_text(result)
            if parsed and parsed["title"]:
                db.save_recipe(
                    user_id, parsed["title"], parsed["description"],
                    parsed["ingredients"], parsed["instructions"],
                    full_text=result,
                    protein_g=parsed["protein_g"], calories=parsed["calories"],
                    sodium_mg=parsed["sodium_mg"],
                )
                await msg.edit_text(f"Saved recipe: {parsed['title']}")
                return
        await msg.edit_text("Could not generate that recipe. Try being more specific.")
    else:
        await update.message.reply_text(
            "Usage: `/save <recipe name>` to save a new recipe, "
            "or run `/suggest` first then tap Save Recipe."
        )


async def list_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipes = db.get_recipes(_effective_user_id(update.effective_user.id))
    if not recipes:
        await update.message.reply_text("No saved recipes. Try `/suggest` to get some!", parse_mode="Markdown")
        return
    text, markup = _build_recipe_list(recipes)
    await _reply_chunked(update.message, text, parse_mode="Markdown", reply_markup=markup)


def _build_recipe_list(recipes):
    """Build (text, InlineKeyboardMarkup) for the recipe list with sequential numbering."""
    lines = ["📖 *Your Recipes*"]
    for i, r in enumerate(recipes, 1):
        p = f" | 🥩 {r['protein_g']}g" if r.get("protein_g") else ""
        lines.append(f"{i}. *{r['title']}*{p}")

    # View buttons: [1] [2] [3] ...
    view_row = []
    delete_row = []
    for i, r in enumerate(recipes, 1):
        view_row.append(InlineKeyboardButton(str(i), callback_data=f"viewrecipe_{r['id']}"))
        delete_row.append(InlineKeyboardButton(f"❌ {i}", callback_data=f"delrecipe_{r['id']}"))

    buttons = []
    if view_row:
        for start in range(0, len(view_row), 8):
            buttons.append(view_row[start:start+8])
    if delete_row:
        for start in range(0, len(delete_row), 8):
            buttons.append(delete_row[start:start+8])
    buttons.append([InlineKeyboardButton("📦 Batch Export", callback_data="export_batch")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def view_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/view <number>` or `/view <title>`", parse_mode="Markdown")
        return
    user_id = _effective_user_id(update.effective_user.id)
    query_str = " ".join(args)

    recipe = None
    if query_str.isdigit():
        idx = int(query_str)
        recipes = db.get_recipes(user_id)
        if 1 <= idx <= len(recipes):
            recipe = db.get_recipe(user_id, recipes[idx - 1]["id"])
    if not recipe:
        recipe = db.get_recipe_by_title(user_id, query_str)

    if not recipe:
        await update.message.reply_text("Recipe not found. Use `/recipes` to see your saved recipes.", parse_mode="Markdown")
        return

    text = _format_recipe(recipe)
    _last_suggestion[user_id] = text
    _is_saved_recipe[user_id] = True
    _pending_cooked[user_id] = recipe["title"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
         InlineKeyboardButton("📤 Export", callback_data="export_last"),
         InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
        [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
    ])
    msg = await update.message.reply_text("Loading recipe...")
    sanitized = _sanitize_markdown(text)
    await _send_long_message(None, msg, sanitized, keyboard)


async def delete_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/delete <id>`", parse_mode="Markdown")
        return
    db.delete_recipe(_effective_user_id(update.effective_user.id), int(args[0]))
    await update.message.reply_text(f"🗑️ Deleted recipe #{args[0]}")


async def export_batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)
    _batch_export[user_id] = {"step": "awaiting_input"}
    recipes = db.get_recipes(user_id)
    if not recipes:
        await query.edit_message_text("No recipes to export.")
        _batch_export.pop(user_id, None)
        return
    count = len(recipes)
    await query.edit_message_text(
        f"📦 *Batch Export*\n\nYou have {count} recipe(s). "
        "Which ones to export? Reply with:\n"
        "• Numbers like `1,2,3` or `1-4`\n"
        "• `all` for everything",
        parse_mode="Markdown",
    )


async def delete_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    recipe_id = int(query.data.split("_")[1])
    user_id = _effective_user_id(query.from_user.id)
    db.delete_recipe(user_id, recipe_id)
    # Refresh with sequential numbering
    recipes = db.get_recipes(user_id)
    if recipes:
        text, markup = _build_recipe_list(recipes)
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            busy = await query.message.reply_text("Refreshing list...")
            await _send_long_message(None, busy, text, markup)
    else:
        await query.edit_message_text("No saved recipes. Try `/suggest` to get some!", parse_mode="Markdown")


async def view_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    recipe_id = int(query.data.split("_")[1])
    user_id = _effective_user_id(query.from_user.id)
    recipe = db.get_recipe(user_id, recipe_id)
    if recipe:
        text = _format_recipe(recipe)
        _last_suggestion[user_id] = text
        _is_saved_recipe[user_id] = True
        _pending_cooked[user_id] = recipe["title"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
             InlineKeyboardButton("📤 Export", callback_data="export_last"),
             InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
            [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
        ])
        msg = await query.message.reply_text("Loading recipe...")
        sanitized = _sanitize_markdown(text)
        await _send_long_message(None, msg, sanitized, keyboard)
    else:
        await query.message.reply_text("Recipe not found.")


async def import_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    url = " ".join(context.args).strip()
    if not url:
        await update.message.reply_text("Usage: `/import <recipe-url>`\n\nImport a recipe from any cooking website.")
        return
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("Please provide a valid URL starting with http:// or https://")
        return

    msg = await update.message.reply_text("Fetching recipe...")
    result = ai.import_recipe_from_url(url)
    if not result:
        await msg.edit_text(
            "Could not extract a recipe from that URL.\n\n"
            "Some sites block automated access. Try:\n"
            "  - A different recipe site (bbcgoodfood.com, bonappetit.com work well)\n"
            "  - Paste the recipe text directly and I'll parse it"
        )
        return

    _last_suggestion[user_id] = result
    _is_saved_recipe[user_id] = False
    mid = result.replace("**", "").replace("*", "").strip()
    title_line = mid.split("\n")[0] if mid else "Imported Recipe"
    _pending_cooked[user_id] = title_line

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
         InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
        [InlineKeyboardButton("📤 Export", callback_data="export_last"),
         InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
        [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
    ])
    sanitized = _sanitize_markdown(result)
    await _send_long_message(update, msg, sanitized, keyboard)


async def cooked_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "cooked":
        title = _pending_cooked.get(user_id)
        if not title:
            await query.edit_message_text("No dish to log. Try viewing or cooking a recipe first.")
            return
        if isinstance(title, list):
            await query.edit_message_text("Use the numbered buttons to log each dish individually.")
            return
        recipe_text = _last_suggestion.get(user_id, "")
        recipe_id = None
        if _is_saved_recipe.get(user_id, False):
            found = db.get_recipe_by_title(user_id, title)
            if found:
                recipe_id = found["id"]
                if not recipe_text:
                    recipe_text = found.get("full_text") or _format_recipe(found)
        db.log_cooked(user_id, title, recipe_text=recipe_text, recipe_id=recipe_id)
        await query.edit_message_text(f"✅ Logged *{title.title()}* as cooked!", parse_mode="Markdown")
        _pending_cooked.pop(user_id, None)

    elif data.startswith("cooked_"):
        idx = int(data.split("_")[1])
        titles = _pending_cooked.get(user_id)
        if not isinstance(titles, list) or idx >= len(titles):
            await query.edit_message_text("Batch cooking data not found. Try generating the plan again.")
            return
        title = titles[idx]
        db.log_cooked(user_id, title)
        remaining = [t for i, t in enumerate(titles) if i != idx]
        if remaining:
            _pending_cooked[user_id] = remaining
            dish_buttons = [[InlineKeyboardButton(f"✅ {i+1}. {t[:30]}", callback_data=f"cooked_{i}")] for i, t in enumerate(remaining)]
            dish_buttons.append([InlineKeyboardButton("📤 Export", callback_data="export_last")])
            await query.edit_message_text(
                f"✅ Logged *{title.title()}* as cooked!\n\nLog the remaining dishes:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(dish_buttons)
            )
        else:
            _pending_cooked.pop(user_id, None)
            await query.edit_message_text("✅ All dishes logged as cooked!", parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text, keyboard = _build_stats_message(user_id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


def _build_stats_message(user_id):
    stats = db.get_cooking_stats(user_id)
    if not stats or stats.get("total", 0) == 0:
        return "📊 *Cooking Stats*\n\nNo dishes cooked yet. Start by viewing or cooking a recipe and tapping ✅ Cooked!", None

    total = stats["total"]
    unique = stats["unique_dishes"]
    most_cooked = stats["most_cooked"]
    most_cooked_count = stats["most_cooked_count"]
    streak = stats["streak"]
    month_count = stats["month_count"]
    weekend_pct = stats["weekend_pct"]
    avg_per_week = stats["avg_per_week"]
    first_date = _fmt_date(stats["first_cook_date"]) if stats.get("first_cook_date") else "N/A"

    dishes = db.get_cooked_dishes(user_id)
    rank = _get_rank(total)
    badges = _get_badges(stats, dishes)

    lines = ["📊 *Cooking Stats*", ""]
    lines.append(f"🍳 Total dishes cooked: *{total}*")
    lines.append(f"👑 Rank: *{rank}*")
    lines.append("")
    lines.append(f"🌟 Unique dishes: {unique}")
    lines.append(f"🏆 Most cooked: *{most_cooked.title()}* ({most_cooked_count}×)")
    lines.append(f"🔥 Current streak: {streak} day{'s' if streak != 1 else ''}")
    lines.append(f"📅 This month: {month_count} dishes")
    lines.append(f"📆 Avg: {avg_per_week} dishes/week")
    lines.append(f"☕ Weekend cooking: {weekend_pct}%")
    lines.append(f"🗓️ First cook: {first_date}")

    if badges:
        lines.append("")
        lines.append("*Badges*")
        lines.append(" ".join(badges))

    keyboard_buttons = []
    if dishes:
        keyboard_buttons.append([InlineKeyboardButton("📋 Dish History", callback_data="stats_dishes")])
    keyboard_buttons.append([InlineKeyboardButton("🗑️ Reset Stats", callback_data="reset_stats_start")])
    keyboard = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None
    return "\n".join(lines), keyboard


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "stats_dishes":
        dishes = db.get_cooked_dishes(user_id)
        if not dishes:
            await query.edit_message_text("No dishes cooked yet.")
            return
        lines = ["📋 *Dish History*", ""]
        entries_by_name = {}
        for e in db.get_cooking_log_entries(user_id):
            entries_by_name.setdefault(e["dish_name"], []).append(e)
        buttons_row = []
        for i, (name, cnt) in enumerate(dishes, 1):
            lines.append(f"{i}. {name.title()} ×{cnt}")
            e_list = entries_by_name.get(name, [])
            best = next((x for x in e_list if x.get("recipe_text") or x.get("recipe_id")), e_list[0] if e_list else {})
            entry_id = best.get("id", 0)
            buttons_row.append(InlineKeyboardButton(str(i), callback_data=f"viewcooked_{entry_id}"))
        text = "\n".join(lines)
        dish_rows = [buttons_row[i:i+5] for i in range(0, len(buttons_row), 5)]
        dish_rows.append([InlineKeyboardButton("🔙 Back", callback_data="stats_back")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(dish_rows))

    elif data == "stats_back":
        text, keyboard = _build_stats_message(user_id)
        if text:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "reset_stats_start":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, wipe everything", callback_data="reset_stats_confirm"),
             InlineKeyboardButton("❌ Cancel", callback_data="reset_stats_cancel")],
        ])
        await query.edit_message_text(
            "⚠️ *Are you sure?*\nThis will permanently delete all your cooking stats.\n\nType `/stats` again later to start fresh.",
            parse_mode="Markdown", reply_markup=keyboard
        )

    elif data == "reset_stats_confirm":
        db.clear_cooking_log(user_id)
        await query.edit_message_text("🗑️ All cooking stats have been wiped.")

    elif data == "reset_stats_cancel":
        text, keyboard = _build_stats_message(user_id)
        if text:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def view_cooked_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    entry_id = int(query.data.split("_")[1])
    entry = db.get_cooking_log_entry(entry_id)
    if not entry or entry["user_id"] != user_id:
        await query.edit_message_text("Entry not found.")
        return
    recipe_text = entry.get("recipe_text") or ""
    recipe_id = entry.get("recipe_id")
    if not recipe_text and recipe_id:
        recipe = db.get_recipe(user_id, recipe_id)
        if recipe:
            recipe_text = recipe.get("full_text") or _format_recipe(recipe)
    if recipe_text:
        _last_suggestion[user_id] = recipe_text
        _is_saved_recipe[user_id] = (recipe_id is not None)
        _pending_cooked[user_id] = entry["dish_name"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 Cooked!", callback_data="cooked")],
            [InlineKeyboardButton("\U0001f4be Save to Recipes", callback_data="save_last"),
             InlineKeyboardButton("\U0001f519 Back", callback_data="stats_dishes")],
            [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
        ])
        if len(recipe_text) <= MAX_MSG_LEN:
            await query.edit_message_text(recipe_text, parse_mode="Markdown", reply_markup=kb)
        else:
            await query.edit_message_text(
                f"📖 *{entry['dish_name'].title()}* recipe sent below \u2193",
                parse_mode="Markdown",
            )
            await _send_long_message(update, query.message, recipe_text, kb)
    else:
        _pending_cooked[user_id] = entry["dish_name"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 Cooked!", callback_data="cooked")],
            [InlineKeyboardButton("\U0001f519 Back", callback_data="stats_dishes")],
            [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
        ])
        await query.edit_message_text(
            f"No recipe text saved for *{entry['dish_name'].title()}*. You can still log it as cooked.",
            parse_mode="Markdown", reply_markup=kb,
        )


async def canbake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    pantry = db.get_pantry_names(user_id)
    if not pantry:
        await update.effective_message.reply_text("Your pantry is empty! Add items first.")
        return

    msg = await update.effective_message.reply_text("Checking your pantry...")
    prefs = _get_prefs(user_id)
    equip = prefs["equipment"]
    pref = f"Equipment: {equip}.\n" if equip else ""
    pref += "Suggest recipes I can cook RIGHT NOW using ONLY ingredients from this list plus common staples."
    menu = ai.suggest_recipe(pantry, pref)
    _last_menu[user_id] = menu
    _last_preference[user_id] = pref

    if not menu:
        await msg.edit_text("Couldn't find recipes for your pantry. Try adding more items.")
        return

    _menu_history[user_id] = {d["title"] for d in menu if d.get("title")}

    lines = ["From your pantry:"]
    for i, dish in enumerate(menu, 1):
        lines.append(f"\n{i}. {dish.get('title', '?')}")
        lines.append(f"   {dish.get('description', '')}")

    buttons = [[
        InlineKeyboardButton(str(i), callback_data=f"elaborate_{i-1}")
        for i in range(1, len(menu) + 1)
    ], [
        InlineKeyboardButton("🔄 Regenerate", callback_data="canbake_again"),
    ]]
    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


# --- Nutrition ---

async def log_meal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _check_ai_limit(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/log chicken rice lunch`", parse_mode="Markdown")
        return
    meal_desc = " ".join(args)

    msg = await update.message.reply_text("📊 Estimating nutrition...")
    nutrition = ai.meal_nutrition(meal_desc)

    db.log_meal(
        update.effective_user.id,
        meal_desc,
        nutrition.get("calories", 0),
        nutrition.get("protein_g", 0),
        nutrition.get("sodium_mg", 0),
    )
    await msg.edit_text(
        f"✅ Logged: _{meal_desc}_\n"
        f"⚡ {nutrition.get('calories', 0)} cal | "
        f"🥩 {nutrition.get('protein_g', 0)}g protein | "
        f"🧂 {nutrition.get('sodium_mg', 0)}mg sodium",
        parse_mode="Markdown",
    )


async def daily_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logs = db.get_today_logs(user_id)
    goals = db.get_goals(user_id)

    if not logs:
        await update.message.reply_text("No meals logged today. Use `/log` to start tracking!", parse_mode="Markdown")
        return

    total_cal = sum(l["calories"] for l in logs)
    total_protein = sum(l["protein_g"] for l in logs)
    total_sodium = sum(l["sodium_mg"] for l in logs)

    cal_pct = int(total_cal / goals["daily_calories"] * 100) if goals["daily_calories"] else 0
    pro_pct = int(total_protein / goals["daily_protein"] * 100) if goals["daily_protein"] else 0
    sod_pct = int(total_sodium / goals["daily_sodium"] * 100) if goals["daily_sodium"] else 0

    bar = "█" * (cal_pct // 10) + "░" * (10 - cal_pct // 10) if cal_pct <= 100 else "█" * 10

    lines = ["📊 *Today's Nutrition*", ""]
    for i, log in enumerate(logs, 1):
        lines.append(f"{i}. {log['meal_name']} — {log['calories']} cal, {log['protein_g']}g protein")
    lines.append("")
    lines.append(f"⚡ *{total_cal} / {goals['daily_calories']} cal* ({cal_pct}%)")
    lines.append(f"`{bar}`")
    lines.append(f"🥩 Protein: {total_protein}g / {goals['daily_protein']}g ({pro_pct}%)")
    lines.append(f"🧂 Sodium: {total_sodium}mg / {goals['daily_sodium']}mg ({sod_pct}%)")

    if total_sodium > goals["daily_sodium"]:
        lines.append("\n⚠️ *High sodium alert!* Watch your salt intake.")
    lines.append(f"\nUse `/remove <number>` to remove an entry.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def nutrition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _check_ai_limit(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/nutrition chicken breast`", parse_mode="Markdown")
        return
    food = " ".join(args)

    msg = await update.message.reply_text("🔍 Looking up nutrition...")
    info = ai.get_nutrition(food)

    if "error" in info:
        await msg.edit_text(f"Sorry, couldn't look up {food}. Try a simpler name.")
        return

    await msg.edit_text(
        f"*{food.title()}* (per 100g)\n"
        f"⚡ {info.get('calories', '?')} cal\n"
        f"🥩 Protein: {info.get('protein_g', '?')}g\n"
        f"🍚 Carbs: {info.get('carbs_g', '?')}g\n"
        f"🧈 Fat: {info.get('fat_g', '?')}g\n"
        f"🧂 Sodium: {info.get('sodium_mg', '?')}mg",
        parse_mode="Markdown",
    )


async def goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) == 3 and all(a.isdigit() for a in args):
        cal, pro, sod = int(args[0]), int(args[1]), int(args[2])
        db.set_goals(user_id, cal, pro, sod)
        await update.message.reply_text(
            f"✅ Goals updated: {cal} cal, {pro}g protein, {sod}mg sodium per day"
        )
    else:
        goals = db.get_goals(user_id)
        await update.message.reply_text(
            f"🎯 *Your Daily Goals*\n"
            f"⚡ {goals['daily_calories']} calories\n"
            f"🥩 {goals['daily_protein']}g protein\n"
            f"🧂 {goals['daily_sodium']}mg sodium\n\n"
            "To change: `/goal 1900 120 2300`",
            parse_mode="Markdown",
        )


async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logs = db.get_weekly_logs(user_id)

    if not logs:
        await update.message.reply_text("No meals logged this week. Start tracking with `/log`!", parse_mode="Markdown")
        return

    goals = db.get_goals(user_id)
    lines = ["📅 *Weekly Summary*", ""]
    total_cal = total_protein = total_sodium = 0

    for day in logs:
        d = datetime.strptime(day["logged_date"], "%Y-%m-%d")
        day_name = d.strftime("%a %d/%m")
        lines.append(
            f"• {day_name}: {day['total_cal']} cal, "
            f"{day['total_protein']}g protein, {day['total_sodium']}mg sodium"
        )
        total_cal += day["total_cal"]
        total_protein += day["total_protein"]
        total_sodium += day["total_sodium"]

    days_logged = len(logs)
    avg_cal = total_cal // days_logged
    avg_pro = total_protein // days_logged
    avg_sod = total_sodium // days_logged

    lines.append("")
    lines.append(f"📊 *Averages*")
    lines.append(f"⚡ {avg_cal} cal/day (target: {goals['daily_calories']})")
    lines.append(f"🥩 {avg_pro}g protein/day (target: {goals['daily_protein']}g)")
    lines.append(f"🧂 {avg_sod}mg sodium/day (target: {goals['daily_sodium']}mg)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Shopping ---

async def shopping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    args = context.args

    if args and args[0].isdigit():
        idx = int(args[0])
        recipes = db.get_recipes(user_id)
        if 1 <= idx <= len(recipes):
            recipe = db.get_recipe(user_id, recipes[idx - 1]["id"])
            ingredients = recipe["ingredients"]
            title = recipe["title"]
        else:
            await update.message.reply_text(f"Recipe #{idx} not found. Use `/recipes` to see your saved recipes.", parse_mode="Markdown")
            return
    elif args:
        title = " ".join(args)
        recipe = db.get_recipe_by_title(user_id, title)
        if not recipe:
            await update.message.reply_text(f"Recipe '{title}' not found. Use `/recipes` to see saved recipes.", parse_mode="Markdown")
            return
        ingredients = recipe["ingredients"]
        title = recipe["title"]
    else:
        last_text = _last_suggestion.get(user_id, "")
        if not last_text:
            await update.message.reply_text("No recipe to shop for. Use `/shopping <number>` or get a suggestion first.", parse_mode="Markdown")
            return
        await update.message.reply_text("🛒 Extracting ingredients from last suggestion...")
        ingredients = ai.parse_ingredients_from_text(last_text)
        title = "last suggested recipe"
        if isinstance(ingredients, str):
            await update.message.reply_text(ingredients)
            return

    pantry_names = set(db.get_pantry_names(user_id))
    missing = []
    for ing in ingredients:
        ing_key = ing.split(",")[0].split("(")[0].strip().lower()
        if not any(p in ing_key for p in pantry_names):
            missing.append(ing)

    if not missing:
        await update.message.reply_text("✅ You have all the ingredients! Time to cook.")
        return

    db.add_to_shopping_list_batch(user_id, [item.split(",")[0].split("(")[0].strip() for item in missing])

    msg = await update.message.reply_text("🛒 Generating shopping list...")
    result = ai.generate_shopping_list(title, missing)
    shop = db.get_shopping_list(user_id)
    shop_count = len([s for s in shop if not s["checked"]])
    await msg.edit_text(
        f"🛒 *Shopping List for {title}*\n{result}\n\n"
        f"📋 {shop_count} item(s) in your shopping list. Use `/list` to view.",
        parse_mode="Markdown",
    )


async def shop_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/shop milk, eggs, chicken`")
        return
    items = [x.strip().lower() for x in " ".join(args).split(",") if x.strip()]
    db.add_to_shopping_list_batch(user_id, items)
    await update.message.reply_text(f"📋 Added {len(items)} item(s) to your shopping list: {', '.join(items)}\nUse `/list` to view.")


async def list_shopping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    text, keyboard = _render_shopping_list(user_id)
    if not text:
        await update.message.reply_text("📋 Shopping list is empty. Use `/shop milk, eggs` to add items.")
        return
    await _reply_chunked(update.message, text, parse_mode="Markdown", reply_markup=keyboard)


async def shop_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/shopremove milk`")
        return
    item = " ".join(args).strip().lower()
    db.remove_from_shopping_list(user_id, item)
    await update.message.reply_text(f"🗑 Removed `{item}` from your shopping list.", parse_mode="Markdown")


async def shop_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    db.clear_shopping_list(user_id)
    await update.message.reply_text("🗑 Shopping list cleared.")


# --- Household Sharing ---


async def addmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/addmember <telegram_id>`\n\nTell your household member to forward a message from the bot to you, or share their Telegram ID.")
        return
    try:
        member_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID. Provide a numeric Telegram user ID.")
        return
    if member_id == user_id:
        await update.message.reply_text("You can't add yourself as a member.")
        return
    db.add_household_member(user_id, member_id)
    await update.message.reply_text(f"✅ Added Telegram ID `{member_id}` to your household. They can now use the shared shopping list and pantry.", parse_mode="Markdown")


async def removemember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/removemember <telegram_id>`")
        return
    try:
        member_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID. Provide a numeric Telegram user ID.")
        return
    db.remove_household_member(user_id, member_id)
    await update.message.reply_text(f"🗑 Removed Telegram ID `{member_id}` from your household.", parse_mode="Markdown")


async def members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db.get_household_members(user_id)
    if not rows:
        await update.message.reply_text("No household members. Use `/addmember <telegram_id>` to add one.")
        return
    lines = ["👨‍👩‍👧‍👦 *Household Members*"]
    for r in rows:
        lines.append(f"• `{r['member_user_id']}` (added {r['added_date']})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Natural language handler ---

async def _handle_user_text(update, context, user_id, text):
    """Core text processing logic used by both handle_text and handle_voice."""
    raw_user_id = user_id  # keep original for personal-only state
    user_id = _effective_user_id(user_id)

    t_lower = text.lower().strip()

    if t_lower == "pantry" or any(w in t_lower for w in ["what do i have", "show pantry", "list pantry", "my pantry", "whats in my pantry", "what's in my pantry"]):
        groups = db.get_pantry_grouped(user_id)
        if not groups:
            await update.effective_message.reply_text("Your pantry is empty. Tell me what to add!")
            return
        await _send_long_message(update, None, _format_pantry_grouped(groups), None)
        return

    if t_lower in ("recipes", "recipe") or (any(w in t_lower for w in ["show recipes", "list recipes", "my recipes", "saved recipes"]) and "recipe" in t_lower):
        await list_recipes(update, context)
        return

    if any(w in t_lower for w in ["expiring", "expire soon", "going bad", "expiring soon"]):
        await expiring(update, context)
        return

    if any(w in t_lower for w in ["clear pantry", "clear my pantry", "empty pantry", "reset pantry"]):
        eff_id = _effective_user_id(user_id)
        for item in db.get_pantry_names(eff_id):
            db.remove_pantry_item(eff_id, item)
        await update.effective_message.reply_text("Pantry cleared!")
        return

    if t_lower in ("help", "menu", "commands"):
        await start(update, context)
        return

    if any(w in t_lower for w in ["shopping list", "show shopping", "my shopping", "view shopping"]):
        await list_shopping(update, context)
        return

    pantry = None
    recipes = None

    # Check batch state first (before dish selection, to avoid conflicts)
    state = _batch_state.get(raw_user_id)
    if state and state["count"] == 0:
        count = await batch_handle_count(raw_user_id, text)
        if count:
            state["count"] = count
            await update.effective_message.reply_text(f"Great! {count} dishes. What cuisine?")
        else:
            await update.effective_message.reply_text("Please enter a number (1-10).")
        return

    if state and state["count"] > 0 and not state["cuisine"]:
        state["cuisine"] = text.strip()
        await update.effective_message.reply_text(f"{state['cuisine'].title()} sounds good! Generating suggestions...")
        menu = _batch_generate_menu(raw_user_id)
        if menu:
            await batch_show_menu(update, context, raw_user_id)
        else:
            await update.effective_message.reply_text("Could not generate suggestions. Try a different cuisine.")
            state["cuisine"] = ""
        return

    # Batch export: user is replying with recipe numbers
    export_state = _batch_export.get(raw_user_id)
    if export_state and export_state.get("step") == "awaiting_input":
        _batch_export.pop(raw_user_id, None)
        user_recipes = db.get_recipes(user_id)
        if not user_recipes:
            await update.effective_message.reply_text("No recipes to export.")
            return
        if t_lower == "all":
            selected = user_recipes
        else:
            indices = set()
            # Parse "1,2,3" or "1-4" or "1 2 3"
            for part in re.split(r'[,\s]+', t_lower):
                part = part.strip()
                if not part:
                    continue
                if '-' in part:
                    try:
                        a, b = part.split('-', 1)
                        start, end = int(a.strip()), int(b.strip())
                        indices.update(range(start, end + 1))
                    except ValueError:
                        pass
                else:
                    try:
                        indices.add(int(part))
                    except ValueError:
                        pass
            selected = []
            for i in sorted(indices):
                if 1 <= i <= len(user_recipes):
                    selected.append(db.get_recipe(user_id, user_recipes[i - 1]["id"]))
            if not selected:
                await update.effective_message.reply_text(
                    "No valid recipe numbers found. Use numbers like `1,2,3`, `1-4`, or `all`.",
                    parse_mode="Markdown",
                )
                return
        # Create zip
        try:
            zip_buffer = _create_recipe_export_zip(selected)
            caption = f"📦 Exported {len(selected)} recipe(s)"
            await update.effective_message.reply_document(
                document=zip_buffer,
                filename="RecipeExport.zip",
                caption=caption,
            )
        except Exception as e:
            logger.exception("Batch export failed")
            await update.effective_message.reply_text(f"Export failed: {e}")
        return

    # Pre-check: dish selection (dish 1, first one, etc.) — bypass AI
    t = text.lower()
    _dish_patterns = {
        "1": 0, "one": 0, "first": 0,
        "2": 1, "two": 1, "second": 1,
        "3": 2, "three": 2, "third": 2,
        "4": 3, "four": 3, "fourth": 3,
        "5": 4, "five": 4, "fifth": 4,
    }
    dish_index = None
    for word in t.split():
        if word in _dish_patterns:
            dish_index = _dish_patterns[word]
            break
    is_dish_pick = dish_index is not None and (
        any(w in t for w in ["dish", "first", "second", "third", "fourth", "fifth", "one", "two", "three", "four", "five", "the "])
        or len(t.split()) <= 3
    )
    if is_dish_pick and _last_menu.get(user_id):
        if pantry is None:
            pantry = db.get_pantry_names(user_id)
        await _elaborate_dish(update, context, user_id, text, dish_index, pantry)
        return

    # Check for substitution/scale on last elaborated recipe
    recipe = _last_suggestion.get(user_id)
    if recipe:
        # Detect substitution intent: swap/sub/replace/change/substitute + any ingredients
        sub_keywords = re.search(r"\b(swap|sub(?:stitute)?\b|replace|change|instead of|in place of)\b", t, re.I)
        if sub_keywords and len(t.split()) <= 20:
            msg = await update.effective_message.reply_text("Applying substitution...")
            result = ai.substitute_ingredient(recipe, text)
            _last_suggestion[user_id] = result
            _is_saved_recipe[user_id] = False
            mid = result.replace("**", "").replace("*", "").strip()
            _pending_cooked[raw_user_id] = mid.split("\n")[0] if mid else "Recipe"
            sanitized = _sanitize_markdown(result)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
                 InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
                [InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
            ])
            await _send_long_message(None, msg, sanitized, keyboard)
            return

        scale_match = re.search(r"(?:for|serve)\s*(\d+)\s*(?:people|person|servings|pax)", t, re.I)
        double_match = re.search(r"\b(double|triple|twice|half|halve)\b", t, re.I)
        if scale_match or double_match:
            if double_match:
                factor_map = {"double": 2, "triple": 3, "twice": 2, "half": 0.5, "halve": 0.5}
                factor = factor_map.get(double_match.group(1).lower(), 2)
            else:
                current_servings = 4
                sv_match = re.search(r"servings?\s*:?\s*(\d+)", recipe, re.I)
                if sv_match:
                    current_servings = int(sv_match.group(1))
                target = int(scale_match.group(1))
                factor = target / current_servings
            msg = await update.effective_message.reply_text(f"Scaling recipe by {factor}x...")
            result = ai.scale_recipe(recipe, factor)
            _last_suggestion[user_id] = result
            _is_saved_recipe[user_id] = False
            mid = result.replace("**", "").replace("*", "").strip()
            _pending_cooked[raw_user_id] = mid.split("\n")[0] if mid else "Recipe"
            sanitized = _sanitize_markdown(result)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
                 InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
                [InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
            ])
            await _send_long_message(None, msg, sanitized, keyboard)
            return

    # Recipe follow-up: if user is asking a question about the current recipe
    recipe = _last_suggestion.get(user_id)
    if recipe:
        t_lower = text.lower().strip()
        is_question = t_lower.endswith("?") or any(
            kw in t_lower.split()[:3] for kw in ["what", "how", "can", "does", "is", "do", "are", "why"]
        )
        known_actions = {"add", "remove", "clear", "list", "show", "save", "delete",
                         "suggest", "cook", "make", "remember", "i have", "my", "help"}
        first_word = t_lower.split()[0] if t_lower.split() else ""
        if is_question and first_word not in known_actions:
            msg = await update.effective_message.reply_text("Let me answer that... ദ്ദി(˵ •̀ ᴗ - ˵ ) ✧")
            answer = ai.recipe_followup(recipe, text)
            sanitized = _sanitize_markdown(answer)
            await msg.edit_text(sanitized, parse_mode="Markdown")
            return

    # --- Route bare numbers to recipe view (before NL) ---
    t_stripped = text.strip()
    if re.fullmatch(r'\d+(?:[,\s]\d+)*', t_stripped):
        first_num = int(re.findall(r'\d+', t_stripped)[0])
        user_recipes = db.get_recipes(user_id)
        if 1 <= first_num <= len(user_recipes):
            recipe = db.get_recipe(user_id, user_recipes[first_num - 1]["id"])
            if recipe:
                text_out = _format_recipe(recipe)
                _last_suggestion[user_id] = text_out
                _is_saved_recipe[user_id] = True
                _pending_cooked[raw_user_id] = recipe["title"]
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
                     InlineKeyboardButton("📤 Export", callback_data="export_last"),
                     InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                    [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
                ])
                sanitized = _sanitize_markdown(text_out)
                await _reply_chunked(update.effective_message, sanitized, parse_mode="Markdown", reply_markup=keyboard)
                return

    # --- Regex pre-checks for add/remove (bypass AI) ---
    add_match = re.match(r"(?:add|put|store|i have|i've got|got|have)\s+(.+?)(?:\s+(?:to|in|into|the)\s+pantry)?$", t_lower)
    if add_match:
        raw = add_match.group(1)
        items = [x.strip() for x in re.split(r'[,&]|\s+and\s+', raw) if x.strip()]
        if items:
            expiry = _parse_expiry(raw)
            eff_id = _effective_user_id(user_id)
            db.add_pantry_items(eff_id, items, expiry=expiry)
            cat_map = ai.categorize_pantry_items(items)
            if cat_map:
                db.recategorize_by_map(eff_id, cat_map)
            await update.effective_message.reply_text(f"Added {len(items)} item(s): {', '.join(items)}")
            return

    remove_match = re.match(r"(?:remove|delete|discard)\s+(.+?)(?:\s+(?:from|off|the)\s+pantry)?$", t_lower)
    if remove_match:
        items = [x.strip() for x in re.split(r'[,&]|\s+and\s+', remove_match.group(1)) if x.strip()]
        if items:
            eff_id = _effective_user_id(user_id)
            db.remove_pantry_items(eff_id, items)
            await update.effective_message.reply_text(f"Removed {len(items)} item(s): {', '.join(items)}")
            return

    # --- NL Processing ---
    msg = await update.effective_message.reply_text("Thinking... (ᵕ—ᴗ—)")
    if pantry is None:
        pantry = db.get_pantry_names(user_id)
    if recipes is None:
        recipes = db.get_recipes(user_id)
    result = ai.process_natural_language(text, pantry, recipes)
    action = result.get("action", "chat")
    items = result.get("items", [])
    reply = result.get("message", "")

    if action == "add":
        eff_id = _effective_user_id(user_id)
        expiry = _parse_expiry(result.get("expiry_date", ""))
        db.add_pantry_items(eff_id, items, expiry=expiry)
        if items:
            cat_map = ai.categorize_pantry_items(items)
            if cat_map:
                db.recategorize_by_map(eff_id, cat_map)
        await msg.edit_text("Added and sorted!")

    elif action == "remove":
        eff_id = _effective_user_id(user_id)
        if items:
            db.remove_pantry_items(eff_id, items)
        await msg.edit_text(reply)

    elif action == "clear_pantry":
        eff_id = _effective_user_id(user_id)
        shared_pantry = db.get_pantry_names(eff_id)
        for item in shared_pantry:
            db.remove_pantry_item(eff_id, item)
        await msg.edit_text("Pantry cleared!")

    elif action == "list_pantry":
        groups = db.get_pantry_grouped(_effective_user_id(user_id))
        if not groups:
            await msg.edit_text("Your pantry is empty. Tell me what to add!")
            return
        await _send_long_message(update, msg, _format_pantry_grouped(groups), None)

    elif action == "set_preference":
        if len(items) >= 2:
            db.set_user_preference(user_id, items[0], " ".join(items[1:]))
            await msg.edit_text(reply or f"Saved your {items[0]}.")
        else:
            await msg.edit_text("Tell me what to remember. Example: I have an air fryer")

    elif action == "expiring":
        ex = db.get_expiring_items(_effective_user_id(user_id))
        if not ex:
            await msg.edit_text("Nothing expiring soon!")
            return
        lines = ["Expiring soon:"]
        for it in ex:
            d = _fmt_date(it['expiry_date'])
            remaining = (datetime.strptime(it['expiry_date'], "%Y-%m-%d").date() - date.today()).days
            days_str = f" ({remaining} days left)" if remaining >= 0 else ""
            lines.append(f"- {it['name'].title()} (expires {d}){days_str}")
        await msg.edit_text("\n".join(lines))

    elif action == "suggest":
        await msg.delete()
        await _do_suggest(update, context, user_id, pantry, text)

    elif action == "elaborate":
        await msg.delete()
        query_text = " ".join(items).lower() if items else text.lower()
        dish_index = None
        for word in query_text.split():
            if word in ["1", "2", "3", "4", "5"]:
                dish_index = int(word) - 1
                break
        if dish_index is None:
            for word, idx in {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "one": 0, "two": 1, "three": 2, "four": 3, "five": 4}.items():
                if word in query_text:
                    dish_index = idx
                    break
        if dish_index is None:
            menu = _last_menu.get(user_id, [])
            for i, d in enumerate(menu):
                if any(w in d.get("title", "").lower() for w in query_text.split()):
                    dish_index = i
                    break
        await _elaborate_dish(update, context, user_id, text, dish_index, pantry)

    elif action == "save_recipe":
        recipe_name = " ".join(items) if items else text
        recipe_name = recipe_name.replace("save", "").replace("recipe", "").replace("add", "").strip()
        await msg.edit_text(f"Creating recipe for {recipe_name}...")
        result_text = ai.generate_recipe_by_name(recipe_name)
        if result_text:
            parsed = ai.parse_recipe_text(result_text)
            if parsed and parsed["title"]:
                db.save_recipe(
                    user_id, parsed["title"], parsed["description"],
                    parsed["ingredients"], parsed["instructions"],
                    full_text=result_text,
                    protein_g=parsed["protein_g"], calories=parsed["calories"],
                    sodium_mg=parsed["sodium_mg"],
                )
                await msg.edit_text(f"Saved recipe: {parsed['title']}")
            else:
                await msg.edit_text(
                    f"Could not parse the recipe. Here's what the AI generated:\n\n{result_text}"
                )
        else:
            await msg.edit_text("Failed to generate recipe. Try being more specific.")

    elif action == "canbake":
        await msg.delete()
        await canbake(update, context)

    elif action == "calories":
        await msg.delete()
        await daily_tracking(update, context)

    elif action == "weekly":
        await msg.delete()
        await weekly(update, context)

    elif action == "list_recipes":
        await msg.delete()
        await list_recipes(update, context)

    elif action == "delete_recipe":
        query_text = " ".join(items) if items else text
        rids = [int(w) for w in query_text.split() if w.isdigit()]
        if rids:
            for rid in rids:
                db.delete_recipe(user_id, rid)
            deleted = ", ".join(f"#{r}" for r in rids)
            await msg.edit_text(f"Deleted recipe(s): {deleted}.")
        else:
            recipes = db.get_recipes(user_id)
            targets = []
            for r in recipes:
                if any(w.lower() in r["title"].lower() for w in query_text.split()):
                    targets.append(r)
            if targets:
                for t in targets:
                    db.delete_recipe(user_id, t["id"])
                names = ", ".join(t["title"] for t in targets)
                await msg.edit_text(f"Deleted: {names}")
            else:
                await msg.edit_text("Recipe not found. Say 'show my recipes' to see them.")

    elif action == "view_recipe":
        await msg.delete()
        query_text = " ".join(items) if items else text
        recipe = None
        for word in query_text.split():
            if word.isdigit():
                recipe = db.get_recipe(user_id, int(word))
                break
        if not recipe:
            recipe = db.get_recipe_by_title(user_id, query_text)
        if not recipe:
            recipes = db.get_recipes(user_id)
            for r in recipes:
                if any(w.lower() in r["title"].lower() for w in query_text.split()):
                    recipe = db.get_recipe(user_id, r["id"])
                    break
        if recipe:
            text = _format_recipe(recipe)
            _last_suggestion[user_id] = text
            _is_saved_recipe[user_id] = True
            _pending_cooked[raw_user_id] = recipe["title"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
                 InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
                [InlineKeyboardButton("❓ Ask a question", callback_data="ask_question")],
            ])
            busy = await update.effective_message.reply_text("Fetching recipe...")
            sanitized = _sanitize_markdown(text)
            await _send_long_message(None, busy, sanitized, keyboard)
        else:
            await update.effective_message.reply_text(
                "Recipe not found. Say 'show my recipes' to see your saved recipes."
            )

    elif action == "scan_receipt":
        await msg.edit_text("Send me a photo of your receipt with the caption 'scan receipt'!")

    elif action == "help":
        await msg.delete()
        await start(update, context)

    else:
        if _last_suggestion.get(user_id):
            await msg.edit_text("Let me think about that... (˶˃ ᵕ ˂˶)")
            answer = ai.chef_chat(text)
            await msg.edit_text(_sanitize_markdown(answer), parse_mode="Markdown")
        else:
            await msg.edit_text(reply if reply else
                "I can help manage your pantry, suggest recipes, track meals, and more! "
                "Try: 'add chicken and rice to pantry' or 'what can I cook?'")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    text = update.message.text.strip()

    if user_id == OWNER_TELEGRAM_ID:
        edit_tx_id = _parse_edit.get(user_id)
        if edit_tx_id is not None:
            _parse_edit.pop(user_id, None)
            tx = db.get_parsed_transaction(edit_tx_id)
            if not tx:
                await update.effective_message.reply_text("Transaction not found.")
                return
            result = ai.parse_edit_natural(text, tx, _load_rulebook(user_id))
            if not result:
                await update.effective_message.reply_text(
                    "Couldn't understand that. Try: \"it's Si Si Nan Chun, Food/Eating Out\""
                )
                return
            update_fields = {}
            for field in ("merchant", "category", "subcategory", "account", "tx_type", "notes"):
                val = result.get(field)
                if val:
                    update_fields[field] = val
            if result.get("amount") is not None:
                update_fields["amount"] = float(result["amount"])
            if update_fields.get("tx_type"):
                v = update_fields["tx_type"].strip().capitalize()
                update_fields["tx_type"] = v if v in ("Expense", "Income") else "Expense"
            if update_fields:
                db.update_parsed_transaction(edit_tx_id, **update_fields)
                if not result.get("add_rule"):
                    db.update_parsed_transaction(edit_tx_id, confirmed=1)
            elif not update_fields and result and (result.get("add_rule") or "confirm" in text.lower() or "yes" in text.lower()):
                db.update_parsed_transaction(edit_tx_id, confirmed=1)
            if result.get("add_rule"):
                merchant = update_fields.get("merchant") or tx["merchant"]
                cat = update_fields.get("category") or tx.get("category") or ""
                sub = update_fields.get("subcategory") or tx.get("subcategory") or ""
                full_cat = f"{cat}/{sub}" if cat and sub else cat
                if full_cat:
                    db.add_transaction_rule(user_id, merchant.lower(), full_cat)
            updated = db.get_parsed_transaction(edit_tx_id)
            if update_fields:
                msg_parts = []
                for field in ("merchant", "category", "subcategory", "account", "tx_type"):
                    val = updated.get(field) or ""
                    if val:
                        msg_parts.append(f"{field.capitalize()}: {val}")
                if updated.get("amount"):
                    msg_parts.append(f"Amount: ${updated['amount']:.2f}")
                reply = f"✅ Updated:\n" + "\n".join(msg_parts)
            else:
                reply = "✅ Accepted suggestion."
            if result.get("add_rule"):
                reply += "\n✅ Rule saved!"
            await update.effective_message.reply_text(reply)
            if tx:
                rtext, rkb = _render_parse_session(tx["session_id"])
                if rtext and user_id in _parse_msg:
                    try:
                        await context.bot.edit_message_text(
                            rtext,
                            chat_id=update.effective_chat.id,
                            message_id=_parse_msg[user_id],
                            reply_markup=rkb,
                        )
                    except Exception:
                        pass
            return

        if re.search(r'(?:remember|learn|add rule|note|save|record|log|this is)\s+.*?(?:is|as|→|->|:).*', text, re.IGNORECASE):
            result = ai.parse_edit_natural(text, {"merchant":"","category":"","subcategory":"","amount":0,"account":"","tx_type":"Expense"}, _load_rulebook(user_id))
            if result and result.get("merchant") and (result.get("category") or result.get("subcategory")):
                merchant = result["merchant"]
                cat = result.get("category") or ""
                sub = result.get("subcategory") or ""
                full_cat = f"{cat}/{sub}" if cat and sub else cat
                db.add_transaction_rule(user_id, merchant.lower(), full_cat)
                await update.effective_message.reply_text(
                    f"✅ Got it! I'll remember \"{merchant}\" → {full_cat}."
                )
                return
            await update.effective_message.reply_text(
                "Couldn't understand the rule. Try: \"remember Si Si Nan Chun is Food/Eating Out\""
            )
            return

        if _pending_statement.get(user_id):
            _pending_statement.pop(user_id, None)
            await handle_statement_parse(update, context, user_id, text)
            return

    receipt_state = _receipt_items.get(user_id)
    if receipt_state and receipt_state["items"]:
        t = text.lower().strip()

        if t.startswith("remove "):
            name = t[7:].strip()
            before = len(receipt_state["items"])
            receipt_state["items"] = [i for i in receipt_state["items"] if i["name"] != name]
            if len(receipt_state["items"]) < before:
                if not receipt_state["items"]:
                    _receipt_items.pop(user_id, None)
                    await update.message.reply_text("All items removed. Receipt scan cancelled.")
                    return
                text, keyboard = _render_receipt(user_id)
                await context.bot.edit_message_text(text,
                    chat_id=update.effective_chat.id, message_id=receipt_state["msg_id"],
                    reply_markup=keyboard)
                status = await update.message.reply_text(f"\u274c Removed {name.title()}.")
                await status.delete()
                return
            else:
                await update.message.reply_text(f"Item '{name}' not found in the list.")
                return

        m = re.match(r'expiry\s+(\d+)\s+(.+)', t)
        if m:
            idx = int(m.group(1)) - 1
            date_input = m.group(2).strip()
            if 0 <= idx < len(receipt_state["items"]):
                parsed = _parse_expiry(date_input)
                if parsed:
                    receipt_state["items"][idx]["expiry"] = parsed
                    text, keyboard = _render_receipt(user_id)
                    await context.bot.edit_message_text(text,
                        chat_id=update.effective_chat.id, message_id=receipt_state["msg_id"],
                        reply_markup=keyboard)
                    status = await update.message.reply_text(f"\u2705 Updated expiry for item #{idx+1}.")
                    await status.delete()
                else:
                    await update.message.reply_text("Could not parse that date. Try '3 days', '15/07/25', 'next friday'.")
            else:
                await update.message.reply_text(f"Item #{idx+1} doesn't exist. There are {len(receipt_state['items'])} items.")
            return

    recipe_text = _asking_question.pop(user_id, None)
    if recipe_text is not None:
        msg = await update.effective_message.reply_text("Let me answer that... ദ്ദി(˵ •̀ ᴗ - ˵ ) ✧")
        answer = ai.recipe_followup((recipe_text or ""), text)
        await msg.edit_text(_sanitize_markdown(answer), parse_mode="Markdown")
        return

    await _handle_user_text(update, context, user_id, text)


# --- Photo recognition ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.store_chat_id(update.effective_user.id, update.effective_chat.id)
    user_id = update.effective_user.id
    _check_ai_limit(user_id)
    caption = (update.message.caption or "").lower()
    is_receipt = any(w in caption for w in ["receipt", "scan", "recipt"])

    today = date.today()
    pc = _photo_counts.get(user_id)
    if not pc or pc["date"] != today:
        pc = {"date": today, "count": 0}
        _photo_counts[user_id] = pc
    if user_id != OWNER_TELEGRAM_ID and pc["count"] >= PHOTO_DAILY_LIMIT:
        await update.message.reply_text("Daily photo scan limit reached (30). Try again tomorrow.")
        return
    pc["count"] += 1

    photos = update.message.photo
    if not photos:
        return
    photo = photos[-1]
    for candidate in photos:
        if candidate.width >= 800 and candidate.file_size and candidate.file_size <= MAX_PHOTO_SIZE:
            photo = candidate
            break
    if photo.file_size and photo.file_size > MAX_PHOTO_SIZE:
        await update.message.reply_text("Image too large. Please send a smaller photo (<10MB).")
        return

    if is_receipt:
        msg = await update.message.reply_text("\U0001f9fe Scanning receipt...")
        try:
            file = await photo.get_file()
            file_bytes = await file.download_as_bytearray()
            b64 = base64.b64encode(file_bytes).decode("utf-8")
            names = ai.scan_receipt_from_image(b64)
            if not names:
                await msg.edit_text("Could not read items from this receipt. Try a clearer photo.")
                return
            items = [{"name": n.strip().lower(), "expiry": ""} for n in names if n.strip()]
            _receipt_items[user_id] = {"items": items, "msg_id": msg.message_id}
            text, keyboard = _render_receipt(user_id)
            await msg.edit_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Receipt scan failed for user {user_id}: {e}")
            await msg.edit_text("Could not scan this receipt. Try a clearer photo.")
        return

    msg = await update.message.reply_text("Analyzing image...")
    try:
        file = await photo.get_file()
        file_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(file_bytes).decode("utf-8")

        caption = update.message.caption or ""
        result = ai.analyze_meal_image(b64, caption)

        if not result:
            await msg.edit_text("Could not process this image. Try a clearer photo.")
            return

        await msg.edit_text(_sanitize_markdown(result), parse_mode="Markdown")

        raw_caption = (update.message.caption or "").strip().lower()
        meal_types = {"breakfast", "lunch", "dinner", "snack"}
        if raw_caption in meal_types:
            nut = ai._extract_nutrition_from_analysis(result)
            db.log_meal(
                user_id,
                raw_caption.capitalize(),
                nut.get("calories", 0),
                nut.get("protein_g", 0),
                nut.get("sodium_mg", 0),
                raw_caption,
            )
    except Exception as e:
        logger.error(f"Image analysis failed for user {user_id}: {e}")
        await msg.edit_text("Could not process this image. Try a clearer photo.")


# --- Voice ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _check_ai_limit(user_id)
    db.store_chat_id(user_id, update.effective_chat.id)
    msg = await update.message.reply_text("Transcribing voice...")
    try:
        file = await update.message.voice.get_file()
        file_bytes = await file.download_as_bytearray()
        text = ai.transcribe_audio(bytes(file_bytes))
        if not text:
            await msg.edit_text("Could not transcribe audio. Try typing instead.")
            return
        await msg.edit_text(f"You said: {text}")
        user_id = update.effective_user.id
        await _handle_user_text(update, context, user_id, text)
    except Exception as e:
        logger.exception(f"Voice processing failed for user {user_id}")
        await msg.edit_text("Could not process voice message. Try typing instead.")


def _render_shopping_list(user_id):
    items = db.get_shopping_list(user_id)
    if not items:
        return None, None
    unchecked = [i for i in items if not i["checked"]]
    checked = [i for i in items if i["checked"]]
    lines = ["📋 *Shopping List*", ""]
    for i, item in enumerate(unchecked, 1):
        lines.append(f"⬜ {i}. {item['name'].title()}")
    for i, item in enumerate(checked, len(unchecked) + 1):
        lines.append(f"✅ {i}. {item['name'].title()}")
    lines.append("")
    lines.append(f"*{len(unchecked)} remaining* · {len(checked)} checked")
    keyboard = []
    row = []
    for i, item in enumerate(items, 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"shop_toggle_{i}"))
        if len(row) >= 8:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("\U0001f9ee Calc", callback_data="calc_open"),
        InlineKeyboardButton("\U0001f5d1 Clear", callback_data="shop_clear_checked"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _render_calculator(user_id):
    state = _calc_state.get(user_id, {"expr": ""})
    expr = state["expr"]
    list_text, _ = _render_shopping_list(user_id) or ("", None)
    lines = []
    if list_text:
        lines.append(list_text)
        lines.append("")
    lines.append("\U0001f9ee *Calculator*")
    if expr:
        lines.append(f"`{expr}`")
        safe = re.sub(r'[^\d+\-*/.% ]', "", expr).replace("%", "/100")
        try:
            if safe and not re.search(r'[+\-*/.]{2,}', safe) and not safe.startswith(("+", "*", "/", ".")):
                total = eval(safe)
                if total == int(total):
                    total = int(total)
                lines.append(f"`= {total:,}`")
            else:
                lines.append("")
        except Exception:
            lines.append("")
    else:
        lines.append("_Tap numbers and operators_\n")
    text = "\n".join(lines)
    kb = [
        [InlineKeyboardButton("7", callback_data="calc_d_7"), InlineKeyboardButton("8", callback_data="calc_d_8"), InlineKeyboardButton("9", callback_data="calc_d_9"), InlineKeyboardButton("\u00f7", callback_data="calc_o_/")],
        [InlineKeyboardButton("4", callback_data="calc_d_4"), InlineKeyboardButton("5", callback_data="calc_d_5"), InlineKeyboardButton("6", callback_data="calc_d_6"), InlineKeyboardButton("\u00d7", callback_data="calc_o_*")],
        [InlineKeyboardButton("1", callback_data="calc_d_1"), InlineKeyboardButton("2", callback_data="calc_d_2"), InlineKeyboardButton("3", callback_data="calc_d_3"), InlineKeyboardButton("-", callback_data="calc_o_-")],
        [InlineKeyboardButton("0", callback_data="calc_d_0"), InlineKeyboardButton(".", callback_data="calc_dot"), InlineKeyboardButton("%", callback_data="calc_pct"), InlineKeyboardButton("+", callback_data="calc_o_+")],
        [InlineKeyboardButton("C", callback_data="calc_clear"), InlineKeyboardButton("\u232b", callback_data="calc_bs"), InlineKeyboardButton("=", callback_data="calc_eq")],
        [InlineKeyboardButton("\U0001f519 Back to List", callback_data="calc_back")],
    ]
    return text, InlineKeyboardMarkup(kb)


async def calc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)
    data = query.data

    state = _calc_state.get(user_id, {"expr": ""})
    expr = state["expr"]

    if data == "calc_open":
        _calc_state.pop(user_id, None)
        expr = ""
    elif data == "calc_back":
        _calc_state.pop(user_id, None)
        text, kb = _render_shopping_list(user_id)
        if text:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    elif data == "calc_clear":
        expr = ""
    elif data == "calc_bs":
        expr = expr[:-1]
    elif data == "calc_eq":
        safe = re.sub(r'[^\d+\-*/.% ]', "", expr).replace("%", "/100")
        try:
            if safe and not re.search(r'[+\-*/.]{2,}', safe) and not safe.startswith(("+", "*", "/", ".")):
                total = eval(safe)
                if total == int(total):
                    total = int(total)
                expr = str(total)
        except Exception:
            pass
    elif data == "calc_pct":
        expr += "%"
    elif data.startswith("calc_d_"):
        digit = data.split("_")[2]
        expr += digit
    elif data.startswith("calc_o_"):
        op = data.split("_")[2]
        if op == "/":
            op = "/"
        elif op == "*":
            op = "*"
        expr += op
    elif data == "calc_dot":
        expr += "."

    _calc_state[user_id] = {"expr": expr}
    text, kb = _render_calculator(user_id)
    if text:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)
    data = query.data
    if data.startswith("shop_toggle_"):
        idx = int(data.split("_")[2])
        items = db.get_shopping_list(user_id)
        if 1 <= idx <= len(items):
            db.toggle_shopping_item(items[idx - 1]["id"])
        text, keyboard = _render_shopping_list(user_id)
        if text:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    elif data == "shop_clear_checked":
        db.clear_checked_items(user_id)
        text, keyboard = _render_shopping_list(user_id)
        if text:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def receipt_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _effective_user_id(query.from_user.id)
    data = _receipt_items.pop(user_id, None)
    if not data or not data["items"]:
        await query.edit_message_text("Nothing to add.")
        return
    items_with_expiry = [(item["name"], item.get("expiry", "")) for item in data["items"]]
    db.add_pantry_items_with_expiry(user_id, items_with_expiry)
    added = [item["name"].title() for item in data["items"]]
    await query.edit_message_text(f"\u2705 Added {len(added)} item(s) to pantry: {', '.join(added)}")


def _extract_item_name(text):
    m = re.search(r'\*\*(?:🛒 Ingredient|🧰 Equipment):\*\*\s*(.+?)(?:\n|$)', text)
    return m.group(1).strip().lower() if m else ""


def _parse_random_args(args):
    country_parts = []
    item_type = "any"
    type_keywords = {"ingredient": "ingredient", "ingredients": "ingredient", "food": "ingredient",
                     "equipment": "equipment", "tool": "equipment", "tools": "equipment", "gear": "equipment"}
    for arg in args:
        low = arg.lower().strip()
        if low in type_keywords:
            item_type = type_keywords[low]
        else:
            country_parts.append(arg)
    country = " ".join(country_parts).strip()
    return country, item_type


async def random_ingredient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _check_ai_limit(update.effective_user.id)
    user_id = update.effective_user.id
    country, item_type = _parse_random_args(context.args or [])
    _random_country[user_id] = country
    _random_item_type[user_id] = item_type
    type_label = {"ingredient": "ingredient", "equipment": "kitchen tool", "any": "ingredient or tool"}[item_type]
    msg = await update.message.reply_text(
        f"Finding a {country + ' ' if country else ''}{type_label}..."
    )
    called = db.get_called_ingredients(user_id)
    result = ai.suggest_random_item(country, called, item_type=item_type)
    if result == "NO_INGREDIENTS_LEFT":
        await msg.edit_text("No more unique items found! Try /randomreset to start over.")
        return
    if result == "AI error" or result.startswith("Something went wrong"):
        await msg.edit_text("AI is temporarily unavailable (high demand). Try again in a moment.")
        return
    name = _extract_item_name(result)
    if name:
        db.add_called_ingredient(user_id, name)
    _last_suggestion[_effective_user_id(user_id)] = result
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerate", callback_data="random_regenerate"),
         InlineKeyboardButton("🔀 Switch type", callback_data="random_switch")],
    ])
    sanitized = _sanitize_markdown(result)
    await msg.edit_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def random_regenerate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    country = _random_country.get(user_id, "")
    item_type = _random_item_type.get(user_id, "any")
    called = db.get_called_ingredients(user_id)
    result = ai.suggest_random_item(country, called, item_type=item_type)
    if result == "NO_INGREDIENTS_LEFT":
        await query.edit_message_text("No more unique items found! Try /randomreset to start over.")
        return
    if result == "AI error" or result.startswith("Something went wrong"):
        await query.edit_message_text("AI is temporarily unavailable (high demand). Try again in a moment.")
        return
    name = _extract_item_name(result)
    if name:
        db.add_called_ingredient(user_id, name)
    _last_suggestion[_effective_user_id(user_id)] = result
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerate", callback_data="random_regenerate"),
         InlineKeyboardButton("🔀 Switch type", callback_data="random_switch")],
    ])
    sanitized = _sanitize_markdown(result)
    await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def random_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    current = _random_item_type.get(user_id, "any")
    if current == "ingredient":
        new_type = "equipment"
    elif current == "equipment":
        new_type = "ingredient"
    else:
        import random as _random
        new_type = _random.choice(["ingredient", "equipment"])
    _random_item_type[user_id] = new_type
    country = _random_country.get(user_id, "")
    called = db.get_called_ingredients(user_id)
    result = ai.suggest_random_item(country, called, item_type=new_type)
    if result == "NO_INGREDIENTS_LEFT":
        await query.edit_message_text("No more unique items found! Try /randomreset to start over.")
        return
    if result == "AI error" or result.startswith("Something went wrong"):
        await query.edit_message_text("AI is temporarily unavailable (high demand). Try again in a moment.")
        return
    name = _extract_item_name(result)
    if name:
        db.add_called_ingredient(user_id, name)
    _last_suggestion[_effective_user_id(user_id)] = result
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerate", callback_data="random_regenerate"),
         InlineKeyboardButton("🔀 Switch type", callback_data="random_switch")],
    ])
    sanitized = _sanitize_markdown(result)
    await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def random_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.set_user_preference(user_id, "called_ingredients", "[]")
    await update.message.reply_text("🔄 Reset! All previously shown items can appear again.")


# --- Token Usage / Cost Tracking ---
# DeepSeek pricing per 1M tokens (deepseek-v4-flash)
PRICE_INPUT_CACHE_MISS = 0.14
PRICE_INPUT_CACHE_HIT = 0.0028
PRICE_OUTPUT = 0.28


def _calc_cost(stats):
    prompt_miss = stats["p"] - stats["h"]
    if prompt_miss < 0:
        prompt_miss = 0
    cost = (prompt_miss / 1_000_000 * PRICE_INPUT_CACHE_MISS
            + stats["h"] / 1_000_000 * PRICE_INPUT_CACHE_HIT
            + stats["c"] / 1_000_000 * PRICE_OUTPUT)
    return cost


def _fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    today = db.get_token_usage_today()
    month = db.get_token_usage_month()
    lifetime = db.get_token_usage_lifetime()
    today_cost = _calc_cost(today)
    month_cost = _calc_cost(month)
    lifetime_cost = _calc_cost(lifetime)
    text = (
        "📊 *Token Usage & Cost*\n"
        f"Model: `deepseek-v4-flash`\n"
        f"Pricing: input `${PRICE_INPUT_CACHE_MISS}/M` (cache miss), `${PRICE_INPUT_CACHE_HIT}/M` (cache hit), output `${PRICE_OUTPUT}/M`\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "*Today*\n"
        f"  Prompt: {_fmt_tokens(today['p'])} (cache hit: {_fmt_tokens(today['h'])})\n"
        f"  Output: {_fmt_tokens(today['c'])}\n"
        f"  Calls: {today['n']}\n"
        f"  Cost: ${today_cost:.4f}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "*This Month*\n"
        f"  Prompt: {_fmt_tokens(month['p'])} (cache hit: {_fmt_tokens(month['h'])})\n"
        f"  Output: {_fmt_tokens(month['c'])}\n"
        f"  Calls: {month['n']}\n"
        f"  Cost: ${month_cost:.4f}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "*Lifetime*\n"
        f"  Prompt: {_fmt_tokens(lifetime['p'])} (cache hit: {_fmt_tokens(lifetime['h'])})\n"
        f"  Output: {_fmt_tokens(lifetime['c'])}\n"
        f"  Calls: {lifetime['n']}\n"
        f"  Cost: ${lifetime_cost:.4f}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# --- Finance: Statement Parser & Bill Tracking ---


def _ddmmyy_to_mmddyyyy(d):
    if not d:
        return ""
    d = d.strip()
    parts = re.split(r'[/\-\.]', d)
    if len(parts) >= 3:
        dd, mm, yy = parts[0], parts[1], parts[2]
        if len(dd) == 1:
            dd = "0" + dd
        if len(mm) == 1:
            mm = "0" + mm
        if len(yy) == 2:
            yy = "20" + yy
        elif len(yy) == 4:
            yy = yy[2:]
        return f"{mm}/{dd}/{yy}"
    return d


def _ddmmyy_to_iso(d):
    if not d:
        return ""
    d = d.strip()
    parts = re.split(r'[/\-\.]', d)
    if len(parts) >= 3:
        dd, mm, yy = parts[0], parts[1], parts[2]
        dd, mm = int(dd), int(mm)
        yy = int(yy)
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd).isoformat()
        except ValueError:
            return ""
    return ""


def _fmt_ddmmyy(d):
    if not d:
        return ""
    d = d.strip()
    parts = re.split(r'[/\-\.]', d)
    if len(parts) >= 2:
        dd, mm = parts[0], parts[1]
        if len(dd) == 1:
            dd = "0" + dd
        if len(mm) == 1:
            mm = "0" + mm
        return f"{dd}/{mm}"
    return d


def _load_rulebook(user_id):
    rb = db.get_user_preference(user_id, "rulebook_md")
    if rb:
        return rb
    try:
        with open(os.path.join(os.path.dirname(__file__), "money_manager.md"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _get_valid_categories(user_id):
    rb = _load_rulebook(user_id)
    if not rb:
        return [], []
    expense_cats = set()
    income_cats = set()
    in_expense_table = False
    in_income = False
    for line in rb.split("\n"):
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("## expense categories"):
            in_expense_table = True
            in_income = False
            continue
        if low.startswith("## income categories"):
            in_expense_table = False
            in_income = True
            continue
        if low.startswith("## ") and "categor" not in low:
            in_expense_table = False
            in_income = False
            continue
        if in_expense_table:
            if stripped.startswith("|") and "---" not in stripped and "category" not in low:
                parts = [p.strip() for p in stripped.split("|")[1:-1]]
                if parts and parts[0] and not parts[0].startswith("-") and parts[0] != "\\---":
                    expense_cats.add(parts[0])
        if in_income:
            if stripped and not stripped.startswith("#") and not stripped.startswith("|") and not stripped.startswith("---") and not stripped.startswith("\\---"):
                cats = [c.strip() for c in stripped.split(",") if c.strip()]
                for c in cats:
                    if not c.startswith("-") and c != "\\---":
                        income_cats.add(c)
    if not expense_cats:
        expense_cats = {"Food", "Social Life", "Self-development", "Transportation", "Holiday",
                        "Household", "Health", "Education", "Gift", "Other", "Insurance",
                        "Shopping", "Gaming", "Loan", "Income Tax"}
    if not income_cats:
        income_cats = {"Allowance", "Salary", "Bonus", "Investment", "Other / Reimbursement"}
    return sorted(expense_cats), sorted(income_cats)


def _validate_category(category, tx_type, expense_cats, income_cats):
    if not category:
        return False
    cat = category.strip()
    if tx_type == "Income":
        return cat in income_cats
    return cat in expense_cats or cat in income_cats


def _extract_pdf_text(file_bytes):
    text_parts = []
    try:
        import pdfplumber
        import io
        with pdfplumber.open(io.BytesIO(bytes(file_bytes))) as pdf:
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                if len(page_text.strip()) < 50:
                    tables = page.extract_tables()
                    if tables:
                        rows = []
                        for table in tables:
                            for row in table:
                                cleaned = [cell.strip() if cell else "" for cell in row]
                                rows.append("\t".join(cleaned))
                        page_text = "\n".join(rows)
                if page_text.strip():
                    text_parts.append(f"--- Page {i+1} ---\n{page_text}")
    except Exception as e:
        logger.error(f"pdfplumber extraction failed: {e}")
        try:
            import fitz
            import io
            doc = fitz.open(stream=bytes(file_bytes), filetype="pdf")
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
        except Exception as e2:
            logger.error(f"PyMuPDF extraction failed: {e2}")
            return ""
    return "\n".join(text_parts)


_CARD_PATTERNS = [
    ("ocbc", r"\bocbc\b"),
    ("uob ppv", r"\buob\s*ppv\b"),
    ("uob", r"\buob\b"),
    ("citi", r"\bciti(?:\s*rewards)?\b"),
    ("dbs", r"\bdbs\b"),
    ("sc", r"\bsc\s*simply\s*cash\b"),
    ("youtrip", r"\byoutrip\b"),
    ("maribank", r"\bmaribank\b"),
]


def _detect_card_name(text):
    low = text.lower()
    for name, pat in _CARD_PATTERNS:
        if re.search(pat, low):
            return name
    return ""


def _detect_card_last4(text):
    m = re.search(r'\b(?:card|acct|account|no\.?)\s*[:#]?\s*(\d{4})\b', text, re.I)
    if m:
        return m.group(1)
    m = re.search(r'\b\*{0,4}\s*(\d{4})\b', text)
    if m:
        return m.group(1)
    return ""


_ACCOUNT_ALIASES = {
    "ocbc 365": "OCBC 365",
    "ocbc": "OCBC 365",
    "posb": "POSB",
    "uob ppv": "UOB PPV",
    "ppv": "UOB PPV",
    "uob lady": "UOB Lady",
    "lady": "UOB Lady",
    "uob": "UOB PPV",
    "citi rewards": "Citi Rewards (Amaze)",
    "citi rewards (amaze)": "Citi Rewards (Amaze)",
    "amaze": "Citi Rewards (Amaze)",
    "citi": "Citi Rewards (Amaze)",
    "dbs altitude": "DBS Altitude",
    "altitude": "DBS Altitude",
    "dbs": "POSB",
    "sc simplycash": "SC SimplyCash",
    "simplycash": "SC SimplyCash",
    "sc": "SC SimplyCash",
    "youtrip": "YouTrip",
    "maribank": "MariBank",
    "mari": "MariBank",
}

_DEFAULT_ACCOUNT_NAMES = [
    "POSB", "UOB PPV", "Citi Rewards (Amaze)", "UOB Lady",
    "DBS Altitude", "SC SimplyCash", "YouTrip", "OCBC 365", "MariBank",
]


def _get_account_names(user_id):
    rb = _load_rulebook(user_id)
    names = []
    if rb:
        m = re.search(r'##\s*Account Names\s*\n(.+?)(?:\n\s*\n|\n#|\Z)', rb, re.DOTALL | re.IGNORECASE)
        if m:
            line = m.group(1).strip().split("\n")[0].strip()
            names = [n.strip() for n in line.split(",") if n.strip()]
    if not names:
        names = list(_DEFAULT_ACCOUNT_NAMES)
    return names


def _detect_account(text, card_name="", user_id=None):
    account_names = _get_account_names(user_id) if user_id else []
    if card_name:
        key = card_name.strip().lower()
        if key in _ACCOUNT_ALIASES:
            mapped = _ACCOUNT_ALIASES[key]
            if not account_names or mapped in account_names:
                return mapped
        for name in account_names:
            if key == name.lower():
                return name
    low = (text or "").lower()
    for key, account in sorted(_ACCOUNT_ALIASES.items(), key=lambda x: -len(x[0])):
        if key in low:
            if not account_names or account in account_names:
                return account
    for name in sorted(account_names, key=lambda x: -len(x)):
        if name.lower() in low:
            return name
    return ""


def _render_parse_session(session_id):
    session = db.get_parse_session(session_id)
    if not session:
        return None, None
    txs = db.get_parsed_transactions(session_id)
    if not txs:
        return "No transactions in this session.", None
    lines = ["\U0001f4c4 Parsed Statement"]
    keyboard = []

    confirmed_txs = [t for t in txs if t["confirmed"] == 1]
    identified_txs = [t for t in txs if t["confirmed"] != 1 and t["confidence"] != "low" and t["confirmed"] != -1]
    unclear_txs = [t for t in txs if t["confidence"] == "low" and t["confirmed"] != 1]

    if confirmed_txs:
        lines.append(f"\n\u2705 Auto-confirmed ({len(confirmed_txs)}):")
        for tx in confirmed_txs:
            d = _fmt_ddmmyy(tx["date"]) if tx["date"] else "??/??"
            amt = f"${tx['amount']:.2f}"
            cat_str = tx.get("category") or ""
            subcat = tx.get("subcategory") or ""
            cat_display = f" \u2192 {cat_str}/{subcat}" if cat_str and subcat else (f" \u2192 {cat_str}" if cat_str else "")
            lines.append(f"  {d} {tx['merchant'][:28]:<28} {amt:>8}{cat_display}")

    if identified_txs:
        lines.append(f"\n\U0001f50d Identified ({len(identified_txs)}):")
        for tx in identified_txs:
            d = _fmt_ddmmyy(tx["date"]) if tx["date"] else "??/??"
            amt = f"${tx['amount']:.2f}"
            cat_str = tx.get("category") or ""
            subcat = tx.get("subcategory") or ""
            cat_display = f" \u2192 {cat_str}/{subcat}" if cat_str and subcat else (f" \u2192 {cat_str}" if cat_str else "")
            lines.append(f"  {d} {tx['merchant'][:28]:<28} {amt:>8}{cat_display}")

    if unclear_txs:
        lines.append(f"\n\u2754 Flags \u2014 need your input ({len(unclear_txs)}):")
        for tx in unclear_txs:
            m = re.sub(r'\s*\(.*?\)\s*', '', tx['merchant']).strip()
            amt = f"${tx['amount']:.2f}" if tx["amount"] else "?"
            lines.append(f"  {m[:28]:<28} {amt:>8}")

    visible = [t for t in txs if t["confirmed"] != -1]
    n_conf = sum(1 for t in visible if t["confirmed"] == 1)
    n_identified = sum(1 for t in visible if t["confirmed"] != 1 and t["confidence"] != "low")
    n_unclear = len(unclear_txs)
    lines.append(f"\u2705 {n_conf} confirmed  \u2754 {n_unclear} flags  /  {len(visible)} total")
    keyboard.append([
        InlineKeyboardButton("\u2705 Confirm All", callback_data=f"parse_confirm_all_{session_id}"),
        InlineKeyboardButton("\u274c Cancel", callback_data=f"parse_cancel_{session_id}"),
    ])
    keyboard.append([
        InlineKeyboardButton("\U0001f501 Retry", callback_data=f"parse_retry_{session_id}"),
    ])
    for tx in txs:
        if tx["confirmed"] == -1:
            continue
        is_unclear = tx["confidence"] == "low" and tx["confirmed"] != 1
        prefix = "\u2754" if is_unclear else "\u2705"
        date_label = _fmt_ddmmyy(tx["date"]) if tx["date"] else "??/??"
        if tx["confirmed"] == 1:
            keyboard.append([
                InlineKeyboardButton(f"{prefix} {date_label} {tx['merchant'][:15]}", callback_data="parse_noop"),
                InlineKeyboardButton("Edit", callback_data=f"parse_edit_{tx['id']}"),
                InlineKeyboardButton("Reject", callback_data=f"parse_reject_{tx['id']}"),
            ])
        elif is_unclear:
            m_short = re.sub(r'\s*\(.*?\)\s*', '', tx['merchant']).strip()[:15]
            keyboard.append([
                InlineKeyboardButton(f"{prefix} {m_short}", callback_data="parse_noop"),
                InlineKeyboardButton("\U0001f50d Identify", callback_data=f"parse_identify_{tx['id']}"),
                InlineKeyboardButton("Edit", callback_data=f"parse_edit_{tx['id']}"),
                InlineKeyboardButton("Reject", callback_data=f"parse_reject_{tx['id']}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(f"{prefix} {date_label} {tx['merchant'][:15]}", callback_data="parse_noop"),
                InlineKeyboardButton("Confirm", callback_data=f"parse_confirm_{tx['id']}"),
                InlineKeyboardButton("Edit", callback_data=f"parse_edit_{tx['id']}"),
                InlineKeyboardButton("Reject", callback_data=f"parse_reject_{tx['id']}"),
            ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _generate_tsv(session_id):
    txs = db.get_parsed_transactions(session_id)
    rows = ["\t".join(["Date", "Account", "Category", "Subcategory", "Note", "Amount", "Income/Expense", "Description"])]
    for tx in txs:
        if tx["confirmed"] != 1:
            continue
        d = _ddmmyy_to_mmddyyyy(tx["date"])
        category = (tx.get("category") or "").strip()
        subcategory = (tx.get("subcategory") or "").strip()
        account = (tx.get("account") or "").strip()
        note = (tx.get("merchant") or "").strip()
        amount = f"{tx['amount']:.2f}"
        io = (tx.get("tx_type") or "Expense").strip().capitalize()
        if io not in ("Expense", "Income"):
            io = "Expense"
        rows.append("\t".join([d, account, category, subcategory, note, amount, io, ""]))
    return "\n".join(rows) + "\n"


def _apply_correction(user_id, tx_id, text):
    m = re.match(r'\s*([\w\s/]+?)\s*[:\-]\s*(.+)', text)
    if not m:
        return False, "Use format: `Category: Food` or `Subcategory: Groceries` or `Merchant: NTUC` or `Amount: 50` or `Date: 12/03/26` or `Account: OCBC 365` or `Type: Income`"
    field = m.group(1).strip().lower()
    value = m.group(2).strip()
    field_map = {
        "category": "category",
        "cat": "category",
        "subcategory": "subcategory",
        "subcat": "subcategory",
        "sub": "subcategory",
        "merchant": "merchant",
        "name": "merchant",
        "amount": "amount",
        "amt": "amount",
        "date": "date",
        "notes": "notes",
        "note": "notes",
        "account": "account",
        "acc": "account",
        "type": "tx_type",
        "tx_type": "tx_type",
        "income/expense": "tx_type",
        "ie": "tx_type",
    }
    key = field_map.get(field)
    if not key:
        return False, f"Unknown field '{field}'. Try Category, Subcategory, Merchant, Amount, Date, Account, Type, or Notes."
    tx = db.get_parsed_transaction(tx_id)
    if not tx:
        return False, "Transaction not found."
    update = {key: value}
    if key == "amount":
        try:
            update["amount"] = float(re.sub(r'[^\d.]', '', value))
        except ValueError:
            return False, "Amount must be a number."
    if key == "category":
        update["category"] = value
    if key == "subcategory":
        update["subcategory"] = value
    if key == "merchant":
        update["merchant"] = value
    if key == "account":
        normalized = _normalize_account(value, user_id)
        if normalized:
            update["account"] = normalized
        else:
            account_names = _get_account_names(user_id)
            return False, f"Unknown account. Use one of: {', '.join(account_names)}"
    if key == "tx_type":
        v = value.lower().strip()
        if v in ("income", "i"):
            update["tx_type"] = "Income"
        elif v in ("expense", "e"):
            update["tx_type"] = "Expense"
        else:
            return False, "Type must be 'Income' or 'Expense'."
    db.update_parsed_transaction(tx_id, **update)
    if key == "category" and tx["merchant"]:
        db.add_transaction_rule(user_id, tx["merchant"], value)
        return True, "Noted! I'll remember that for next time."
    if key == "merchant" and tx["category"]:
        db.add_transaction_rule(user_id, value, tx["category"])
        return True, "Noted! I'll remember that for next time."
    return True, "\u2705 Updated."


def _normalize_account(value, user_id=None):
    if not value:
        return ""
    low = value.strip().lower()
    account_names = _get_account_names(user_id) if user_id else []
    for name in account_names:
        if low == name.lower():
            return name
    if low in _ACCOUNT_ALIASES:
        mapped = _ACCOUNT_ALIASES[low]
        if not account_names or mapped in account_names:
            return mapped
    for name in account_names:
        if low in name.lower():
            return name
    return ""


async def handle_statement_parse(update, context, user_id, statement_text, override_msg=None, query=None):
    if not statement_text or not statement_text.strip():
        if override_msg:
            await override_msg.edit_text("No statement text found. Paste your statement text or upload a .txt/.csv/.pdf file.")
        elif update and update.effective_message:
            await update.effective_message.reply_text("No statement text found. Paste your statement text or upload a .txt/.csv/.pdf file.")
        return
    import hashlib
    raw_hash = hashlib.sha256(statement_text.encode("utf-8")).hexdigest()
    existing = db.get_parse_session_by_hash(user_id, raw_hash)
    confirmed_existing = [s for s in existing if s["status"] == "confirmed"]
    if confirmed_existing and not override_msg:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f501 Re-parse", callback_data=f"parse_reparse_new"),
             InlineKeyboardButton("\U0001f4c4 Show previous", callback_data=f"parse_show_{confirmed_existing[0]['id']}")],
        ])
        await update.effective_message.reply_text(
            "This statement was already parsed and confirmed. Re-parse or view previous results?",
            reply_markup=keyboard,
        )
        return
    if override_msg:
        busy = override_msg
    else:
        busy = await update.effective_message.reply_text("\U0001f9d1\u200d\U0001f4bb Parsing statement with AI...")
    rulebook = _load_rulebook(user_id)
    rules = db.get_transaction_rules(user_id)
    if rules:
        rule_lines = ["", "--- Learned Rules ---"]
        for r in rules:
            rule_lines.append(f"- {r['merchant_pattern']} -> {r['category']}")
        rulebook = rulebook + "\n".join(rule_lines)
    result = ai.parse_statement(statement_text, rulebook)
    if result.get("error"):
        await busy.edit_text(f"\u274c {result['error']}")
        return
    transactions = result.get("transactions", [])
    unclear = result.get("unclear_items", [])
    due_date = result.get("due_date", "")
    if not transactions and not unclear:
        await busy.edit_text("Could not extract any transactions from this statement. Try pasting the text more clearly.")
        return
    fallback_account = _detect_account(statement_text, user_id=user_id)
    expense_cats, income_cats = _get_valid_categories(user_id)
    learned_rules = {r["merchant_pattern"]: (r["category"], r.get("notes", "")) for r in db.get_transaction_rules(user_id)}
    session_id = db.create_parse_session(user_id, statement_text)
    seen_merchants = set()
    auto_confirmed = 0
    invalid_cats = []
    for tx in transactions:
        try:
            conf = (tx.get("confidence") or "high").strip().lower()
            merchant = (tx.get("merchant") or "").strip()
            merchant_lower = merchant.lower()
            tx_type = (tx.get("tx_type") or "Expense").strip().capitalize()
            if tx_type not in ("Expense", "Income"):
                tx_type = "Expense"
            category = (tx.get("category") or "").strip()
            subcategory = (tx.get("subcategory") or "").strip()
            if merchant_lower in learned_rules and conf != "low":
                learned_cat = learned_rules[merchant_lower][0]
                if "/" in learned_cat and tx_type == "Expense":
                    category, subcategory = learned_cat.split("/", 1)
                    category = category.strip()
                    subcategory = subcategory.strip()
                else:
                    category = learned_cat
                    subcategory = ""
                conf = "high"
                auto_confirm = True
            else:
                auto_confirm = False
            if category and not _validate_category(category, tx_type, expense_cats, income_cats):
                invalid_cats.append((merchant, category, tx_type))
                conf = "low"
                auto_confirm = False
            if conf == "low":
                if merchant and merchant not in seen_merchants:
                    db.add_parsed_transaction(
                        session_id, user_id, merchant, 0.0, "", category, "low", "",
                        account=(tx.get("account") or fallback_account),
                        subcategory=subcategory,
                        tx_type=tx_type,
                    )
                    seen_merchants.add(merchant)
                continue
            if merchant in seen_merchants:
                continue
            account = (tx.get("account") or "").strip() or fallback_account
            amount = float(tx.get("amount", 0))
            tx_id = db.add_parsed_transaction(
                session_id, user_id,
                merchant,
                amount,
                tx.get("date", ""),
                category,
                conf,
                tx.get("notes", ""),
                account=account,
                subcategory=subcategory,
                tx_type=tx_type,
            )
            if auto_confirm:
                db.update_parsed_transaction(tx_id, confirmed=1)
                auto_confirmed += 1
            seen_merchants.add(merchant)
        except (ValueError, TypeError):
            continue
    for item in unclear:
        if isinstance(item, str):
            m = item.strip()
            if m and m not in seen_merchants:
                db.add_parsed_transaction(
                    session_id, user_id, m, 0.0, "", "", "low", "",
                    account=fallback_account,
                )
                seen_merchants.add(m)
    if auto_confirmed > 0:
        await busy.edit_text(f"\u2705 {auto_confirmed} recurring transaction(s) auto-confirmed from learned rules.\n\U0001f9d1\u200d\U0001f4bb Checking unclear items...")
    else:
        await busy.edit_text("\U0001f50d Checking unclear items via web search...")
    unclear_txs = [t for t in db.get_parsed_transactions(session_id) if t["confidence"] == "low" and t["confirmed"] != 1 and t["merchant"]]
    identified_count = 0
    if unclear_txs:
        unclear_names = list(dict.fromkeys(t["merchant"] for t in unclear_txs))  # unique, preserve order
        batch_result = ai.batch_identify_merchants(unclear_names, _load_rulebook(user_id), statement_text)
        for ident in batch_result.get("identifications", []):
            ic = (ident.get("confidence") or "low").strip().lower()
            if ic == "low":
                continue
            im = (ident.get("merchant") or "").strip()
            icat = (ident.get("category") or "").strip()
            isub = (ident.get("subcategory") or "").strip()
            iacct = (ident.get("account") or "").strip()
            itype = (ident.get("tx_type") or "Expense").strip().capitalize()
            if itype not in ("Expense", "Income"):
                itype = "Expense"
            if icat and not _validate_category(icat, itype, expense_cats, income_cats):
                continue
            update_fields = {"merchant": im, "category": icat, "subcategory": isub, "confidence": ic, "tx_type": itype}
            if iacct:
                iacct = _normalize_account(iacct, user_id) or iacct
                update_fields["account"] = iacct
            for utx in unclear_txs:
                if utx["merchant"].lower() == ident.get("original_name", "").lower():
                    db.update_parsed_transaction(utx["id"], **update_fields)
            db.add_transaction_rule(user_id, im, icat if not isub else f"{icat}/{isub}")
            identified_count += 1
    text, keyboard = _render_parse_session(session_id)
    prefix = ""
    if auto_confirmed:
        prefix += f"\u2705 {auto_confirmed} auto-confirmed (recurring)\n"
    if identified_count:
        prefix += f"\U0001f50d {identified_count} identified via AI\n"
    if invalid_cats:
        prefix += f"\u26a0\ufe0f {len(invalid_cats)} had invalid categories (flagged)\n"
    if prefix:
        await busy.edit_text(prefix, reply_markup=None)
        await busy.reply_text(text, reply_markup=keyboard)
    else:
        await busy.edit_text(text, reply_markup=keyboard)
    _parse_msg[user_id] = busy.message_id
    if due_date:
        try:
            iso = _ddmmyy_to_iso(due_date)
            if iso:
                card_name = _detect_card_name(statement_text)
                total = sum(float(t.get("amount", 0)) for t in transactions)
                _pending_bill_from_parse[user_id] = {
                    "card_name": card_name,
                    "card_last4": _detect_card_last4(statement_text),
                    "amount": round(total, 2),
                    "due_date": iso,
                    "session_id": session_id,
                }
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("\U0001f4be Save as bill", callback_data="parse_billsave"),
                     InlineKeyboardButton("\u23ed Skip", callback_data="parse_billskip")],
                ])
                card_label = f"{card_name.upper()} " if card_name else ""
                last4_label = f"({ _pending_bill_from_parse[user_id]['card_last4']}) " if _pending_bill_from_parse[user_id]["card_last4"] else ""
                await update.effective_message.reply_text(
                    f"\U0001f514 Detected due date: {_fmt_ddmmyy(due_date)}. Save as bill? [{card_label}{last4_label}${total:.2f}]",
                    reply_markup=kb,
                )
        except Exception:
            pass


async def parse_statement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if args:
        statement_text = " ".join(args)
        await handle_statement_parse(update, context, user_id, statement_text)
    else:
        _pending_statement[user_id] = True
        await update.message.reply_text(
            "\U0001f4c4 Send me your statement now — paste text, or upload a .txt / .csv / .pdf file.\n\n"
            "PDF bank/credit card statements are auto-read.\n"
            "Send /cancel to abort."
        )


async def handle_finance_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    doc = update.message.document
    if not doc:
        return
    fname = (doc.file_name or "").lower()
    is_text = fname.endswith(".txt") or fname.endswith(".csv") or fname.endswith(".tsv")
    is_pdf = fname.endswith(".pdf")
    if not is_text and not is_pdf:
        return
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large (max 10MB).")
        return
    busy = await update.message.reply_text("\U0001f4c4 Reading file...")
    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.error(f"Document download failed: {e}")
        await busy.edit_text("Could not download that file.")
        return
    if is_pdf:
        await busy.edit_text("\U0001f4c4 Extracting text from PDF...")
        content = _extract_pdf_text(file_bytes)
        if not content.strip():
            await busy.edit_text("Could not extract any text from this PDF. It may be a scanned image (OCR not supported) or empty.")
            return
    else:
        try:
            content = bytes(file_bytes).decode("utf-8")
        except UnicodeDecodeError:
            content = bytes(file_bytes).decode("latin-1", errors="replace")
    _pending_statement.pop(user_id, None)
    if user_id in _merge_buffer:
        _merge_buffer[user_id].append(content)
        n = len(_merge_buffer[user_id])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 Parse all now", callback_data="merge_parse_now")],
            [InlineKeyboardButton("\u274c Cancel merge", callback_data="merge_cancel")],
        ])
        await busy.edit_text(
            f"\U0001f4e6 Added to merge buffer ({n} statement(s) buffered).\n"
            f"Upload more files, or tap Parse all now to process them together as one session.",
            reply_markup=kb,
        )
        return
    await busy.delete()
    await handle_statement_parse(update, context, user_id, content)


async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    _merge_buffer[user_id] = []
    await update.message.reply_text(
        "\U0001f4e6 Merge mode ON.\n\n"
        "Upload your statements now (.pdf / .txt / .csv) — one by one or all at once.\n"
        "When done, tap *Parse all now* (or send /mergeparse) to process them together as one session.\n"
        "Send /mergecancel to abort."
    )


async def merge_parse_now(update_or_query, context, user_id=None):
    if hasattr(update_or_query, 'answer') and hasattr(update_or_query, 'edit_message_text'):
        query = update_or_query
        await query.answer()
        uid = query.from_user.id
        if uid != OWNER_TELEGRAM_ID:
            return
        buffers = _merge_buffer.pop(uid, [])
        if not buffers:
            await query.edit_message_text("No statements in merge buffer.")
            return
        await query.edit_message_text(f"\U0001f4e6 Parsing {len(buffers)} merged statement(s)...")
        combined = "\n\n--- STATEMENT BREAK ---\n\n".join(buffers)
        await handle_statement_parse(None, context, uid, combined, override_msg=query.message)
    else:
        update = update_or_query
        uid = update.effective_user.id
        if uid != OWNER_TELEGRAM_ID:
            return
        buffers = _merge_buffer.pop(uid, [])
        if not buffers:
            await update.message.reply_text("No statements in merge buffer. Use /merge first, then upload files.")
            return
        msg = await update.message.reply_text(f"\U0001f4e6 Parsing {len(buffers)} merged statement(s)...")
        combined = "\n\n--- STATEMENT BREAK ---\n\n".join(buffers)
        await handle_statement_parse(update, context, uid, combined, override_msg=msg)


async def merge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = query.from_user.id
    if uid != OWNER_TELEGRAM_ID:
        await query.answer()
        return
    if data == "merge_parse_now":
        await merge_parse_now(query, context)
    elif data == "merge_cancel":
        await query.answer()
        n = len(_merge_buffer.pop(uid, []))
        await query.edit_message_text(f"\u274c Merge cancelled ({n} statement(s) discarded).")


async def merge_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    n = len(_merge_buffer.pop(user_id, []))
    await update.message.reply_text(f"\u274c Merge cancelled ({n} statement(s) discarded).")


async def parse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    data = query.data
    if data == "parse_noop":
        return
    if data == "parse_reparse_new":
        _pending_statement[user_id] = True
        await query.edit_message_text("Send the statement text again to re-parse.")
        return
    if data.startswith("parse_show_"):
        sid = int(data.split("_")[2])
        text, kb = _render_parse_session(sid)
        if text:
            await query.edit_message_text(text, reply_markup=kb)
        return
    if data.startswith("parse_confirm_all_"):
        sid = int(data.split("_")[3])
        session = db.get_parse_session(sid)
        if not session:
            await query.edit_message_text("Session not found.")
            return
        txs = db.get_parsed_transactions(sid)
        for tx in txs:
            if tx["confirmed"] == -1:
                continue
            if tx["confidence"] == "low":
                continue
            db.update_parsed_transaction(tx["id"], confirmed=1)
        tsv = _generate_tsv(sid)
        db.update_parse_session_status(sid, "confirmed")
        n = sum(1 for t in txs if t["confirmed"] == 1 or (t["confirmed"] != -1 and t["confidence"] != "low"))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tsv", delete=False, encoding="utf-8") as f:
            f.write(tsv)
            tmp_path = f.name
        try:
            with open(tmp_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=f,
                    filename="import.tsv",
                    caption=f"\U0001f4e5 {n} transactions exported. Import this into your expense manager.",
                )
        finally:
            os.unlink(tmp_path)
        unclear_left = sum(1 for t in db.get_parsed_transactions(sid) if t["confidence"] == "low" and t["confirmed"] != -1)
        msg = f"\u2705 Confirmed {n} transaction(s). TSV file sent above. Session marked as confirmed."
        if unclear_left:
            msg += f"\n\u26a0\ufe0f {unclear_left} unclear item(s) skipped (review them individually)."
        await query.edit_message_text(msg)
        _parse_msg.pop(user_id, None)
        return
    if data.startswith("parse_cancel_"):
        sid = int(data.split("_")[2])
        db.update_parse_session_status(sid, "cancelled")
        db.delete_parse_session(sid)
        _parse_msg.pop(user_id, None)
        await query.edit_message_text("\u274c Parse session cancelled.")
        return
    if data.startswith("parse_retry_"):
        sid = int(data.split("_")[2])
        db.update_parse_session_status(sid, "cancelled")
        db.delete_parse_session(sid)
        _parse_statement[user_id] = True
        _parse_msg.pop(user_id, None)
        await query.edit_message_text("\U0001f501 Session cleared. Send the statement text again (paste or upload a file).")
        return
    if data.startswith("parse_confirm_"):
        tx_id = int(data.split("_")[2])
        db.update_parsed_transaction(tx_id, confirmed=1)
        tx = db.get_parsed_transaction(tx_id)
        if tx:
            text, kb = _render_parse_session(tx["session_id"])
            if text:
                await query.edit_message_text(text, reply_markup=kb)
        return
    if data.startswith("parse_reject_"):
        tx_id = int(data.split("_")[2])
        tx = db.get_parsed_transaction(tx_id)
        if not tx:
            return
        db.update_parsed_transaction(tx_id, confirmed=-1)
        text, kb = _render_parse_session(tx["session_id"])
        if text:
            await query.edit_message_text(text, reply_markup=kb)
        return
    if data.startswith("parse_edit_"):
        tx_id = int(data.split("_")[2])
        _parse_edit[user_id] = tx_id
        tx = db.get_parsed_transaction(tx_id)
        rulebook = _load_rulebook(user_id)
        suggestion = ai.batch_identify_merchants([tx["merchant"]], rulebook, tx.get("notes", ""))
        if suggestion.get("identifications"):
            ident = suggestion["identifications"][0]
            sc = (ident.get("confidence") or "low").strip().lower()
            if sc != "low":
                desc = ident.get("description", "").strip()
                cat = ident.get("category", "") or tx.get("category", "") or "?"
                sub = ident.get("subcategory", "") or tx.get("subcategory", "") or ""
                acct = ident.get("account", "") or tx.get("account", "") or "?"
                cat_display = f"{cat}/{sub}" if cat and sub else (cat or "?")
                hint = f"\n💡 I think this is: {ident.get('merchant', tx['merchant'])}" if ident.get('merchant') else ""
                await query.edit_message_text(
                    f"Edit: {tx['merchant']}{hint}\n"
                    f"💳 {acct} → {cat_display}\n"
                    f"📝 {desc}\n\n"
                    "Reply with a correction, e.g.:\n"
                    "\"it's Si Si Nan Chun, Food/Eating Out\"\n"
                    "\"Category: Health\"\n"
                    "\"confirm\" to accept the suggestion",
                )
                return
        await query.edit_message_text(
            f"Edit: {tx['merchant']}\n\n"
            "Send what it should be, e.g.:\n"
            "\"it's Si Si Nan Chun, Food/Eating Out\"\n"
            "\"Category: Health/Health\"\n"
            "\"Amount: 12.34\"",
        )
        return
    if data.startswith("parse_identify_"):
        tx_id = int(data.split("_")[2])
        tx = db.get_parsed_transaction(tx_id)
        if not tx:
            await query.answer("Transaction not found.", show_alert=True)
            return
        await query.edit_message_text(f"\U0001f50d Searching the web to identify \"{tx['merchant']}\"...")
        rulebook = _load_rulebook(user_id)
        result = ai.identify_merchant(tx["merchant"], rulebook)
        if not result:
            await query.edit_message_text(f"Could not identify \"{tx['merchant']}\". Try editing it manually.")
            return
        conf = (result.get("confidence") or "low").strip().lower()
        new_merchant = (result.get("merchant") or tx["merchant"]).strip()
        new_category = (result.get("category") or "").strip()
        new_subcategory = (result.get("subcategory") or "").strip()
        new_account = (result.get("account") or "").strip()
        if new_account:
            new_account = _normalize_account(new_account, user_id) or new_account
        new_tx_type = (result.get("tx_type") or "Expense").strip().capitalize()
        if new_tx_type not in ("Expense", "Income"):
            new_tx_type = "Expense"
        description = (result.get("description") or "").strip()
        if conf != "low" and new_category:
            update_fields = {"merchant": new_merchant, "category": new_category, "subcategory": new_subcategory, "tx_type": new_tx_type, "confidence": conf}
            if new_account:
                update_fields["account"] = new_account
            db.update_parsed_transaction(tx_id, **update_fields)
            db.add_transaction_rule(user_id, new_merchant, new_category)
            note_line = f"\U0001f50d Identified: {description}\n" if description else ""
            acct_line = f"\nAccount: {new_account}" if new_account else ""
            subcat_line = f"\nSubcategory: {new_subcategory}" if new_subcategory else ""
            type_line = f"\nType: {new_tx_type}"
            cat_display = f"{new_category}/{new_subcategory}" if new_subcategory else new_category
            await query.edit_message_text(
                f"{note_line}\u2705 Identified as: {new_merchant}\nCategory: {cat_display}{acct_line}{type_line}\nConfidence: {conf}\n\nNoted! I'll remember that for next time.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("\u2190 Back to session", callback_data=f"parse_show_{tx['session_id']}"),
                ]]),
            )
        else:
            note_line = f"\n\U0001f4ac {description}" if description else ""
            await query.edit_message_text(
                f"Could not confidently identify \"{tx['merchant']}\".{note_line}\n\nTry editing it manually with the Edit button.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("\u2190 Back to session", callback_data=f"parse_show_{tx['session_id']}"),
                ]]),
            )
        return
    if data == "parse_billsave":
        pending = _pending_bill_from_parse.pop(user_id, None)
        if not pending:
            await query.edit_message_text("No pending bill.")
            return
        db.add_bill(user_id, pending["card_name"], pending["amount"], pending["due_date"], pending["card_last4"])
        await query.edit_message_text(f"\u2705 Saved as bill: ${pending['amount']:.2f} due {_fmt_ddmmyy_to_display(pending['due_date'])}")
        return
    if data == "parse_billskip":
        _pending_bill_from_parse.pop(user_id, None)
        await query.edit_message_text("\u23ed Skipped saving as bill.")
        return


def _fmt_ddmmyy_to_display(iso_date):
    if not iso_date:
        return ""
    try:
        d = date.fromisoformat(iso_date)
        return d.strftime("%d/%m/%y")
    except Exception:
        return iso_date


async def add_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/addbill OCBC 2097 400 6 May`\n"
            "or: `/addbill OCBC $400 due 6/5`",
            parse_mode="Markdown",
        )
        return
    text = " ".join(args)
    busy = await update.message.reply_text("\U0001f9d1\u200d\U0001f4bb Parsing bill input...")
    result = ai.parse_bill_input(text)
    if result.get("error"):
        await busy.edit_text(result["error"])
        return
    iso = _ddmmyy_to_iso(result["due_date"])
    if not iso:
        await busy.edit_text("Could not parse the due date. Try: `/addbill OCBC 2097 400 6 May`")
        return
    _pending_bill[user_id] = {
        "card_name": result["card_name"],
        "card_last4": result["card_last4"],
        "amount": float(result["amount"]),
        "due_date": iso,
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Confirm", callback_data="bill_confirm"),
         InlineKeyboardButton("\u274c Cancel", callback_data="bill_cancel")],
    ])
    await busy.edit_text(
        f"Add this bill?\n\n"
        f"\U0001f4b3 Card: {result['card_name']} {result['card_last4']}\n"
        f"\U0001f4b0 Amount: ${float(result['amount']):.2f}\n"
        f"\U0001f4c5 Due: {_fmt_ddmmyy_to_display(iso)}",
        reply_markup=keyboard,
    )


async def bill_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    data = query.data
    if data == "bill_confirm":
        pending = _pending_bill.pop(user_id, None)
        if not pending:
            await query.edit_message_text("No pending bill.")
            return
        db.add_bill(user_id, pending["card_name"], pending["amount"], pending["due_date"], pending["card_last4"])
        if pending.get("card_last4"):
            db.add_card(user_id, pending["card_name"], pending["card_last4"])
        await query.edit_message_text(
            f"\u2705 Added: {pending['card_name'].upper()} (${pending['amount']:.2f} due {_fmt_ddmmyy_to_display(pending['due_date'])})"
        )
        return
    if data == "bill_cancel":
        _pending_bill.pop(user_id, None)
        await query.edit_message_text("\u274c Bill addition cancelled.")
        return


def _render_bills(user_id):
    bills = db.get_bills(user_id)
    if not bills:
        return None, None
    today = date.today()
    lines = ["\U0001f4cb Upcoming Bills"]
    for b in bills:
        try:
            due = date.fromisoformat(b["due_date"])
        except Exception:
            due = today
        days_left = (due - today).days
        if days_left < 0:
            icon = "\u26a0\ufe0f"
            when = f"overdue {abs(days_left)}d"
        elif days_left < 7:
            icon = "\U0001f534"
            when = f"{days_left}d left"
        elif days_left < 30:
            icon = "\U0001f7e1"
            when = f"{days_left}d left"
        else:
            icon = "\U0001f7e2"
            when = f"{days_left}d left"
        last4 = b.get("card_last4") or ""
        if not last4:
            last4 = db.get_card_last4(user_id, b["card_name"])
        last4_str = f" **{last4}**" if last4 else ""
        lines.append(f"{icon} #{b['id']} {b['card_name'].upper()}{last4_str}  ${b['amount']:.2f}  \u2014 due {due.strftime('%b %-d')} ({when})")
    total = sum(b["amount"] for b in bills)
    lines.append(f"Total: ${total:.2f}")
    keyboard = []
    row = []
    for b in bills:
        row.append(InlineKeyboardButton(str(b["id"]), callback_data=f"billpay_{b['id']}"))
        if len(row) >= 8:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def list_bills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    text, kb = _render_bills(user_id)
    if not text:
        await update.message.reply_text("\U0001f4cb No unpaid bills. You're all caught up!")
        return
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def pay_bill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/pay <bill_id>`", parse_mode="Markdown")
        return
    bill_id = int(args[0])
    bill = db.get_bill(bill_id)
    if not bill or bill["user_id"] != user_id:
        await update.message.reply_text("Bill not found.")
        return
    if bill["paid"]:
        await update.message.reply_text("That bill is already paid.")
        return
    db.pay_bill(bill_id)
    try:
        due = date.fromisoformat(bill["due_date"]).strftime("%b %-d")
    except Exception:
        due = bill["due_date"]
    await update.message.reply_text(f"\u2705 Paid: {bill['card_name'].upper()} (${bill['amount']:.2f} due {due})")


async def remove_bill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/removebill <bill_id>`", parse_mode="Markdown")
        return
    bill_id = int(args[0])
    bill = db.get_bill(bill_id)
    if not bill or bill["user_id"] != user_id:
        await update.message.reply_text("Bill not found.")
        return
    db.remove_bill(bill_id)
    await update.message.reply_text(f"\U0001f5d1 Removed bill #{bill_id}: {bill['card_name'].upper()} (${bill['amount']:.2f})")


async def add_voucher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text("Usage: `/addvoucher PARADISE, $10 off $50, 30th June`\nExpiry is optional — `/addvoucher NAME, details` works too.", parse_mode="Markdown")
        return
    raw = " ".join(args)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        await update.message.reply_text("Need at least 2 comma-separated fields: name, details [, expiry date]")
        return
    name = parts[0]
    details = parts[1]
    details = re.sub(r'(?<!\$)\b(\d+)\b(?!\s*%)', r'$\1', details)
    parsed = None
    if len(parts) >= 3:
        expiry_raw = ", ".join(parts[2:])
        parsed = _parse_expiry(expiry_raw)
    db.add_voucher(user_id, name, details, parsed)
    if parsed:
        try:
            due = date.fromisoformat(parsed).strftime("%-d %b %Y")
        except Exception:
            due = parsed
        await update.message.reply_text(f"\u2705 Added voucher: **{name}** — {details} (exp {due})", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"\u2705 Added voucher: **{name}** — {details} (no expiry)", parse_mode="Markdown")


async def vouchers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    vouchers = db.get_active_vouchers(user_id)
    if not vouchers:
        await update.message.reply_text("No active vouchers.")
        return
    today = date.today()
    name_count = {}
    for v in vouchers:
        name_count[v["name"]] = name_count.get(v["name"], 0) + 1
    seen = {}
    lines = ["\U0001f39f Your Vouchers", ""]
    buttons = []
    for idx, v in enumerate(vouchers, 1):
        exp_str = v.get("expiry_date")
        if exp_str:
            try:
                exp = date.fromisoformat(exp_str)
                remaining = (exp - today).days
                exp_str = exp.strftime("%-d %b %Y")
                days_str = f" ({remaining}d left)" if remaining >= 0 else " (expired)"
            except Exception:
                days_str = ""
            exp_line = f"   \u26a0 Expires: {exp_str}{days_str}"
        else:
            exp_line = "   \u26a0 No expiry"
        lines.append(f"{idx}. {v['name']}")
        lines.append(f"   \U0001f4cb {v['details']}")
        lines.append(exp_line)
        lines.append("")
        n = v["name"]
        if name_count[n] == 1:
            label = f"\u2705 {n}"
        else:
            seen[n] = seen.get(n, 0) + 1
            hint = (v.get("details") or "").strip()[:10]
            if not hint and not v.get("expiry_date"):
                hint = "no exp"
            elif not hint:
                hint = f"#{seen[n]}"
            label = f"\u2705 {n} ({hint})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"voucher_use_{v['id']}"))
    keyboard = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    await update.message.reply_text("\n".join(lines).strip(), reply_markup=InlineKeyboardMarkup(keyboard))


async def voucher_use_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    vid = int(query.data.split("_")[2])
    v = db.get_active_vouchers(user_id)
    v = next((x for x in v if x["id"] == vid), None)
    db.delete_voucher(vid)
    remaining = db.get_active_vouchers(user_id)
    if not remaining:
        await query.edit_message_text(f"\u2705 Marked **{v['name']}** as used. No active vouchers remaining.", parse_mode="Markdown")
        return
    today = date.today()
    name_count = {}
    for v2 in remaining:
        name_count[v2["name"]] = name_count.get(v2["name"], 0) + 1
    seen = {}
    lines = ["\U0001f39f Your Vouchers", ""]
    buttons = []
    for idx2, v2 in enumerate(remaining, 1):
        es = v2.get("expiry_date")
        if es:
            try:
                exp = date.fromisoformat(es)
                rem = (exp - today).days
                es = exp.strftime("%-d %b %Y")
                days_str = f" ({rem}d left)" if rem >= 0 else " (expired)"
            except Exception:
                days_str = ""
            exp_line = f"   \u26a0 Expires: {es}{days_str}"
        else:
            exp_line = "   \u26a0 No expiry"
        lines.append(f"{idx2}. {v2['name']}")
        lines.append(f"   \U0001f4cb {v2['details']}")
        lines.append(exp_line)
        lines.append("")
        n = v2["name"]
        if name_count[n] == 1:
            label = f"\u2705 {n}"
        else:
            seen[n] = seen.get(n, 0) + 1
            hint = (v2.get("details") or "").strip()[:10]
            if not hint and not v2.get("expiry_date"):
                hint = "no exp"
            elif not hint:
                hint = f"#{seen[n]}"
            label = f"\u2705 {n} ({hint})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"voucher_use_{v2['id']}"))
    keyboard = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    await query.edit_message_text("\n".join(lines).strip(), reply_markup=InlineKeyboardMarkup(keyboard))


async def bill_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    data = query.data
    if data.startswith("billpay_"):
        bill_id = int(data.split("_")[1])
        bill = db.get_bill(bill_id)
        if not bill or bill["user_id"] != user_id:
            await query.edit_message_text("Bill not found.")
            return
        db.pay_bill(bill_id)
        try:
            due = date.fromisoformat(bill["due_date"]).strftime("%b %-d")
        except Exception:
            due = bill["due_date"]
        await query.answer(f"Paid: {bill['card_name'].upper()} ${bill['amount']:.2f}", show_alert=False)
        await query.edit_message_text(f"\u2705 Paid: {bill['card_name'].upper()} (${bill['amount']:.2f} due {due})")
        remaining = db.get_bills(user_id)
        if remaining:
            rtext, rkb = _render_bills(user_id)
            if rtext:
                await context.bot.send_message(chat_id=query.message.chat_id, text=rtext, parse_mode="Markdown", reply_markup=rkb)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text="\U0001f4cb All bills paid. You're all caught up!")
        return
    if data.startswith("billdue_yes_"):
        bill_id = int(data.split("_")[2])
        bill = db.get_bill(bill_id)
        if not bill or bill["user_id"] != user_id:
            await query.edit_message_text("Bill not found.")
            return
        db.pay_bill(bill_id)
        await query.edit_message_text(f"\u2705 Marked as paid: {bill['card_name'].upper()} ${bill['amount']:.2f}")
        return
    if data.startswith("billdue_no_"):
        bill_id = int(data.split("_")[2])
        bill = db.get_bill(bill_id)
        if not bill or bill["user_id"] != user_id:
            await query.edit_message_text("Bill not found.")
            return
        try:
            due = date.fromisoformat(bill["due_date"]).strftime("%b %-d")
        except Exception:
            due = bill["due_date"]
        await query.edit_message_text(f"\u23f3 OK, I'll remind you again tomorrow. ({bill['card_name'].upper()} ${bill['amount']:.2f} due {due})")
        return


async def add_card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: `/addcard <card_name> <last4>`\n"
            "Example: `/addcard CITI 9999`\n"
            "If the card exists, its last 4 digits are updated. If not, a new card is added.",
            parse_mode="Markdown",
        )
        return
    card_name = args[0].strip()
    last4 = args[1].strip()
    if not re.fullmatch(r'\d{4}', last4):
        await update.message.reply_text("Last 4 digits must be exactly 4 numbers. Example: `/addcard CITI 9999`", parse_mode="Markdown")
        return
    existing = db.get_card(user_id, card_name)
    db.add_card(user_id, card_name, last4)
    if existing:
        await update.message.reply_text(f"\u2705 Updated card {card_name.upper()} \u2192 last 4: **{last4}**", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"\u2705 Added card {card_name.upper()} with last 4: **{last4}**", parse_mode="Markdown")


async def list_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    cards = db.get_cards(user_id)
    if not cards:
        await update.message.reply_text("\U0001f4b3 No cards saved. Use `/addcard <name> <last4>` to add one.", parse_mode="Markdown")
        return
    lines = ["\U0001f4b3 Saved Cards", "\u2501" * 23]
    for c in cards:
        last4 = c["card_last4"] or "(no last4)"
        lines.append(f"{c['card_name'].upper()}  \u2192  **{last4}**")
    lines.append("\u2501" * 23)
    lines.append("Used in /bills to show the last 4 digits.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def check_bill_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    chat_id_str = db.get_user_preference(OWNER_TELEGRAM_ID, "chat_id")
    if not chat_id_str:
        return
    chat_id = int(chat_id_str)
    for bill in db.get_bills_due_today(OWNER_TELEGRAM_ID):
        last4 = bill.get("card_last4") or ""
        if not last4:
            last4 = db.get_card_last4(OWNER_TELEGRAM_ID, bill["card_name"])
        last4_str = f" {last4}" if last4 else ""
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2705 Yes, paid", callback_data=f"billdue_yes_{bill['id']}"),
                 InlineKeyboardButton("\u23f3 Not yet", callback_data=f"billdue_no_{bill['id']}")],
            ])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f514 Is {bill['card_name'].upper()}{last4_str} - ${bill['amount']:.2f} paid?",
                reply_markup=kb,
            )
            db.mark_bill_notified_today(bill["id"])
        except Exception as e:
            logger.error(f"Bill due-today notification failed: {e}")
    for bill in db.get_bills_due_soon(OWNER_TELEGRAM_ID, days=3):
        try:
            due = date.fromisoformat(bill["due_date"]).strftime("%b %-d")
        except Exception:
            due = bill["due_date"]
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f514 Bill reminder: {bill['card_name'].upper()} ${bill['amount']:.2f} due {due}",
            )
            db.mark_bill_notified(bill["id"])
        except Exception as e:
            logger.error(f"Bill reminder send failed: {e}")
    for reminder in db.get_monthly_reminders_due(OWNER_TELEGRAM_ID, today.day):
        try:
            await context.bot.send_message(chat_id=chat_id, text=reminder["message"])
            db.mark_monthly_notified(reminder["id"], today.strftime("%Y-%m"))
        except Exception as e:
            logger.error(f"Monthly reminder send failed: {e}")
    # Voucher reminders: monthly (new month) + weekly (once per week, 7 days before expiry)
    today_y = today.year
    today_m = today.month
    tmrw = today.replace(day=1)
    if today.day == 1:
        for v in db.get_vouchers_expiring_in_month(OWNER_TELEGRAM_ID, today_y, today_m):
            try:
                exp = date.fromisoformat(v["expiry_date"]).strftime("%-d %b %Y")
            except Exception:
                exp = v["expiry_date"]
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"\U0001f39f Voucher expiring this month: **{v['name']}** ({v['details']}) \u2014 expires {exp}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Monthly voucher reminder failed: {e}")
    current_week = today.isocalendar()[1]
    last_week = db.get_user_preference(OWNER_TELEGRAM_ID, "voucher_weekly_week")
    if last_week is None or int(last_week) < current_week:
        for v in db.get_vouchers_expiring_soon(OWNER_TELEGRAM_ID, days=7):
            try:
                exp = date.fromisoformat(v["expiry_date"]).strftime("%-d %b %Y")
            except Exception:
                exp = v["expiry_date"]
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"\u26a0 Voucher **{v['name']}** expires {exp}! ({v['details']})",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Weekly voucher reminder failed: {e}")
        db.set_user_preference(OWNER_TELEGRAM_ID, "voucher_weekly_week", str(current_week))


async def ask_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    recipe_text = _last_suggestion.get(user_id, "")
    _asking_question[user_id] = recipe_text
    await query.message.reply_text("May I know your question? (✿◠‿◠)")


async def question_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /question <your question>")
        return
    msg = await update.message.reply_text("Let me think about that... (˶˃ ᵕ ˂˶)")
    answer = ai.chef_chat(text)
    await msg.edit_text(_sanitize_markdown(answer), parse_mode="Markdown")


async def cancel_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _pending_statement.pop(user_id, None)
    _parse_edit.pop(user_id, None)
    _pending_bill.pop(user_id, None)
    _pending_bill_from_parse.pop(user_id, None)
    _asking_question.pop(user_id, None)
    await update.message.reply_text("\u274c Cancelled.")


async def reload_rulebook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    rb_path = os.path.join(os.path.dirname(__file__), "money_manager.md")
    if not os.path.exists(rb_path):
        await update.message.reply_text("money_manager.md not found in the project folder.")
        return
    try:
        with open(rb_path, "r", encoding="utf-8") as f:
            content = f.read()
        db.set_user_preference(user_id, "rulebook_md", content)
        account_names = _get_account_names(user_id)
        await update.message.reply_text(
            f"\u2705 Rulebook reloaded from money_manager.md.\n"
            f"Account names: {', '.join(account_names)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Could not reload rulebook: {e}")


async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    if user_id != OWNER_TELEGRAM_ID:
        return
    rules = db.get_transaction_rules(user_id)
    if not rules:
        await update.message.reply_text(
            "\U0001f4dc No learned rules yet.\n\n"
            "Rules are added automatically when you:\n"
            "- Correct a transaction's category (Edit)\n"
            "- Tap \U0001f50d Identify on an unclear merchant"
        )
        return
    lines = ["\U0001f4dc Learned Merchant Rules", "\u2501" * 23]
    keyboard = []
    for r in rules:
        lines.append(f"#{r['id']}  {r['merchant_pattern']}  \u2192  {r['category']}")
    lines.append("\u2501" * 23)
    lines.append(f"{len(rules)} rule(s). Tap \U0001f5d1 to delete a rule.")
    for r in rules:
        keyboard.append([
            InlineKeyboardButton(f"{r['merchant_pattern']} \u2192 {r['category']}", callback_data="rule_noop"),
            InlineKeyboardButton("\U0001f5d1", callback_data=f"delrule_{r['id']}"),
        ])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def delrule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    data = query.data
    if data == "rule_noop":
        return
    if data.startswith("delrule_"):
        rule_id = int(data.split("_")[1])
        db.delete_transaction_rule(rule_id)
        rules = db.get_transaction_rules(user_id)
        if not rules:
            await query.edit_message_text("\u2705 Rule deleted. No rules left.")
            return
        lines = ["\U0001f4dc Learned Merchant Rules", "\u2501" * 23]
        keyboard = []
        for r in rules:
            lines.append(f"#{r['id']}  {r['merchant_pattern']}  \u2192  {r['category']}")
        lines.append("\u2501" * 23)
        lines.append(f"{len(rules)} rule(s). Tap \U0001f5d1 to delete a rule.")
        for r in rules:
            keyboard.append([
                InlineKeyboardButton(f"{r['merchant_pattern']} \u2192 {r['category']}", callback_data="rule_noop"),
                InlineKeyboardButton("\U0001f5d1", callback_data=f"delrule_{r['id']}"),
            ])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    err = context.error
    msg = str(err) if err else "Something went wrong. (,,>﹏<,,)"
    friendly = None
    if "rate limit" in msg.lower() or "ai is temporarily unavailable" in msg.lower():
        friendly = msg
    elif "daily request limit" in msg.lower():
        friendly = msg
    if update and update.effective_message:
        try:
            if friendly:
                await update.effective_message.reply_text(friendly)
            else:
                await update.effective_message.reply_text("Something went wrong. Try again later. (,,>﹏<,,)")
        except Exception:
            pass


def create_app(token: str):
    app = (
        Application.builder()
        .token(token)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_pantry))
    app.add_handler(CommandHandler("remove", remove_pantry))
    app.add_handler(CommandHandler("pantry", show_pantry))
    app.add_handler(CommandHandler("expiring", expiring))
    app.add_handler(CommandHandler("recategorize", recategorize))
    app.add_handler(CommandHandler("sort", sort_pantry))
    app.add_handler(CommandHandler("set", set_pref))
    app.add_handler(CommandHandler("equipment", equipment))
    app.add_handler(CommandHandler("addequipment", addequipment))
    app.add_handler(CommandHandler("suggest", suggest))
    app.add_handler(CommandHandler("improvise", improvise))
    app.add_handler(CommandHandler("save", save_recipe))
    app.add_handler(CommandHandler("recipes", list_recipes))
    app.add_handler(CommandHandler("recipe", list_recipes))  # alias
    app.add_handler(CommandHandler("view", view_recipe))
    app.add_handler(CommandHandler("delete", delete_recipe))
    app.add_handler(CommandHandler("canbake", canbake))
    app.add_handler(CommandHandler("log", log_meal))
    app.add_handler(CommandHandler("daily", daily_tracking))
    app.add_handler(CommandHandler("nutrition", nutrition))
    app.add_handler(CommandHandler("goal", goal))
    app.add_handler(CommandHandler("weekly", weekly))
    app.add_handler(CommandHandler("shopping", shopping))
    app.add_handler(CommandHandler("shop", shop_add))
    app.add_handler(CommandHandler("list", list_shopping))
    app.add_handler(CommandHandler("shopremove", shop_remove))
    app.add_handler(CommandHandler("shopclear", shop_clear))
    app.add_handler(CommandHandler("addmember", addmember))
    app.add_handler(CommandHandler("removemember", removemember))
    app.add_handler(CommandHandler("members", members))
    app.add_handler(CommandHandler("diet", diet))
    app.add_handler(CommandHandler("expiryremind", expiry_remind))
    app.add_handler(CommandHandler("export", export_recipe))
    app.add_handler(CommandHandler("import", import_recipe))
    app.add_handler(CommandHandler("batch", batch_start))
    app.add_handler(CommandHandler("random", random_ingredient))
    app.add_handler(CommandHandler("randomreset", random_reset))
    app.add_handler(CommandHandler("tokens", tokens_command))
    app.add_handler(CommandHandler("cookmode", cookmode))
    app.add_handler(CommandHandler("cook", cookmode))  # alias
    app.add_handler(CallbackQueryHandler(elaborate_callback, pattern="^elaborate_[0-4]$"))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(cooked_callback, pattern=r"^(cooked|cooked_\d+)$"))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern="^(stats_|reset_stats_|stats_back)"))
    app.add_handler(CallbackQueryHandler(view_cooked_callback, pattern="^viewcooked_"))
    app.add_handler(CallbackQueryHandler(suggest_callback, pattern="^(save_last|suggest_again|export_last|canbake_again)$"))
    app.add_handler(CallbackQueryHandler(batch_callback, pattern="^batch_"))
    app.add_handler(CallbackQueryHandler(cook_callback, pattern="^cook_next$"))
    app.add_handler(CallbackQueryHandler(cook_done_callback, pattern="^cook_done$"))
    app.add_handler(CallbackQueryHandler(cook_from_recipe_callback, pattern="^cook_recipe$"))
    app.add_handler(CallbackQueryHandler(export_batch_callback, pattern="^export_batch$"))
    app.add_handler(CallbackQueryHandler(random_regenerate_callback, pattern="^random_regenerate$"))
    app.add_handler(CallbackQueryHandler(random_switch_callback, pattern="^random_switch$"))
    app.add_handler(CallbackQueryHandler(delete_recipe_callback, pattern="^delrecipe_"))
    app.add_handler(CallbackQueryHandler(view_recipe_callback, pattern="^viewrecipe_"))
    app.add_handler(CallbackQueryHandler(calc_callback, pattern="^calc_"))
    app.add_handler(CallbackQueryHandler(shop_callback, pattern="^shop_"))
    app.add_handler(CallbackQueryHandler(receipt_confirm_callback, pattern="^receipt_confirm$"))
    app.add_handler(CommandHandler("parse", parse_statement))
    app.add_handler(CommandHandler("addbill", add_bill))
    app.add_handler(CommandHandler("bills", list_bills))
    app.add_handler(CommandHandler("pay", pay_bill_cmd))
    app.add_handler(CommandHandler("removebill", remove_bill_cmd))
    app.add_handler(CommandHandler("addcard", add_card_cmd))
    app.add_handler(CommandHandler("cards", list_cards))
    app.add_handler(CommandHandler("reloadrulebook", reload_rulebook))
    app.add_handler(CommandHandler("listrules", list_rules))
    app.add_handler(CommandHandler("merge", merge_command))
    app.add_handler(CommandHandler("mergeparse", merge_parse_now))
    app.add_handler(CommandHandler("mergecancel", merge_cancel))
    app.add_handler(CommandHandler("addvoucher", add_voucher_cmd))
    app.add_handler(CommandHandler("vouchers", vouchers_cmd))
    app.add_handler(CallbackQueryHandler(voucher_use_callback, pattern="^voucher_use_"))
    app.add_handler(CommandHandler("question", question_command))
    app.add_handler(CallbackQueryHandler(ask_question_callback, pattern="^ask_question$"))
    app.add_handler(CommandHandler("cancel", cancel_pending))
    app.add_handler(CallbackQueryHandler(parse_callback, pattern="^parse_"))
    app.add_handler(CallbackQueryHandler(bill_callback, pattern="^bill_(confirm|cancel)$"))
    app.add_handler(CallbackQueryHandler(bill_pay_callback, pattern="^bill(pay|due_yes|due_no)_"))
    app.add_handler(CallbackQueryHandler(delrule_callback, pattern="^(delrule_|rule_noop$)"))
    app.add_handler(CallbackQueryHandler(merge_callback, pattern="^merge_"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_finance_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.add_error_handler(error_handler)

    import datetime as _dt
    try:
        app.job_queue.run_daily(check_bill_reminders, time=_dt.time(hour=9, minute=0))
    except Exception as e:
        logger.error(f"Could not schedule bill reminder job: {e}")

    return app
