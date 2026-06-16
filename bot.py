import logging
logging.getLogger("duckduckgo_search").setLevel(logging.WARNING)
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

import database as db
import ai
from config import OWNER_TELEGRAM_ID

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Store the last AI-suggested recipe text and user prompt per user
_last_suggestion = {}
_last_preference = {}
_last_menu = {}  # user_id -> [{"title": ..., "description": ..., "search_query": ...}]
_menu_history = {}  # user_id -> set of dish titles seen across all regenerations

# Batch cooking state
_batch_state = {}  # user_id -> {"count": int, "cuisine": str, "menu": [...], "selected": set(int), "msg_id": int}
_batch_history = {}  # user_id -> set of dish titles seen across batch regenerations

# Cooking mode state
_cook_state = {}  # user_id -> {"title": str, "ingredients": list[str], "steps": list[str], "shown": int, "msg_id": int}

# Pending cooked dish tracking
_pending_cooked = {}  # user_id -> dish_name (str) or list of dish names (batch)

# Batch export state
_batch_export = {}  # user_id -> {"step": "awaiting_input"}

# Random ingredient country tracker
_random_country = {}  # user_id -> country string

# Photo tracking
_photo_counts = {}  # user_id -> date
PHOTO_DAILY_LIMIT = 30
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB

# Per-user AI call tracking
_ai_counts = {}  # user_id -> count per day
_ai_count_date = date.today()
AI_DAILY_LIMIT = 1500


def _check_ai_limit(user_id):
    global _ai_count_date, _ai_counts
    today = date.today()
    if today != _ai_count_date:
        _ai_counts = {}
        _ai_count_date = today
    if user_id == OWNER_TELEGRAM_ID:
        return
    _ai_counts.setdefault(user_id, 0)
    _ai_counts[user_id] += 1
    if _ai_counts[user_id] > AI_DAILY_LIMIT:
        raise RuntimeError("You've reached your daily request limit (5,000). Try again tomorrow.")


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


# Track whether the recipe in _last_suggestion is already saved (hide Save button)
_is_saved_recipe = {}

# Receipt scan state
_receipt_items = {}  # {user_id: {"items": [{"name": str, "expiry": str}, ...], "msg_id": int}}


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

    m = re.search(r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+(\d{2,4}))?', text)
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
        "  /expiryremind on/off             toggle monthly expiry reminders\n\n"
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
        "  /calories                        today's nutrition totals\n"
        "  /nutrition chicken               lookup per 100g\n"
        "  /goal 1900 120 2300              set daily targets\n"
        "  /weekly                          weekly summary\n\n"
        "*Settings*\n\n"
        "  /equipment air fryer, oven       save your kitchen gear\n"
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
        _last_preference.pop(user_id, None)
        await update.message.reply_text("Diet profile reset to normal.")
    else:
        db.set_user_preference(user_id, "diet_profile", value)
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
        await update.message.reply_text("Usage: `/remove milk`", parse_mode="Markdown")
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
    equip = db.get_user_preference(user_id, "equipment")
    diet = db.get_user_preference(user_id, "diet_profile")
    extra = f"Equipment: {equip}." if equip else ""
    if diet:
        extra += f" Diet: {diet}."
    prompt = f"{pref}\n{extra}".strip()

    target = update.effective_message
    msg = await target.reply_text("Thinking...")
    menu = ai.generate_menu(pantry, prompt)
    _last_menu[user_id] = menu
    _last_preference[user_id] = pref

    if not menu:
        await msg.edit_text("Could not generate suggestions. Try being more specific.")
        return

    _menu_history[user_id] = {d["title"] for d in menu if d.get("title")}

    lines = ["Here are some ideas:"]
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
    if len(text) <= MAX_MSG_LEN:
        await busy.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return
    # First chunk replaces the "busy" message (no buttons)
    chunks = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = text.rfind("\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = MAX_MSG_LEN
        else:
            split_at += 1
        chunks.append(text[:split_at])
        text = text[split_at:]
    await busy.edit_text(chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:-1]:
        await busy.reply_text(chunk, parse_mode="Markdown")
    await busy.reply_text(chunks[-1], parse_mode="Markdown", reply_markup=keyboard)


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
    equip = db.get_user_preference(user_id, "equipment")
    diet = db.get_user_preference(user_id, "diet_profile")
    prefs = f"{_last_preference.get(user_id, '')}\nEquipment: {equip}.".strip()
    if diet:
        prefs += f" Diet: {diet}."
    busy = await update.effective_message.reply_text(f"Getting details for {dish['title']}...")
    result = ai.elaborate_recipe(dish.get("search_query", dish["title"]), pantry_items, prefs)
    _last_suggestion[user_id] = result
    _is_saved_recipe[user_id] = False
    _pending_cooked[user_id] = dish["title"]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
         InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
        [InlineKeyboardButton("📤 Export", callback_data="export_last"),
         InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
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
        equip = db.get_user_preference(user_id, "equipment")
        diet = db.get_user_preference(user_id, "diet_profile")
        pantry = db.get_pantry_names(user_id)
        prev_pref = _last_preference.get(user_id, "")
        extra = f"Equipment: {equip}." if equip else ""
        if diet:
            extra += f" Diet: {diet}."
        prompt = f"{prev_pref}\n{extra}".strip()
        await query.edit_message_text("Thinking...")
        seen = _menu_history.get(user_id, set())
        hint = f"Absolutely do NOT suggest any of these dishes (already suggested before): {list(seen)}" if seen else ""
        menu = ai.generate_menu(pantry, prompt, diversity_hint=hint)
        _last_menu[user_id] = menu
        if not menu:
            await query.edit_message_text("Could not generate suggestions. Try being more specific.")
            return
        _menu_history[user_id] = seen | {d["title"] for d in menu if d.get("title")}
        lines = ["Here are some ideas:"]
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
        pantry = db.get_pantry_names(user_id)
        if not pantry:
            await query.edit_message_text("Your pantry is empty! Add items first.")
            return
        equip = db.get_user_preference(user_id, "equipment")
        prev_pref = _last_preference.get(user_id, "")
        pref = f"Equipment: {equip}.\n{prev_pref}" if equip else prev_pref
        await query.edit_message_text("Thinking...")
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
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
                 InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
                [InlineKeyboardButton("📤 Export", callback_data="export_last"),
                 InlineKeyboardButton("✅ Cooked!", callback_data="cooked")],
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
        busy = await query.message.reply_text("Refreshing list...")
        await _send_long_message(None, busy, text, markup)
    else:
        await query.message.reply_text("No saved recipes. Try `/suggest` to get some!", parse_mode="Markdown")


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
        db.log_cooked(user_id, title)
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
        for i, (name, cnt) in enumerate(dishes, 1):
            lines.append(f"{i}. {name.title()} ×{cnt}")
        text = "\n".join(lines)
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="stats_back")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_keyboard)

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


async def canbake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update.effective_user.id)
    _check_ai_limit(user_id)
    pantry = db.get_pantry_names(user_id)
    if not pantry:
        await update.effective_message.reply_text("Your pantry is empty! Add items first.")
        return

    msg = await update.effective_message.reply_text("Checking your pantry...")
    equip = db.get_user_preference(user_id, "equipment")
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
    nutrition = ai.estimate_meal_calories(meal_desc)

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


async def calories_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    for log in logs:
        lines.append(f"• {log['meal_name']} — {log['calories']} cal, {log['protein_g']}g protein")
    lines.append("")
    lines.append(f"⚡ *{total_cal} / {goals['daily_calories']} cal* ({cal_pct}%)")
    lines.append(f"`{bar}`")
    lines.append(f"🥩 Protein: {total_protein}g / {goals['daily_protein']}g ({pro_pct}%)")
    lines.append(f"🧂 Sodium: {total_sodium}mg / {goals['daily_sodium']}mg ({sod_pct}%)")

    if total_sodium > goals["daily_sodium"]:
        lines.append("\n⚠️ *High sodium alert!* Watch your salt intake.")

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
    info = ai.nutrition_info(food)

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
    pantry = db.get_pantry_names(user_id)
    recipes = db.get_recipes(user_id)

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
        t_lower = text.lower().strip()
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
            msg = await update.effective_message.reply_text("Let me answer that...")
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
                ])
                sanitized = _sanitize_markdown(text_out)
                await _reply_chunked(update.effective_message, sanitized, parse_mode="Markdown", reply_markup=keyboard)
                return

    # --- NL Processing ---
    msg = await update.effective_message.reply_text("Thinking...")
    result = ai.process_natural_language(text, pantry, recipes)
    action = result.get("action", "chat")
    items = result.get("items", [])
    reply = result.get("message", "")

    if action == "add":
        eff_id = _effective_user_id(user_id)
        expiry = _parse_expiry(result.get("expiry_date", ""))
        db.add_pantry_items(eff_id, items, expiry=expiry)
        pantry_items = db.get_pantry(eff_id)
        names = [i["name"] for i in pantry_items]
        cat_map = ai.categorize_pantry_items(names)
        if cat_map:
            db.recategorize_by_map(eff_id, cat_map)
        await msg.edit_text("Added and sorted!")

    elif action == "remove":
        eff_id = _effective_user_id(user_id)
        for item in items:
            db.remove_pantry_item(eff_id, item)
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
        await calories_today(update, context)

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
        recipe = _last_suggestion.get(user_id)
        if recipe:
            await msg.edit_text("Let me answer that...")
            answer = ai.recipe_followup(recipe, text)
            sanitized = _sanitize_markdown(answer)
            await msg.edit_text(sanitized, parse_mode="Markdown")
        else:
            await msg.edit_text(reply if reply else
                "I can help manage your pantry, suggest recipes, track meals, and more! "
                "Try: 'add chicken and rice to pantry' or 'what can I cook?'")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.store_chat_id(user_id, update.effective_chat.id)
    text = update.message.text.strip()

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

    await _handle_user_text(update, context, user_id, text)


# --- Photo recognition ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.store_chat_id(update.effective_user.id, update.effective_chat.id)
    user_id = update.effective_user.id
    caption = (update.message.caption or "").lower()
    is_receipt = any(w in caption for w in ["receipt", "scan", "recipt"])

    # Photo daily limit check (owner bypass)
    today = date.today()
    _photo_counts.setdefault(user_id, {"date": None, "count": 0})
    if _photo_counts[user_id]["date"] != today:
        _photo_counts[user_id] = {"date": today, "count": 0}
    if user_id != OWNER_TELEGRAM_ID and _photo_counts[user_id]["count"] >= PHOTO_DAILY_LIMIT:
        await update.message.reply_text("Daily photo scan limit reached (30). Try again tomorrow.")
        return
    _photo_counts[user_id]["count"] += 1

    if is_receipt:
        msg = await update.message.reply_text("\U0001f9fe Scanning receipt...")
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            if file.file_size and file.file_size > MAX_PHOTO_SIZE:
                await msg.edit_text("Image too large. Please send a smaller photo (<10MB).")
                return
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
        photo = update.message.photo[-1]
        file = await photo.get_file()
        if file.file_size and file.file_size > MAX_PHOTO_SIZE:
            await msg.edit_text("Image too large. Please send a smaller photo (<10MB).")
            return
        file_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(file_bytes).decode("utf-8")

        info = ai.recognize_food_from_image(b64)

        if "error" in info and info.get("name") == "Unknown":
            await msg.edit_text("Could not identify the food. Try a clearer photo or tell me what you ate.")
            return

        text = (
            f"*{info.get('name', 'Unknown')}*\n"
            f"_{info.get('description', '')}_\n\n"
            f"Per 100g:\n"
            f"Calories: {info.get('calories_per_100g', '?')} kcal\n"
            f"Protein: {info.get('protein_g_per_100g', '?')}g\n"
            f"Carbs: {info.get('carbs_g_per_100g', '?')}g\n"
            f"Fat: {info.get('fat_g_per_100g', '?')}g\n"
            f"Sodium: {info.get('sodium_mg_per_100g', '?')}mg"
        )
        await msg.edit_text(text, parse_mode="Markdown")
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
    keyboard.append([InlineKeyboardButton("🗑 Clear checked", callback_data="shop_clear_checked")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


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
    added = []
    for item in data["items"]:
        db.add_pantry_item(user_id, item["name"], expiry=item.get("expiry", ""))
        added.append(item["name"].title())
    await query.edit_message_text(f"\u2705 Added {len(added)} item(s) to pantry: {', '.join(added)}")


async def random_ingredient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _check_ai_limit(update.effective_user.id)
    user_id = update.effective_user.id
    country = " ".join(context.args).strip() if context.args else ""
    _random_country[user_id] = country
    msg = await update.message.reply_text("Thinking..." if not country else f"Finding a {country} ingredient...")
    try:
        result = ai.suggest_random_ingredient(country)
    except Exception as e:
        await msg.edit_text("Something went wrong. Try again later.")
        return
    if result == "AI error" or result.startswith("Something went wrong"):
        await msg.edit_text("Gemini is temporarily unavailable (high demand). Try again in a moment.")
        return
    _last_suggestion[_effective_user_id(user_id)] = result
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerate", callback_data="random_regenerate")],
    ])
    sanitized = _sanitize_markdown(result)
    await msg.edit_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def random_regenerate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    country = _random_country.get(user_id, "")
    try:
        result = ai.suggest_random_ingredient(country)
    except Exception as e:
        await query.edit_message_text("Something went wrong. Try again later.")
        return
    if result == "AI error" or result.startswith("Something went wrong"):
        await query.edit_message_text("Gemini is temporarily unavailable (high demand). Try again in a moment.")
        return
    _last_suggestion[_effective_user_id(user_id)] = result
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerate", callback_data="random_regenerate")],
    ])
    sanitized = _sanitize_markdown(result)
    await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        msg = str(context.error)
        if "daily request limit" in msg.lower():
            await update.effective_message.reply_text(msg)
        else:
            await update.effective_message.reply_text("Something went wrong. Try again later.")


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
    app.add_handler(CommandHandler("calories", calories_today))
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
    app.add_handler(CommandHandler("cookmode", cookmode))
    app.add_handler(CommandHandler("cook", cookmode))  # alias
    app.add_handler(CallbackQueryHandler(elaborate_callback, pattern="^elaborate_[0-4]$"))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(cooked_callback, pattern=r"^(cooked|cooked_\d+)$"))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern="^(stats_|reset_stats_|stats_back)"))
    app.add_handler(CallbackQueryHandler(suggest_callback, pattern="^(save_last|suggest_again|export_last)$"))
    app.add_handler(CallbackQueryHandler(batch_callback, pattern="^batch_"))
    app.add_handler(CallbackQueryHandler(cook_callback, pattern="^cook_next$"))
    app.add_handler(CallbackQueryHandler(cook_done_callback, pattern="^cook_done$"))
    app.add_handler(CallbackQueryHandler(cook_from_recipe_callback, pattern="^cook_recipe$"))
    app.add_handler(CallbackQueryHandler(export_batch_callback, pattern="^export_batch$"))
    app.add_handler(CallbackQueryHandler(random_regenerate_callback, pattern="^random_regenerate$"))
    app.add_handler(CallbackQueryHandler(delete_recipe_callback, pattern="^delrecipe_"))
    app.add_handler(CallbackQueryHandler(view_recipe_callback, pattern="^viewrecipe_"))
    app.add_handler(CallbackQueryHandler(shop_callback, pattern="^shop_"))
    app.add_handler(CallbackQueryHandler(receipt_confirm_callback, pattern="^receipt_confirm$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.add_error_handler(error_handler)

    return app
