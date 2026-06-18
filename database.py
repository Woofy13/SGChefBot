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
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            details TEXT DEFAULT '',
            expiry_date TEXT,
            created_at TEXT DEFAULT (date('now'))
        );
    """)
    try:
        _execute(conn, "ALTER TABLE recipes ADD COLUMN full_text TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        _execute(conn, "ALTER TABLE recipes ADD COLUMN full_text TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        _execute(conn, "ALTER TABLE parsed_transactions ADD COLUMN account TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        _execute(conn, "ALTER TABLE parsed_transactions ADD COLUMN subcategory TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        # Migrate old NOT NULL expiry_date to nullable
        _execute(conn, "ALTER TABLE vouchers RENAME TO vouchers_old")
        _execute(conn, "CREATE TABLE vouchers (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, details TEXT DEFAULT '', expiry_date TEXT, created_at TEXT DEFAULT (date('now')))")
        _execute(conn, "INSERT INTO vouchers SELECT id, user_id, name, details, expiry_date, created_at FROM vouchers_old")
        _execute(conn, "DROP TABLE vouchers_old")
    except Exception:
        pass

    try:
        _execute(conn, "ALTER TABLE parsed_transactions ADD COLUMN tx_type TEXT DEFAULT 'Expense'")
    except Exception:
        pass

    try:
        _execute(conn, "ALTER TABLE bills ADD COLUMN notified_today INTEGER DEFAULT 0")
    except Exception:
        pass

    try:
        _execute(conn, """
            CREATE TABLE IF NOT EXISTS household_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                primary_user_id INTEGER NOT NULL,
                member_user_id INTEGER NOT NULL UNIQUE,
                added_date TEXT DEFAULT (date('now')),
                UNIQUE(primary_user_id, member_user_id)
            )
        """)
    except Exception:
        pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transaction_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            merchant_pattern TEXT NOT NULL,
            category TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (date('now')),
            updated_at TEXT DEFAULT (date('now')),
            UNIQUE(user_id, merchant_pattern)
        );
        CREATE TABLE IF NOT EXISTS parse_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_text_hash TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS parsed_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            merchant TEXT NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            category TEXT DEFAULT '',
            subcategory TEXT DEFAULT '',
            account TEXT DEFAULT '',
            tx_type TEXT DEFAULT 'Expense',
            confidence TEXT DEFAULT 'high',
            notes TEXT DEFAULT '',
            confirmed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_name TEXT NOT NULL,
            card_last4 TEXT DEFAULT '',
            amount REAL NOT NULL,
            due_date TEXT NOT NULL,
            description TEXT DEFAULT '',
            paid INTEGER DEFAULT 0,
            notified_3d INTEGER DEFAULT 0,
            notified_today INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS monthly_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            day_of_month INTEGER DEFAULT 15,
            last_notified_month TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_name TEXT NOT NULL,
            card_last4 TEXT DEFAULT '',
            created_at TEXT DEFAULT (date('now')),
            UNIQUE(user_id, card_name)
        );
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cache_hit_tokens INTEGER DEFAULT 0,
            model TEXT DEFAULT '',
            created_at TEXT DEFAULT (date('now')),
            created_ts TEXT DEFAULT (datetime('now'))
        );
    """)
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_cooking_log_user ON cooking_log(user_id, cooked_date)",
        "CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date ON meal_logs(user_id, logged_date)",
        "CREATE INDEX IF NOT EXISTS idx_pantry_user_expiry ON pantry(user_id, expiry_date)",
        "CREATE INDEX IF NOT EXISTS idx_parse_sessions_user ON parse_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_parsed_tx_session ON parsed_transactions(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_bills_user_due ON bills(user_id, due_date, paid)",
        "CREATE INDEX IF NOT EXISTS idx_tx_rules_user ON transaction_rules(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(created_ts)",
        "CREATE INDEX IF NOT EXISTS idx_vouchers_user_expiry ON vouchers(user_id, expiry_date)",
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS household_members (
            id SERIAL PRIMARY KEY,
            primary_user_id INTEGER NOT NULL,
            member_user_id INTEGER NOT NULL UNIQUE,
            added_date TEXT DEFAULT (CURRENT_DATE),
            UNIQUE(primary_user_id, member_user_id)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transaction_rules (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            merchant_pattern TEXT NOT NULL,
            category TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (CURRENT_DATE),
            updated_at TEXT DEFAULT (CURRENT_DATE),
            UNIQUE(user_id, merchant_pattern)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parse_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            raw_text_hash TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parsed_transactions (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            merchant TEXT NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            category TEXT DEFAULT '',
            subcategory TEXT DEFAULT '',
            account TEXT DEFAULT '',
            tx_type TEXT DEFAULT 'Expense',
            confidence TEXT DEFAULT 'high',
            notes TEXT DEFAULT '',
            confirmed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            card_name TEXT NOT NULL,
            card_last4 TEXT DEFAULT '',
            amount REAL NOT NULL,
            due_date TEXT NOT NULL,
            description TEXT DEFAULT '',
            paid INTEGER DEFAULT 0,
            notified_3d INTEGER DEFAULT 0,
            notified_today INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS monthly_reminders (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            day_of_month INTEGER DEFAULT 15,
            last_notified_month TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            card_name TEXT NOT NULL,
            card_last4 TEXT DEFAULT '',
            created_at TEXT DEFAULT (CURRENT_DATE),
            UNIQUE(user_id, card_name)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cache_hit_tokens INTEGER DEFAULT 0,
            model TEXT DEFAULT '',
            created_at TEXT DEFAULT (CURRENT_DATE),
            created_ts TEXT DEFAULT (CURRENT_TIMESTAMP)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            details TEXT DEFAULT '',
            expiry_date TEXT,
            created_at TEXT DEFAULT (CURRENT_DATE)
        );
    """)
    try:
        cur.execute("ALTER TABLE parsed_transactions ADD COLUMN IF NOT EXISTS account TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE parsed_transactions ADD COLUMN IF NOT EXISTS subcategory TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE parsed_transactions ADD COLUMN IF NOT EXISTS tx_type TEXT DEFAULT 'Expense'")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS notified_today INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE vouchers ALTER COLUMN expiry_date DROP NOT NULL")
    except Exception:
        pass
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_cooking_log_user ON cooking_log(user_id, cooked_date)",
        "CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date ON meal_logs(user_id, logged_date)",
        "CREATE INDEX IF NOT EXISTS idx_pantry_user_expiry ON pantry(user_id, expiry_date)",
        "CREATE INDEX IF NOT EXISTS idx_parse_sessions_user ON parse_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_parsed_tx_session ON parsed_transactions(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_bills_user_due ON bills(user_id, due_date, paid)",
        "CREATE INDEX IF NOT EXISTS idx_tx_rules_user ON transaction_rules(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(created_ts)",
        "CREATE INDEX IF NOT EXISTS idx_vouchers_user_expiry ON vouchers(user_id, expiry_date)",
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


def get_called_ingredients(user_id):
    val = get_user_preference(user_id, "called_ingredients")
    return json.loads(val) if val else []


def add_called_ingredient(user_id, name):
    ingredients = get_called_ingredients(user_id)
    if name not in ingredients:
        ingredients.append(name)
        set_user_preference(user_id, "called_ingredients", json.dumps(ingredients))


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


# --- Household Members ---


def add_household_member(primary_user_id, member_user_id):
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO household_members (primary_user_id, member_user_id) VALUES (?, ?) ON CONFLICT(member_user_id) DO UPDATE SET primary_user_id = excluded.primary_user_id"),
        (primary_user_id, member_user_id),
    )
    _commit(conn)
    _close(conn)


def remove_household_member(primary_user_id, member_user_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM household_members WHERE primary_user_id = ? AND member_user_id = ?"), (primary_user_id, member_user_id))
    _commit(conn)
    _close(conn)


def get_household_primary(member_user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT primary_user_id FROM household_members WHERE member_user_id = ?"), (member_user_id,))
    row = _fetchone(cur)
    _close(conn)
    return row["primary_user_id"] if row else None


def get_household_members(primary_user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT member_user_id, added_date FROM household_members WHERE primary_user_id = ? ORDER BY added_date"), (primary_user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


# --- Transaction Rules (Statement Parser Rulebook) ---


def add_transaction_rule(user_id, merchant_pattern, category, notes=""):
    merchant_pattern = merchant_pattern.strip().lower()
    category = (category or "").strip()
    notes = (notes or "").strip()
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO transaction_rules (user_id, merchant_pattern, category, notes, updated_at) VALUES (?, ?, ?, ?, CURRENT_DATE) ON CONFLICT(user_id, merchant_pattern) DO UPDATE SET category = excluded.category, notes = excluded.notes, updated_at = CURRENT_DATE"),
        (user_id, merchant_pattern, category, notes),
    )
    _commit(conn)
    _close(conn)


def get_transaction_rules(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, merchant_pattern, category, notes, created_at, updated_at FROM transaction_rules WHERE user_id = ? ORDER BY updated_at DESC"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_transaction_rule(user_id, merchant_pattern):
    merchant_pattern = merchant_pattern.strip().lower()
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, merchant_pattern, category, notes FROM transaction_rules WHERE user_id = ? AND merchant_pattern = ?"), (user_id, merchant_pattern))
    row = _fetchone(cur)
    _close(conn)
    return row


def delete_transaction_rule(rule_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM transaction_rules WHERE id = ?"), (rule_id,))
    _commit(conn)
    _close(conn)


# --- Parse Sessions & Parsed Transactions ---


def create_parse_session(user_id, raw_text):
    import hashlib
    raw_text_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    conn = get_connection()
    cur = _execute(conn,
        _q("INSERT INTO parse_sessions (user_id, raw_text_hash) VALUES (?, ?)"),
        (user_id, raw_text_hash),
    )
    _commit(conn)
    session_id = cur.lastrowid if not USE_PG else cur.lastrowid
    if USE_PG:
        cur2 = _execute(conn, "SELECT lastval()")
        r = _fetchone(cur2)
        session_id = r["lastval"] if r else None
    _close(conn)
    return session_id


def add_parsed_transaction(session_id, user_id, merchant, amount, date_str, category, confidence, notes="", account="", subcategory="", tx_type="Expense"):
    merchant = (merchant or "").strip()
    category = (category or "").strip()
    subcategory = (subcategory or "").strip()
    account = (account or "").strip()
    tx_type = (tx_type or "Expense").strip().capitalize()
    if tx_type not in ("Expense", "Income"):
        tx_type = "Expense"
    confidence = (confidence or "high").strip().lower()
    notes = (notes or "").strip()
    conn = get_connection()
    cur = _execute(conn,
        _q("INSERT INTO parsed_transactions (session_id, user_id, merchant, amount, date, category, subcategory, account, tx_type, confidence, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
        (session_id, user_id, merchant, float(amount), date_str, category, subcategory, account, tx_type, confidence, notes),
    )
    _commit(conn)
    tx_id = cur.lastrowid if not USE_PG else cur.lastrowid
    if USE_PG:
        cur2 = _execute(conn, "SELECT lastval()")
        r = _fetchone(cur2)
        tx_id = r["lastval"] if r else None
    _close(conn)
    return tx_id


def get_parse_session(session_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, raw_text_hash, status, created_at FROM parse_sessions WHERE id = ?"), (session_id,))
    row = _fetchone(cur)
    _close(conn)
    return row


def get_parse_session_by_hash(user_id, raw_text_hash):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, raw_text_hash, status, created_at FROM parse_sessions WHERE user_id = ? AND raw_text_hash = ? ORDER BY id DESC"), (user_id, raw_text_hash))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_parsed_transactions(session_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, session_id, user_id, merchant, amount, date, category, subcategory, account, tx_type, confidence, notes, confirmed, created_at FROM parsed_transactions WHERE session_id = ? ORDER BY id"), (session_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_parsed_transaction(tx_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, session_id, user_id, merchant, amount, date, category, subcategory, account, tx_type, confidence, notes, confirmed, created_at FROM parsed_transactions WHERE id = ?"), (tx_id,))
    row = _fetchone(cur)
    _close(conn)
    return row


def update_parsed_transaction(tx_id, **kwargs):
    if not kwargs:
        return
    allowed = {"merchant", "amount", "date", "category", "subcategory", "account", "tx_type", "confidence", "notes", "confirmed"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k == "merchant":
            v = (v or "").strip()
        elif k == "category":
            v = (v or "").strip()
        elif k == "subcategory":
            v = (v or "").strip()
        elif k == "account":
            v = (v or "").strip()
        elif k == "tx_type":
            v = (v or "Expense").strip().capitalize()
            if v not in ("Expense", "Income"):
                v = "Expense"
        elif k == "confidence":
            v = (v or "").strip().lower()
        elif k == "notes":
            v = (v or "").strip()
        elif k == "amount":
            v = float(v)
        elif k == "confirmed":
            v = int(v)
        fields.append(f"{k} = ?")
        values.append(v)
    if not fields:
        return
    values.append(tx_id)
    conn = get_connection()
    _execute(conn, _q(f"UPDATE parsed_transactions SET {', '.join(fields)} WHERE id = ?"), values)
    _commit(conn)
    _close(conn)


def update_parse_session_status(session_id, status):
    status = status.strip().lower()
    conn = get_connection()
    _execute(conn, _q("UPDATE parse_sessions SET status = ? WHERE id = ?"), (status, session_id))
    _commit(conn)
    _close(conn)


def delete_parse_session(session_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM parsed_transactions WHERE session_id = ?"), (session_id,))
    _execute(conn, _q("DELETE FROM parse_sessions WHERE id = ?"), (session_id,))
    _commit(conn)
    _close(conn)


# --- Bills ---


def add_bill(user_id, card_name, amount, due_date, card_last4="", description=""):
    card_name = (card_name or "").strip().lower()
    card_last4 = (card_last4 or "").strip()
    description = (description or "").strip()
    conn = get_connection()
    cur = _execute(conn,
        _q("INSERT INTO bills (user_id, card_name, card_last4, amount, due_date, description) VALUES (?, ?, ?, ?, ?, ?)"),
        (user_id, card_name, card_last4, float(amount), due_date, description),
    )
    _commit(conn)
    bill_id = cur.lastrowid if not USE_PG else cur.lastrowid
    if USE_PG:
        cur2 = _execute(conn, "SELECT lastval()")
        r = _fetchone(cur2)
        bill_id = r["lastval"] if r else None
    _close(conn)
    return bill_id


def get_bills(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, card_name, card_last4, amount, due_date, description, paid, notified_3d, notified_today, created_at FROM bills WHERE user_id = ? AND paid = 0 ORDER BY due_date"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_bill(bill_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, card_name, card_last4, amount, due_date, description, paid, notified_3d, notified_today, created_at FROM bills WHERE id = ?"), (bill_id,))
    row = _fetchone(cur)
    _close(conn)
    return row


def pay_bill(bill_id):
    conn = get_connection()
    _execute(conn, _q("UPDATE bills SET paid = 1 WHERE id = ?"), (bill_id,))
    _commit(conn)
    _close(conn)


def remove_bill(bill_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM bills WHERE id = ?"), (bill_id,))
    _commit(conn)
    _close(conn)


def mark_bill_notified(bill_id):
    conn = get_connection()
    _execute(conn, _q("UPDATE bills SET notified_3d = 1 WHERE id = ?"), (bill_id,))
    _commit(conn)
    _close(conn)


def mark_bill_notified_today(bill_id):
    conn = get_connection()
    _execute(conn, _q("UPDATE bills SET notified_today = 1 WHERE id = ?"), (bill_id,))
    _commit(conn)
    _close(conn)


def get_bills_due_today(user_id):
    today_str = date.today().isoformat()
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, user_id, card_name, card_last4, amount, due_date, description, paid, notified_3d, notified_today, created_at FROM bills WHERE user_id = ? AND paid = 0 AND notified_today = 0 AND due_date = ? ORDER BY id"),
        (user_id, today_str),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_bills_due_soon(user_id, days=3):
    today = date.today()
    cutoff = (today + timedelta(days=days)).isoformat()
    today_str = today.isoformat()
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, user_id, card_name, card_last4, amount, due_date, description, paid, notified_3d, notified_today, created_at FROM bills WHERE user_id = ? AND paid = 0 AND notified_3d = 0 AND due_date > ? AND due_date <= ? ORDER BY due_date"),
        (user_id, today_str, cutoff),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


# --- Monthly Reminders ---


def get_monthly_reminders(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, message, day_of_month, last_notified_month, enabled, created_at FROM monthly_reminders WHERE user_id = ? AND enabled = 1 ORDER BY id"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def add_monthly_reminder(user_id, message, day_of_month=15):
    conn = get_connection()
    cur = _execute(conn,
        _q("INSERT INTO monthly_reminders (user_id, message, day_of_month) VALUES (?, ?, ?)"),
        (user_id, message, int(day_of_month)),
    )
    _commit(conn)
    rid = cur.lastrowid if not USE_PG else cur.lastrowid
    if USE_PG:
        cur2 = _execute(conn, "SELECT lastval()")
        r = _fetchone(cur2)
        rid = r["lastval"] if r else None
    _close(conn)
    return rid


def mark_monthly_notified(reminder_id, month_str):
    conn = get_connection()
    _execute(conn, _q("UPDATE monthly_reminders SET last_notified_month = ? WHERE id = ?"), (month_str, reminder_id))
    _commit(conn)
    _close(conn)


def get_monthly_reminders_due(user_id, day_of_month):
    month_str = date.today().strftime("%Y-%m")
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, user_id, message, day_of_month, last_notified_month, enabled, created_at FROM monthly_reminders WHERE user_id = ? AND enabled = 1 AND day_of_month = ? AND (last_notified_month IS NULL OR last_notified_month != ?)"),
        (user_id, int(day_of_month), month_str),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


# --- Cards (card registry for bills) ---


def add_card(user_id, card_name, card_last4=""):
    card_name = (card_name or "").strip().lower()
    card_last4 = (card_last4 or "").strip()
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO cards (user_id, card_name, card_last4) VALUES (?, ?, ?) ON CONFLICT(user_id, card_name) DO UPDATE SET card_last4 = excluded.card_last4"),
        (user_id, card_name, card_last4),
    )
    _commit(conn)
    _close(conn)


def get_cards(user_id):
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, card_name, card_last4, created_at FROM cards WHERE user_id = ? ORDER BY card_name"), (user_id,))
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_card(user_id, card_name):
    card_name = (card_name or "").strip().lower()
    conn = get_connection()
    cur = _execute(conn, _q("SELECT id, user_id, card_name, card_last4, created_at FROM cards WHERE user_id = ? AND card_name = ?"), (user_id, card_name))
    row = _fetchone(cur)
    _close(conn)
    return row


def get_card_last4(user_id, card_name):
    card = get_card(user_id, card_name)
    if card:
        return card["card_last4"] or ""
    return ""


def remove_card(user_id, card_name):
    card_name = (card_name or "").strip().lower()
    conn = get_connection()
    _execute(conn, _q("DELETE FROM cards WHERE user_id = ? AND card_name = ?"), (user_id, card_name))
    _commit(conn)
    _close(conn)


# --- Token Usage Tracking ---


def record_token_usage(user_id, prompt_tokens, completion_tokens, cache_hit_tokens, model=""):
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO token_usage (user_id, prompt_tokens, completion_tokens, cache_hit_tokens, model) VALUES (?, ?, ?, ?, ?)"),
        (int(user_id), int(prompt_tokens), int(completion_tokens), int(cache_hit_tokens), model or ""),
    )
    _commit(conn)
    _close(conn)


def get_token_usage_today(user_id=None):
    today = date.today().isoformat()
    conn = get_connection()
    if user_id:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage WHERE created_at = ? AND user_id = ?"),
            (today, user_id))
    else:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage WHERE created_at = ?"),
            (today,))
    row = _fetchone(cur)
    _close(conn)
    return row or {"p": 0, "c": 0, "h": 0, "n": 0}


def get_token_usage_month(user_id=None):
    today = date.today()
    month_start = f"{today.year:04d}-{today.month:02d}-01"
    conn = get_connection()
    if user_id:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage WHERE created_at >= ? AND user_id = ?"),
            (month_start, user_id))
    else:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage WHERE created_at >= ?"),
            (month_start,))
    row = _fetchone(cur)
    _close(conn)
    return row or {"p": 0, "c": 0, "h": 0, "n": 0}


def get_token_usage_lifetime(user_id=None):
    conn = get_connection()
    if user_id:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage WHERE user_id = ?"),
            (user_id,))
    else:
        cur = _execute(conn,
            _q("SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, COALESCE(SUM(cache_hit_tokens),0) as h, COUNT(*) as n FROM token_usage"))
    row = _fetchone(cur)
    _close(conn)
    return row or {"p": 0, "c": 0, "h": 0, "n": 0}


# --- Batch Pantry Operations ---


def add_pantry_items_with_expiry(user_id, items):
    """items: list of (name, expiry_iso_or_empty) tuples. Single connection."""
    conn = get_connection()
    for name, expiry in items:
        name = name.strip().lower()
        category = categorize_item(name)
        _execute(conn,
            _q("INSERT INTO pantry (user_id, name, quantity, unit, expiry_date, category) VALUES (?, ?, '1', '', ?, ?) ON CONFLICT(user_id, name) DO UPDATE SET quantity = excluded.quantity, unit = excluded.unit, expiry_date = COALESCE(excluded.expiry_date, pantry.expiry_date), category = COALESCE(excluded.category, pantry.category)"),
            (user_id, name, expiry if expiry else None, category),
        )
    _commit(conn)
    _close(conn)


def remove_pantry_items(user_id, names):
    """Batch remove multiple pantry items. Single connection."""
    conn = get_connection()
    for name in names:
        _execute(conn, _q("DELETE FROM pantry WHERE user_id = ? AND name = ?"),
                     (user_id, name.strip().lower()))
    _commit(conn)
    _close(conn)


# --- Vouchers ---


def add_voucher(user_id, name, details, expiry_date):
    name = name.strip().title()
    conn = get_connection()
    _execute(conn,
        _q("INSERT INTO vouchers (user_id, name, details, expiry_date) VALUES (?, ?, ?, ?)"),
        (user_id, name, (details or "").strip(), expiry_date),
    )
    _commit(conn)
    _close(conn)


def get_active_vouchers(user_id):
    today_str = date.today().isoformat()
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, user_id, name, details, expiry_date, created_at FROM vouchers WHERE user_id = ? AND (expiry_date IS NULL OR expiry_date >= ?) ORDER BY expiry_date"),
        (user_id, today_str),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def delete_voucher(voucher_id):
    conn = get_connection()
    _execute(conn, _q("DELETE FROM vouchers WHERE id = ?"), (voucher_id,))
    _commit(conn)
    _close(conn)


def get_vouchers_expiring_soon(user_id, days=7):
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    today_str = date.today().isoformat()
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, name, details, expiry_date FROM vouchers WHERE user_id = ? AND expiry_date > ? AND expiry_date <= ? ORDER BY expiry_date"),
        (user_id, today_str, cutoff),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows


def get_vouchers_expiring_in_month(user_id, year, month):
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    conn = get_connection()
    cur = _execute(conn,
        _q("SELECT id, name, details, expiry_date FROM vouchers WHERE user_id = ? AND expiry_date >= ? AND expiry_date < ? ORDER BY expiry_date"),
        (user_id, start, end),
    )
    rows = _fetchall(cur)
    _close(conn)
    return rows
