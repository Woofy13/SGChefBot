import sqlite3
import json
import os
from datetime import datetime, date, timedelta
from config import DB_PATH

# Detect if we're using PostgreSQL (Supabase) or SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def get_connection():
    if USE_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _commit(conn):
    if USE_PG:
        conn.commit()
    else:
        conn.commit()


def _close(conn):
    conn.close()


def _q(query):
    """Convert SQLite ? placeholders to PostgreSQL %s when needed."""
    if USE_PG:
        return query.replace("?", "%s")
    return query


def _execute(conn, query, params=None):
    """Execute query on either SQLite or PostgreSQL. Returns cursor-like object."""
    if USE_PG:
        cur = conn.cursor()
        cur.execute(query, params or ())
        return cur
    return conn.execute(query, params or ())


def _fetchone(cur):
    """Fetch one row as dict regardless of backend."""
    row = cur.fetchone()
    if row is None:
        return None
    if USE_PG:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return dict(row)


def _fetchall(cur):
    """Fetch all rows as list of dicts regardless of backend."""
    rows = cur.fetchall()
    if not rows:
        return []
    if USE_PG:
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]


def init_db():
    if USE_PG:
        _init_pg()
    else:
        _init_sqlite()


def _init_sqlite():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pantry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            quantity TEXT DEFAULT '1',
            unit TEXT DEFAULT '',
            expiry_date TEXT,
            added_date TEXT DEFAULT (date('now')),
            category TEXT DEFAULT '',
            UNIQUE(user_id, name)
        );
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            ingredients TEXT NOT NULL,
            instructions TEXT NOT NULL,
            full_text TEXT DEFAULT '',
            cuisine TEXT DEFAULT '',
            protein_g INTEGER DEFAULT 0,
            calories INTEGER DEFAULT 0,
            sodium_mg INTEGER DEFAULT 0,
            saved_date TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS meal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            meal_name TEXT NOT NULL,
            calories INTEGER DEFAULT 0,
            protein_g INTEGER DEFAULT 0,
            sodium_mg INTEGER DEFAULT 0,
            logged_date TEXT DEFAULT (date('now')),
            meal_type TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cooking_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            dish_name TEXT NOT NULL,
            cooked_date TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS user_goals (
            user_id INTEGER PRIMARY KEY,
            daily_calories INTEGER DEFAULT 1900,
            daily_protein INTEGER DEFAULT 120,
            daily_sodium INTEGER DEFAULT 2300
        );
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            quantity TEXT DEFAULT '',
            added_date TEXT DEFAULT (date('now')),
            checked INTEGER DEFAULT 0,
            UNIQUE(user_id, name)
        );
    """)
    try:
        _execute(conn, "ALTER TABLE recipes ADD COLUMN full_text TEXT DEFAULT ''")
    except Exception:
        pass

    except Exception:
        pass
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_cooking_log_user ON cooking_log(user_id, cooked_date)",
        "CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date ON meal_logs(user_id, logged_date)",
        "CREATE INDEX IF NOT EXISTS idx_pantry_user_expiry ON pantry(user_id, expiry_date)",
    ]:
        try:
            _execute(conn, idx)
        except Exception:
            pass
    _commit(conn)
    _close(conn)


def _init_pg():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pantry (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            quantity TEXT DEFAULT '1',
            unit TEXT DEFAULT '',
            expiry_date TEXT,
            added_date TEXT DEFAULT (CURRENT_DATE),
            category TEXT DEFAULT '',
            UNIQUE(user_id, name)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            ingredients TEXT NOT NULL,
            instructions TEXT NOT NULL,
            full_text TEXT DEFAULT '',
            cuisine TEXT DEFAULT '',
            protein_g INTEGER DEFAULT 0,
            calories INTEGER DEFAULT 0,
            sodium_mg INTEGER DEFAULT 0,
            saved_date TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_logs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            meal_name TEXT NOT NULL,
            calories INTEGER DEFAULT 0,
            protein_g INTEGER DEFAULT 0,
            sodium_mg INTEGER DEFAULT 0,
            logged_date TEXT DEFAULT (CURRENT_DATE),
            meal_type TEXT DEFAULT ''
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_goals (
            user_id INTEGER PRIMARY KEY,
            daily_calories INTEGER DEFAULT 1900,
            daily_protein INTEGER DEFAULT 120,
            daily_sodium INTEGER DEFAULT 2300
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shopping_list (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            quantity TEXT DEFAULT '',
            added_date TEXT DEFAULT (CURRENT_DATE),
            checked INTEGER DEFAULT 0,
            UNIQUE(user_id, name)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cooking_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            dish_name TEXT NOT NULL,
            cooked_date TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_cooking_log_user ON cooking_log(user_id, cooked_date)",
        "CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date ON meal_logs(user_id, logged_date)",
        "CREATE INDEX IF NOT EXISTS idx_pantry_user_expiry ON pantry(user_id, expiry_date)",
    ]:
        try:
            cur.execute(idx)
        except Exception:
            pass
    _commit(conn)
    _close(conn)
    # Migration: add UNIQUE for databases created without it
    try:
        c = get_connection()
        try:
            c.cursor().execute("ALTER TABLE shopping_list ADD UNIQUE (user_id, name)")
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


# --- Pantry ---

CATEGORY_MAP = {
    "marinated chicken": "Proteins & Prepared Meats / Frozen",
    "chicken breast": "Proteins & Prepared Meats / Frozen",
    "shabu shabu thin-sliced beef": "Proteins & Prepared Meats / Frozen",
    "cheeseburger": "Proteins & Prepared Meats / Frozen",
    "sausages": "Proteins & Prepared Meats / Frozen",
    "otah roll": "Proteins & Prepared Meats / Frozen",
    "popcorn chicken": "Proteins & Prepared Meats / Frozen",
    "buffalo wings": "Proteins & Prepared Meats / Frozen",
    "hash browns": "Proteins & Prepared Meats / Frozen",
    "pork chop": "Proteins & Prepared Meats / Frozen",
    "garlic pieces": "Proteins & Prepared Meats / Frozen",
    "beef chuck roast": "Proteins & Prepared Meats / Fresh/Chilled",
    "sliced cheese": "Proteins & Prepared Meats / Fresh/Chilled",
    "grated cheese": "Proteins & Prepared Meats / Fresh/Chilled",
    "soy sauce": "Sauces, Condiments & Fermented",
    "fish sauce": "Sauces, Condiments & Fermented",
    "oyster sauce": "Sauces, Condiments & Fermented",
    "mirin": "Sauces, Condiments & Fermented",
    "sake": "Sauces, Condiments & Fermented",
    "laoganma chili crisp": "Sauces, Condiments & Fermented",
    "sambal paste": "Sauces, Condiments & Fermented",
    "longan honey": "Sauces, Condiments & Fermented",
    "better than bouillon": "Sauces, Condiments & Fermented",
    "mccormick taco seasoning": "Spices, Seasonings & Mixes",
    "shan briyani spices": "Spices, Seasonings & Mixes",
    "thai holy basil mix": "Spices, Seasonings & Mixes",
    "chili spices": "Spices, Seasonings & Mixes",
    "other meat spices": "Spices, Seasonings & Mixes",
    "msg": "Spices, Seasonings & Mixes",
    "salt": "Spices, Seasonings & Mixes",
    "black pepper": "Spices, Seasonings & Mixes",
    "sugar": "Spices, Seasonings & Mixes",
    "chicken": "Proteins & Prepared Meats / Fresh/Chilled",
    "chicken thigh": "Proteins & Prepared Meats / Fresh/Chilled",
    "chicken breast": "Proteins & Prepared Meats / Fresh/Chilled",
    "chicken wings": "Proteins & Prepared Meats / Fresh/Chilled",
    "beef": "Proteins & Prepared Meats / Fresh/Chilled",
    "pork": "Proteins & Prepared Meats / Fresh/Chilled",
    "fish": "Proteins & Prepared Meats / Fresh/Chilled",
    "salmon": "Proteins & Prepared Meats / Fresh/Chilled",
    "shrimp": "Proteins & Prepared Meats / Fresh/Chilled",
    "tofu": "Proteins & Prepared Meats / Fresh/Chilled",
    "eggs": "Proteins & Prepared Meats / Fresh/Chilled",
    "butter": "Proteins & Prepared Meats / Fresh/Chilled",
    "olive oil blend": "Pantry Staples",
    "angel hair pasta": "Pantry Staples",
    "instant noodles": "Pantry Staples",
    "raw briyani rice": "Pantry Staples",
    "tortilla wraps": "Pantry Staples",
}


def categorize_item(name):
    return CATEGORY_MAP.get(name.strip().lower(), "")


def add_pantry_item(user_id, name, quantity="1", unit="", expiry="", category=None):
    name = name.strip().lower()
    if category is None:
        category = categorize_item(name)
    conn = get_connection()
    _execute(conn, 
        _q("INSERT INTO pantry (user_id, name, quantity, unit, expiry_date, category) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, name) DO UPDATE SET quantity = excluded.quantity, unit = excluded.unit, expiry_date = COALESCE(excluded.expiry_date, pantry.expiry_date), category = COALESCE(excluded.category, pantry.category)"),
        (user_id, name, quantity, unit, expiry if expiry else None, category),
    )
    _commit(conn)
    _close(conn)


def add_pantry_items(user_id, items, expiry=""):
    conn = get_connection()
    for name in items:
        name = name.strip().lower()
        category = categorize_item(name)
        _execute(conn,
            _q("INSERT INTO pantry (user_id, name, quantity, unit, expiry_date, category) VALUES (?, ?, '1', '', ?, ?) ON CONFLICT(user_id, name) DO UPDATE SET quantity = excluded.quantity, unit = excluded.unit, expiry_date = COALESCE(excluded.expiry_date, pantry.expiry_date), category = COALESCE(excluded.category, pantry.category)"),
            (user_id, name, expiry if expiry else None, category),
        )
    _commit(conn)
    _close(conn)


def remove_pantry_item(user_id, name):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM pantry WHERE user_id = ? AND name = ?"),
                 (user_id, name.strip().lower()))
    _commit(conn)
    _close(conn)


def get_pantry(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT name, quantity, unit, expiry_date, category FROM pantry WHERE user_id = ? ORDER BY category, name"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_pantry_grouped(user_id):
    items = get_pantry(user_id)
    groups = {}
    for item in items:
        cat = item.get("category", "") or "Other"
        groups.setdefault(cat, []).append(item)
    return groups


def recategorize_by_map(user_id, cat_map):
    conn = get_connection()
    for name, cat in cat_map.items():
        _execute(conn, _q("UPDATE pantry SET category = ? WHERE user_id = ? AND name = ?"),
                 (cat, user_id, name.strip().lower()))
    _commit(conn)
    _close(conn)


def recategorize_pantry(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, name FROM pantry WHERE user_id = ?"), (user_id,))
    rows = _fetchall(cur)
    for row in rows:
        cat = categorize_item(row["name"])
        if cat:
            _execute(conn, _q("UPDATE pantry SET category = ? WHERE id = ?"), (cat, row["id"]))
    _commit(conn)
    _close(conn)


def get_expiring_items(user_id, days=3):
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    conn = get_connection()
    cur = _execute(conn, _q("SELECT name, quantity, unit, expiry_date FROM pantry WHERE user_id = ? AND expiry_date IS NOT NULL AND expiry_date <= ? ORDER BY expiry_date"), (user_id, cutoff))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def store_chat_id(user_id, chat_id):
    set_user_preference(user_id, "chat_id", str(chat_id))


def get_expiring_items_in_month(user_id, year, month):
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT name, quantity, unit, expiry_date FROM pantry WHERE user_id = ? AND expiry_date IS NOT NULL AND expiry_date >= ? AND expiry_date < ? ORDER BY expiry_date"),
        (user_id, start, end),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_users_for_reminder(year, month):
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT DISTINCT p.user_id, up.value as chat_id FROM pantry p LEFT JOIN user_preferences up ON p.user_id = up.user_id AND up.key = 'chat_id' WHERE p.expiry_date IS NOT NULL AND p.expiry_date >= ? AND p.expiry_date < ? AND p.user_id NOT IN (SELECT user_id FROM user_preferences WHERE key = 'expiry_reminder' AND value = 'off')"),
        (start, end),
    )
    rows = _fetchall(cur)
    _close(conn)
    return [(r["user_id"], r["chat_id"]) for r in rows if r.get("chat_id")]


def get_pantry_names(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT name FROM pantry WHERE user_id = ? ORDER BY name"), (user_id,))
    rows = cur.fetchall()
    _close(conn)
    return [r["name"] if not USE_PG else r[0] for r in rows]


# --- Recipes ---

def save_recipe(user_id, title, description, ingredients, instructions,
                full_text="", cuisine="", protein_g=0, calories=0, sodium_mg=0):
    conn = get_connection()
    _execute(conn, 
        _q("INSERT INTO recipes (user_id, title, description, ingredients, instructions, full_text, cuisine, protein_g, calories, sodium_mg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
        (user_id, title, description,
         json.dumps(ingredients), json.dumps(instructions),
         full_text, cuisine, protein_g, calories, sodium_mg),
    )
    _commit(conn)
    _close(conn)


def get_recipes(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, title, description, cuisine, protein_g, calories, saved_date FROM recipes WHERE user_id = ? ORDER BY saved_date DESC"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def _recipe_row_to_dict(d):
    if not d:
        return None
    d["ingredients"] = json.loads(d["ingredients"])
    d["instructions"] = json.loads(d["instructions"])
    return d


def get_recipe(user_id, recipe_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT * FROM recipes WHERE user_id = ? AND id = ?"), (user_id, recipe_id))
    row = _fetchone(cur)
    _close(conn)
    return _recipe_row_to_dict(row)


def get_recipe_by_title(user_id, title):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT * FROM recipes WHERE user_id = ? AND LOWER(title) = LOWER(?)"), (user_id, title.strip()))
    row = _fetchone(cur)
    _close(conn)
    return _recipe_row_to_dict(row)


def delete_recipe(user_id, recipe_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM recipes WHERE user_id = ? AND id = ?"),
                 (user_id, recipe_id))
    _commit(conn)
    _close(conn)


# --- Meal Logs ---

def log_meal(user_id, meal_name, calories=0, protein_g=0, sodium_mg=0, meal_type=""):
    conn = get_connection()
    _execute(conn, 
        _q("INSERT INTO meal_logs (user_id, meal_name, calories, protein_g, sodium_mg, meal_type) VALUES (?, ?, ?, ?, ?, ?)"),
        (user_id, meal_name, calories, protein_g, sodium_mg, meal_type),
    )
    _commit(conn)
    _close(conn)


def get_today_logs(user_id):
    today = date.today().isoformat()
    conn = get_connection()
    cur = _execute(conn, 
        _q("SELECT meal_name, calories, protein_g, sodium_mg, meal_type FROM meal_logs WHERE user_id = ? AND logged_date = ? ORDER BY id"),
        (user_id, today),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_weekly_logs(user_id):
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    conn = get_connection()
    cur = _execute(conn, 
        _q("SELECT logged_date, SUM(calories) as total_cal, SUM(protein_g) as total_protein, SUM(sodium_mg) as total_sodium FROM meal_logs WHERE user_id = ? AND logged_date >= ? GROUP BY logged_date ORDER BY logged_date"),
        (user_id, week_ago),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


# --- User Preferences ---

def get_user_preference(user_id, key):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT value FROM user_preferences WHERE user_id = ? AND key = ?"), (user_id, key))
    row = cur.fetchone()
    _close(conn)
    if row:
        return row["value"] if not USE_PG else row[0]
    return ""


def set_user_preference(user_id, key, value):
    conn = get_connection()
    _execute(conn, 
        _q("INSERT INTO user_preferences (user_id, key, value) VALUES (?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value"),
        (user_id, key, value.strip()),
    )
    _commit(conn)
    _close(conn)


def get_all_preferences(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT key, value FROM user_preferences WHERE user_id = ?"), (user_id,))
    rows = cur.fetchall()
    _close(conn)
    if USE_PG:
        return {r[0]: r[1] for r in rows}
    return {r["key"]: r["value"] for r in rows}


# --- User Goals ---

def get_goals(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT daily_calories, daily_protein, daily_sodium FROM user_goals WHERE user_id = ?"), (user_id,))
    row = cur.fetchone()
    _close(conn)
    if row:
        if USE_PG:
            return {"daily_calories": row[0], "daily_protein": row[1], "daily_sodium": row[2]}
        return dict(row)
    return {"daily_calories": 1900, "daily_protein": 120, "daily_sodium": 2300}


def set_goals(user_id, daily_calories=None, daily_protein=None, daily_sodium=None):
    current = get_goals(user_id)
    cal = daily_calories if daily_calories is not None else current["daily_calories"]
    pro = daily_protein if daily_protein is not None else current["daily_protein"]
    sod = daily_sodium if daily_sodium is not None else current["daily_sodium"]
    conn = get_connection()
    _execute(conn, 
        _q("INSERT INTO user_goals (user_id, daily_calories, daily_protein, daily_sodium) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET daily_calories = excluded.daily_calories, daily_protein = excluded.daily_protein, daily_sodium = excluded.daily_sodium"),
        (user_id, cal, pro, sod),
    )
    _commit(conn)
    _close(conn)


# --- Shopping List ---

def add_to_shopping_list(user_id, name, quantity=""):
    name = name.strip().lower()
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO shopping_list (user_id, name, quantity) VALUES (?, ?, ?) ON CONFLICT(user_id, name) DO UPDATE SET quantity = excluded.quantity, checked = 0"),
        (user_id, name, quantity),
    )
    _commit(conn)
    _close(conn)


def get_shopping_list(user_id):
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, name, quantity, checked FROM shopping_list WHERE user_id = ? ORDER BY checked, added_date"),
        (user_id,),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def add_to_shopping_list_batch(user_id, items):
    conn = get_connection()
    for name in items:
        name = name.strip().lower()
        _execute(conn,
            _q("INSERT INTO shopping_list (user_id, name) VALUES (?, ?) ON CONFLICT(user_id, name) DO UPDATE SET checked = 0"),
            (user_id, name),
        )
    _commit(conn)
    _close(conn)


def remove_from_shopping_list(user_id, name):
    name = name.strip().lower()
    conn = get_connection()
    _execute(conn, _q("DELETE FROM shopping_list WHERE user_id = ? AND name = ?"), (user_id, name))
    _commit(conn)
    _close(conn)


def clear_checked_items(user_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM shopping_list WHERE user_id = ? AND checked = 1"), (user_id,))
    _commit(conn)
    _close(conn)


def clear_shopping_list(user_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM shopping_list WHERE user_id = ?"), (user_id,))
    _commit(conn)
    _close(conn)


def toggle_shopping_item(item_id):
    conn = get_connection()
    _execute(conn, _q("UPDATE shopping_list SET checked = 1 - checked WHERE id = ?"), (item_id,))
    _commit(conn)
    _close(conn)


# --- Cooking Stats ---


def log_cooked(user_id, dish_name):
    conn = get_connection()
    _execute(conn, _q("INSERT INTO cooking_log (user_id, dish_name) VALUES (?, ?)"),
             (user_id, dish_name.strip().lower()))
    _commit(conn)
    _close(conn)


def get_cooking_stats(user_id):
    conn = get_connection()
    
    # Combined: total, unique, first_date
    cur = _execute(conn, _q("SELECT COUNT(*) as total, COUNT(DISTINCT dish_name) as unique_dishes, MIN(cooked_date) as first_date FROM cooking_log WHERE user_id = ?"), (user_id,))
    row = _fetchone(cur)
    total = row["total"] if row else 0
    if not total:
        _close(conn)
        return {}
    unique_dishes = row["unique_dishes"]
    first_cook_date = row["first_date"]
    
    # Most cooked dish
    cur = _execute(conn, _q("SELECT dish_name, COUNT(*) as cnt FROM cooking_log WHERE user_id = ? GROUP BY dish_name ORDER BY cnt DESC LIMIT 1"), (user_id,))
    top = _fetchone(cur)
    most_cooked = top["dish_name"] if top else ""
    most_cooked_count = top["cnt"] if top else 0
    
    # All dates in one pass (streak, month, weekend, avg)
    cur = _execute(conn, _q("SELECT cooked_date FROM cooking_log WHERE user_id = ? ORDER BY cooked_date DESC"), (user_id,))
    all_dates = [r["cooked_date"] for r in _fetchall(cur)]
    _close(conn)
    
    from datetime import datetime as dt
    today = date.today()
    month_start = f"{today.year:04d}-{today.month:02d}-01"
    dt_dates = [dt.strptime(d, "%Y-%m-%d").date() for d in all_dates]
    
    # Streak (deduplicated by date)
    streak = 0
    if dt_dates:
        seen = set()
        unique_ordered = []
        for d in dt_dates:
            if d not in seen:
                seen.add(d)
                unique_ordered.append(d)
        streak = 1
        for i in range(1, len(unique_ordered)):
            if (unique_ordered[i - 1] - unique_ordered[i]).days == 1:
                streak += 1
            else:
                break
    
    # Month count & weekend % from same date list
    month_count = sum(1 for d in dt_dates if d.isoformat() >= month_start)
    weekend_count = sum(1 for d in dt_dates if d.weekday() >= 5)
    weekend_pct = round(weekend_count / len(dt_dates) * 100) if dt_dates else 0
    
    # Days since first cook, avg per week
    days_since_first = 0
    avg_per_week = 0
    if first_cook_date and total:
        first = dt.strptime(first_cook_date, "%Y-%m-%d").date()
        days_since_first = (today - first).days or 1
        avg_per_week = round(total / (days_since_first / 7), 1)
    
    return {
        "total": total,
        "unique_dishes": unique_dishes,
        "most_cooked": most_cooked,
        "most_cooked_count": most_cooked_count,
        "streak": streak,
        "month_count": month_count,
        "weekend_pct": weekend_pct,
        "avg_per_week": avg_per_week,
        "days_since_first": days_since_first,
        "first_cook_date": first_cook_date,
    }


def get_cooked_dishes(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT dish_name, COUNT(*) as cnt FROM cooking_log WHERE user_id = ? GROUP BY dish_name ORDER BY cnt DESC, dish_name"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return [(r["dish_name"], r["cnt"]) for r in rows]


def clear_cooking_log(user_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM cooking_log WHERE user_id = ?"), (user_id,))
    _commit(conn)
    _close(conn)
