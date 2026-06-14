import logging
logging.getLogger("duckduckgo_search").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)
import base64
import os
import tempfile
import json
import re
from datetime import datetime, date
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

# Track whether the recipe in _last_suggestion is already saved (hide Save button)
_is_saved_recipe = {}


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "SG Chef Bot - your personal kitchen assistant\n\n"
        "Just chat naturally - try:\n"
        '"add chicken and rice to my pantry"\n'
        '"whats in my pantry?"\n'
        '"suggest fried chicken for air fryer"\n'
        '"swap chicken for tofu" (on an elaborated recipe)\n'
        '"make this for 2 people" (scale a recipe)\n'
        '"what can i cook?"\n'
        '"save recipe for braised pork rice"\n'
        '"log chicken rice for lunch"\n'
        '"how many calories today?"\n'
        '"show my recipes"\n'
        '"show recipe 1"\n'
        '"i have an air fryer and rice cooker"\n\n'
        "Or use commands:\n"
        "/pantry - show your pantry\n"
        "/add chicken, rice - add items\n"
        "/remove milk - remove item\n"
        "/equipment air fryer, oven - save your kitchen gear\n"
        "/diet keto - set a diet profile (all suggestions adapt)\n"
        "/diet all - reset to normal diet\n"
        "/recategorize - fix item categories\n"
        "/expiring - items expiring soon\n"
        "/suggest - AI recipe suggestions\n"
        "/save <name> - save a new recipe\n"
        "/recipes - list saved recipes\n"
        "/view <id> - view a saved recipe\n"
        "/export <name> - export a recipe as a file\n"
        "/canbake - cook with only whats on hand\n"
        "/batch - plan a multi-dish meal with cooking timeline\n"
        "/cookmode - step-by-step cooking mode on the last recipe\n"
        "/log chicken rice - log a meal\n"
        "/calories - todays nutrition\n"
        "/nutrition chicken - lookup nutrition\n"
        "/goal - set daily targets\n"
        "/weekly - weekly summary\n"
        "/shopping <recipe> - generate shopping list\n\n"
        "You can send voice messages too!"
    )
    await update.message.reply_text(text)


# --- Pantry ---

async def add_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/add chicken, rice, broccoli`")
        return
    items = [x.strip() for x in " ".join(args).split(",") if x.strip()]
    for item in items:
        db.add_pantry_item(update.effective_user.id, item)
    await update.message.reply_text(f"Added {len(items)} item(s) to pantry: {', '.join(items)}")


async def recategorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.recategorize_pantry(update.effective_user.id)
    await update.message.reply_text("Recategorized all pantry items.")


async def set_pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/set equipment air fryer, rice cooker, oven`"
        )
        return
    key = args[0].lower()
    value = " ".join(args[1:])
    db.set_user_preference(update.effective_user.id, key, value)
    await update.message.reply_text(f"Saved your {key}: {value}")


async def equipment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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
    user_id = update.effective_user.id
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
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/remove milk`", parse_mode="Markdown")
        return
    item = " ".join(args).strip().lower()
    db.remove_pantry_item(update.effective_user.id, item)
    await update.message.reply_text(f"🗑️ Removed `{item}` from pantry", parse_mode="Markdown")


def _format_pantry_grouped(groups):
    lines = ["Pantry", ""]
    cat_order = [
        "Proteins & Prepared Meats",
        "Sauces, Condiments & Fermented",
        "Spices, Seasonings & Mixes",
        "Pantry Staples",
    ]
    seen = set()
    for cat in cat_order:
        # Collect subcategories under this top-level group
        subs = {}
        for full_cat, names in groups.items():
            if full_cat.startswith(cat):
                sub = full_cat.replace(cat, "").strip(" /")
                subs.setdefault(sub, []).extend(names)
        if not subs:
            continue
        seen.add(cat)
        lines.append(cat)
        for sub, names in subs.items():
            if sub:
                lines.append(f"  {sub}:")
            for name in sorted(names):
                lines.append(f"  - {name}")
        lines.append("")

    # Remaining categories
    for full_cat, names in groups.items():
        top = full_cat.split("/")[0].strip()
        if top in seen:
            continue
        seen.add(top)
        lines.append(top)
        for name in sorted(names):
            lines.append(f"  - {name}")
        lines.append("")

    return "\n".join(lines).strip()


async def show_pantry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = db.get_pantry_grouped(update.effective_user.id)
    if not groups:
        await update.message.reply_text("Pantry is empty. Tell me what to add!")
        return
    await update.message.reply_text(_format_pantry_grouped(groups))


async def expiring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = int(args[0]) if args else 3
    items = db.get_expiring_items(update.effective_user.id, days)
    if not items:
        await update.message.reply_text(f"✅ Nothing expiring in {days} days!")
        return
    lines = [f"⚠️ *Items expiring within {days} days:*"]
    for item in items:
        lines.append(f"• {item['name'].title()} — expires {item['expiry_date']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Recipes ---

async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pantry = db.get_pantry_names(user_id)
    pref = " ".join(context.args) if context.args else ""
    await _do_suggest(update, context, user_id, pantry, pref)


async def _do_suggest(update, context, user_id, pantry, pref):
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


async def _elaborate_dish(update, context, user_id, text, dish_index, pantry_items):
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

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
         InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
        [InlineKeyboardButton("📤 Export", callback_data="export_last")],
    ])
    sanitized = _sanitize_markdown(result)
    await _send_long_message(update, busy, sanitized, keyboard)


async def suggest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "save_last":
        text = _last_suggestion.get(user_id, "")
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


async def elaborate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
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
        _batch_state.pop(user_id, None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Export", callback_data="export_last")],
        ])
        await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)

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

    # Show title + ingredients with Next button
    lines = [f"**Cooking Mode: {title}**", "", "**Ingredients**"]
    for ing in ingredients:
        lines.append(f"- {ing}")
    lines.append("")
    lines.append(f"*{len(steps)} steps total*")

    _cook_state[user_id] = {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "shown": 0,
        "msg_id": None,
    }

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

    # Build the full message: title + ingredients + steps shown so far + new batch
    lines = [f"**Cooking Mode: {state['title']}**", "", "**Ingredients**"]
    for ing in state["ingredients"]:
        lines.append(f"- {ing}")
    lines.append("")

    # All steps revealed so far (ingredients always shown)
    for i in range(shown + batch):
        lines.append(f"{i+1}. {steps[i]}")

    state["shown"] += batch
    remaining = len(steps) - state["shown"]

    if remaining <= 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Done", callback_data="cook_done")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Step", callback_data="cook_next")],
        ])

    try:
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    except Exception:
        pass


async def cook_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _cook_state.pop(user_id, None)
    recipe_text = _last_suggestion.get(user_id, "")
    if recipe_text:
        saved = _is_saved_recipe.get(user_id, False)
        if saved:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
                 InlineKeyboardButton("📤 Export", callback_data="export_last")],
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Save Recipe", callback_data="save_last"),
                 InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe")],
                [InlineKeyboardButton("📤 Export", callback_data="export_last")],
            ])
        sanitized = _sanitize_markdown(recipe_text)
        await query.edit_message_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)
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

    lines = [f"**Cooking Mode: {title}**", "", "**Ingredients**"]
    for ing in ingredients:
        lines.append(f"- {ing}")
    lines.append("")
    lines.append(f"*{len(steps)} steps total*")

    _cook_state[user_id] = {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "shown": 0,
        "msg_id": None,
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Next Step", callback_data="cook_next")],
    ])
    msg = await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    _cook_state[user_id]["msg_id"] = msg.message_id


async def save_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    text = _last_suggestion.get(user_id, "")

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
    recipes = db.get_recipes(update.effective_user.id)
    if not recipes:
        await update.message.reply_text("No saved recipes. Try `/suggest` to get some!", parse_mode="Markdown")
        return
    text, markup = _build_recipe_list(recipes)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


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
        # Split into rows of 8 (Telegram max per row)
        for start in range(0, len(view_row), 8):
            buttons.append(view_row[start:start+8])
    if delete_row:
        for start in range(0, len(delete_row), 8):
            buttons.append(delete_row[start:start+8])

    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def view_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/view <id>` or `/view <title>`", parse_mode="Markdown")
        return
    user_id = update.effective_user.id
    query_str = " ".join(args)

    recipe = None
    if query_str.isdigit():
        recipe = db.get_recipe(user_id, int(query_str))
    if not recipe:
        recipe = db.get_recipe_by_title(user_id, query_str)

    if not recipe:
        await update.message.reply_text("Recipe not found. Use `/recipes` to see your saved recipes.", parse_mode="Markdown")
        return

    text = _format_recipe(recipe)
    _last_suggestion[update.effective_user.id] = text
    _is_saved_recipe[update.effective_user.id] = True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
         InlineKeyboardButton("📤 Export", callback_data="export_last")],
    ])
    sanitized = _sanitize_markdown(text)
    await update.message.reply_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)


async def delete_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/delete <id>`", parse_mode="Markdown")
        return
    db.delete_recipe(update.effective_user.id, int(args[0]))
    await update.message.reply_text(f"🗑️ Deleted recipe #{args[0]}")


async def delete_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    recipe_id = int(query.data.split("_")[1])
    user_id = query.from_user.id
    db.delete_recipe(user_id, recipe_id)
    # Refresh with sequential numbering
    recipes = db.get_recipes(user_id)
    if recipes:
        text, markup = _build_recipe_list(recipes)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await query.edit_message_text("No saved recipes. Try `/suggest` to get some!", parse_mode="Markdown")


async def view_recipe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    recipe_id = int(query.data.split("_")[1])
    user_id = query.from_user.id
    recipe = db.get_recipe(user_id, recipe_id)
    if recipe:
        text = _format_recipe(recipe)
        _last_suggestion[user_id] = text
        _is_saved_recipe[user_id] = True
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🍳 Cook Mode", callback_data="cook_recipe"),
             InlineKeyboardButton("📤 Export", callback_data="export_last")],
        ])
        sanitized = _sanitize_markdown(text)
        await query.message.reply_text(sanitized, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query.message.reply_text("Recipe not found.")


async def canbake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pantry = db.get_pantry_names(user_id)
    if not pantry:
        await update.message.reply_text("Your pantry is empty! Add items first.")
        return

    items_str = ", ".join(pantry)
    msg = await update.message.reply_text("Checking your pantry...")
    equip = db.get_user_preference(user_id, "equipment")
    pref = f"Equipment: {equip}.\n" if equip else ""
    pref += "Suggest recipes I can cook RIGHT NOW using ONLY ingredients from this list plus common staples (salt, pepper, oil, sugar, garlic, onion, eggs, rice, soy sauce). Mark any [BUY] items I still need."
    text = ai.suggest_recipe(user_id, pantry, pref)
    text = text.replace("**", "")
    await msg.edit_text(text)


# --- Nutrition ---

async def log_meal(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/shopping Braised Beef Kolo Mee`", parse_mode="Markdown")
        return
    title = " ".join(args)
    user_id = update.effective_user.id

    recipe = db.get_recipe_by_title(user_id, title)
    if not recipe:
        await update.message.reply_text(f"Recipe '{title}' not found. Use `/recipes` to see saved recipes.", parse_mode="Markdown")
        return

    pantry_names = set(db.get_pantry_names(user_id))
    missing = []
    for ing in recipe["ingredients"]:
        # Simple check — ingredient name is the first word(s) before any comma or parentheses
        ing_key = ing.split(",")[0].split("(")[0].strip().lower()
        if not any(p in ing_key for p in pantry_names):
            missing.append(ing)

    if not missing:
        await update.message.reply_text("✅ You have all the ingredients! Time to cook.")
        return

    msg = await update.message.reply_text("🛒 Generating shopping list...")
    result = ai.generate_shopping_list(title, missing)
    await msg.edit_text(
        f"🛒 *Shopping List for {title}*\n{result}",
        parse_mode="Markdown",
    )


# --- Natural language handler ---

async def _handle_user_text(update, context, user_id, text):
    """Core text processing logic used by both handle_text and handle_voice."""
    pantry = db.get_pantry_names(user_id)
    recipes = db.get_recipes(user_id)

    # Check batch state first (before dish selection, to avoid conflicts)
    state = _batch_state.get(user_id)
    if state and state["count"] == 0:
        count = await batch_handle_count(user_id, text)
        if count:
            state["count"] = count
            await update.effective_message.reply_text(f"Great! {count} dishes. What cuisine?")
        else:
            await update.effective_message.reply_text("Please enter a number (1-10).")
        return

    if state and state["count"] > 0 and not state["cuisine"]:
        state["cuisine"] = text.strip()
        await update.effective_message.reply_text(f"{state['cuisine'].title()} sounds good! Generating suggestions...")
        menu = _batch_generate_menu(user_id)
        if menu:
            await batch_show_menu(update, context, user_id)
        else:
            await update.effective_message.reply_text("Could not generate suggestions. Try a different cuisine.")
            state["cuisine"] = ""
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
            sanitized = _sanitize_markdown(result)
            await msg.edit_text(sanitized, parse_mode="Markdown")
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
            sanitized = _sanitize_markdown(result)
            await msg.edit_text(sanitized, parse_mode="Markdown")
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
            await msg.edit_text(answer)
            return

    # --- NL Processing ---
    msg = await update.effective_message.reply_text("Thinking...")
    result = ai.process_natural_language(text, pantry, recipes)
    action = result.get("action", "chat")
    items = result.get("items", [])
    reply = result.get("message", "")

    if action == "add":
        for item in items:
            db.add_pantry_item(user_id, item)
        await msg.edit_text(reply)

    elif action == "remove":
        for item in items:
            db.remove_pantry_item(user_id, item)
        await msg.edit_text(reply)

    elif action == "clear_pantry":
        for item in pantry:
            db.remove_pantry_item(user_id, item)
        await msg.edit_text("Pantry cleared!")

    elif action == "list_pantry":
        groups = db.get_pantry_grouped(user_id)
        if not groups:
            await msg.edit_text("Your pantry is empty. Tell me what to add!")
            return
        await msg.edit_text(_format_pantry_grouped(groups))

    elif action == "set_preference":
        if len(items) >= 2:
            db.set_user_preference(user_id, items[0], " ".join(items[1:]))
            await msg.edit_text(reply or f"Saved your {items[0]}.")
        else:
            await msg.edit_text("Tell me what to remember. Example: I have an air fryer")

    elif action == "expiring":
        ex = db.get_expiring_items(user_id)
        if not ex:
            await msg.edit_text("Nothing expiring soon!")
            return
        lines = ["Expiring soon:"]
        for it in ex:
            lines.append(f"- {it['name'].title()} (expires {it['expiry_date']})")
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
            await update.effective_message.reply_text(
                _format_recipe(recipe), parse_mode="Markdown"
            )
        else:
            await update.effective_message.reply_text(
                "Recipe not found. Say 'show my recipes' to see your saved recipes."
            )

    elif action == "help":
        await msg.delete()
        await start(update, context)

    else:
        recipe = _last_suggestion.get(user_id)
        if recipe:
            await msg.edit_text("Let me answer that...")
            answer = ai.recipe_followup(recipe, text)
            await msg.edit_text(answer)
        else:
            await msg.edit_text(reply if reply else
                "I can help manage your pantry, suggest recipes, track meals, and more! "
                "Try: 'add chicken and rice to pantry' or 'what can I cook?'")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    await _handle_user_text(update, context, user_id, text)


# --- Photo recognition ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Analyzing image...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
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
        await msg.edit_text(f"Error processing image: {e}")


# --- Voice ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await msg.edit_text(f"Voice processing error: {e}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        msg = str(context.error)
        if "Daily Groq request limit" in msg or "Groq daily request" in msg:
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
    app.add_handler(CommandHandler("set", set_pref))
    app.add_handler(CommandHandler("equipment", equipment))
    app.add_handler(CommandHandler("suggest", suggest))
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
    app.add_handler(CommandHandler("diet", diet))
    app.add_handler(CommandHandler("export", export_recipe))
    app.add_handler(CommandHandler("batch", batch_start))
    app.add_handler(CommandHandler("cookmode", cookmode))
    app.add_handler(CommandHandler("cook", cookmode))  # alias
    app.add_handler(CallbackQueryHandler(elaborate_callback, pattern="^elaborate_[0-4]$"))
    app.add_handler(CallbackQueryHandler(suggest_callback, pattern="^(save_last|suggest_again|export_last)$"))
    app.add_handler(CallbackQueryHandler(batch_callback, pattern="^batch_"))
    app.add_handler(CallbackQueryHandler(cook_callback, pattern="^cook_next$"))
    app.add_handler(CallbackQueryHandler(cook_done_callback, pattern="^cook_done$"))
    app.add_handler(CallbackQueryHandler(cook_from_recipe_callback, pattern="^cook_recipe$"))
    app.add_handler(CallbackQueryHandler(delete_recipe_callback, pattern="^delrecipe_"))
    app.add_handler(CallbackQueryHandler(view_recipe_callback, pattern="^viewrecipe_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.add_error_handler(error_handler)

    return app
