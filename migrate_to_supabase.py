"""Run this script once to copy all data from SQLite to Supabase PostgreSQL.
Usage: DATABASE_URL=postgresql://... python migrate_to_supabase.py
"""
import sqlite3
import json
import os
import sys
from datetime import date, timedelta

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgres://"):
    print("ERROR: Set DATABASE_URL env var to your Supabase connection string")
    print("Example: $env:DATABASE_URL='postgresql://user:pass@host:6543/postgres'")
    sys.exit(1)

import psycopg2
from config import DB_PATH

pg = psycopg2.connect(DATABASE_URL)
pg.autocommit = False
cur = pg.cursor()

sqlite = sqlite3.connect(DB_PATH)
sqlite.row_factory = sqlite3.Row

# --- Create tables on PostgreSQL ---
print("Creating tables on Supabase...")
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
pg.commit()
print("Tables created.")

# --- Migrate pantry ---
print("\nMigrating pantry...")
rows = sqlite.execute("SELECT * FROM pantry").fetchall()
count = 0
for r in rows:
    try:
        cur.execute(
            "INSERT INTO pantry (user_id, name, quantity, unit, expiry_date, added_date, category) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, name) DO NOTHING",
            (r["user_id"], r["name"], r["quantity"], r["unit"], r["expiry_date"], r["added_date"], r["category"])
        )
        count += 1
    except Exception as e:
        print(f"  Skip pantry {r['name']}: {e}")
pg.commit()
print(f"  {count} items migrated")

# --- Migrate recipes ---
print("\nMigrating recipes...")
rows = sqlite.execute("SELECT * FROM recipes").fetchall()
count = 0
for r in rows:
    try:
        cur.execute(
            "INSERT INTO recipes (user_id, title, description, ingredients, instructions, full_text, cuisine, protein_g, calories, sodium_mg, saved_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (r["user_id"], r["title"], r["description"], r["ingredients"], r["instructions"],
             r["full_text"] if "full_text" in r else "", r["cuisine"], r["protein_g"], r["calories"], r["sodium_mg"], r["saved_date"])
        )
        count += 1
    except Exception as e:
        print(f"  Skip recipe {r['title']}: {e}")
pg.commit()
print(f"  {count} recipes migrated")

# --- Migrate meal_logs ---
print("\nMigrating meal logs...")
rows = sqlite.execute("SELECT * FROM meal_logs").fetchall()
count = 0
for r in rows:
    try:
        cur.execute(
            "INSERT INTO meal_logs (user_id, meal_name, calories, protein_g, sodium_mg, logged_date, meal_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (r["user_id"], r["meal_name"], r["calories"], r["protein_g"], r["sodium_mg"], r["logged_date"], r["meal_type"])
        )
        count += 1
    except Exception as e:
        print(f"  Skip log: {e}")
pg.commit()
print(f"  {count} logs migrated")

# --- Migrate user_goals ---
print("\nMigrating user goals...")
rows = sqlite.execute("SELECT * FROM user_goals").fetchall()
count = 0
for r in rows:
    try:
        cur.execute(
            "INSERT INTO user_goals (user_id, daily_calories, daily_protein, daily_sodium) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (r["user_id"], r["daily_calories"], r["daily_protein"], r["daily_sodium"])
        )
        count += 1
    except Exception as e:
        print(f"  Skip goal: {e}")
pg.commit()
print(f"  {count} goals migrated")

# --- Migrate user_preferences ---
print("\nMigrating user preferences...")
rows = sqlite.execute("SELECT * FROM user_preferences").fetchall()
count = 0
for r in rows:
    try:
        cur.execute(
            "INSERT INTO user_preferences (user_id, key, value) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, key) DO NOTHING",
            (r["user_id"], r["key"], r["value"])
        )
        count += 1
    except Exception as e:
        print(f"  Skip pref: {e}")
pg.commit()
print(f"  {count} preferences migrated")

sqlite.close()
pg.close()
print("\nMigration complete!")
