import json
import os
import sys
from decimal import Decimal

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json

JSON_FILE = "menu-clean.json"
DB_NAME = os.getenv("MENU_DB_NAME", "restaurant_menu")


def connect(database):
    return psycopg2.connect(
        dbname=database,
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        user=os.getenv("PGUSER", "postgres"),
        password=2024
    )


def load_and_check():
    with open(JSON_FILE, encoding="utf-8") as file:
        data = json.load(file)
    assert data["restaurantId"] and data["menus"], "restaurantId and menus are required"
    counts = {"menus": 0, "categories": 0, "subcategories": 0, "items": 0, "options": 0, "choices": 0}
    for menu in data["menus"]:
        assert menu["id"] and menu["name"] and menu["timing"]
        counts["menus"] += 1
        for category in menu["categories"]:
            assert category["id"] and category["name"]
            counts["categories"] += 1
            for subcategory in category["subcategories"]:
                assert subcategory["id"] and subcategory["name"]
                counts["subcategories"] += 1
                for item in subcategory["items"]:
                    assert item["id"] and item["name"] and Decimal(str(item["price"])) >= 0
                    counts["items"] += 1
                    for option in item.get("options", []):
                        assert option["id"] and option["name"]
                        counts["options"] += 1
                        for choice in option["choices"]:
                            assert choice["id"] and choice["name"] and Decimal(str(choice["price"])) >= 0
                            counts["choices"] += 1
    return data, counts


def ensure_database():
    connection = connect("postgres")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cursor.fetchone():
            cursor.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(DB_NAME)))
    connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (id BIGINT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS menus (
  id BIGINT PRIMARY KEY, restaurant_id BIGINT NOT NULL REFERENCES restaurants(id), name TEXT NOT NULL,
  description TEXT, opens TEXT, closes TEXT, days JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
  id BIGINT PRIMARY KEY, menu_id BIGINT NOT NULL REFERENCES menus(id), name TEXT NOT NULL,
  description TEXT, available BOOLEAN NOT NULL, opens TEXT, closes TEXT, days JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS subcategories (
  id BIGINT PRIMARY KEY, category_id BIGINT NOT NULL REFERENCES categories(id), name TEXT NOT NULL,
  description TEXT, available BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
  id BIGINT PRIMARY KEY, subcategory_id BIGINT NOT NULL REFERENCES subcategories(id), name TEXT NOT NULL,
  description TEXT, price NUMERIC(12,2) NOT NULL, discount_price NUMERIC(12,2), deal BOOLEAN NOT NULL, available BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS option_groups (
  id BIGINT PRIMARY KEY, item_id BIGINT NOT NULL REFERENCES items(id), name TEXT NOT NULL, description TEXT,
  required BOOLEAN NOT NULL, multiple BOOLEAN NOT NULL, min_selections INTEGER NOT NULL, max_selections INTEGER NOT NULL,
  linked_option_id BIGINT
);
CREATE TABLE IF NOT EXISTS option_choices (
  id BIGINT PRIMARY KEY, option_group_id BIGINT NOT NULL REFERENCES option_groups(id), name TEXT NOT NULL,
  description TEXT, price NUMERIC(12,2) NOT NULL, discount_price NUMERIC(12,2), linked_choice_id BIGINT
);
"""


def upsert(cursor, table, values, columns):
    columns_sql = sql.SQL(", ").join(map(sql.Identifier, columns))
    if len(columns) == 1:
        cursor.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING").format(
                sql.Identifier(table), columns_sql, sql.Placeholder(), sql.Identifier(columns[0])
            ),
            values,
        )
        return
    updates = sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(column)) for column in columns[1:])
    cursor.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(table), columns_sql, sql.SQL(", ").join(sql.Placeholder() * len(columns)), sql.Identifier(columns[0]), updates
        ),
        values,
    )


def import_data(data):
    ensure_database()
    connection = connect(DB_NAME)
    with connection, connection.cursor() as cursor:
        cursor.execute(SCHEMA)
        upsert(cursor, "restaurants", (data["restaurantId"],), ["id"])
        for menu in data["menus"]:
            upsert(cursor, "menus", (menu["id"], data["restaurantId"], menu["name"], menu.get("description"), menu["timing"].get("opens"), menu["timing"].get("closes"), Json(menu["timing"]["days"])), ["id", "restaurant_id", "name", "description", "opens", "closes", "days"])
            for category in menu["categories"]:
                upsert(cursor, "categories", (category["id"], menu["id"], category["name"], category.get("description"), category["available"], category["timing"].get("opens"), category["timing"].get("closes"), Json(category["timing"]["days"])), ["id", "menu_id", "name", "description", "available", "opens", "closes", "days"])
                for subcategory in category["subcategories"]:
                    upsert(cursor, "subcategories", (subcategory["id"], category["id"], subcategory["name"], subcategory.get("description"), subcategory["available"]), ["id", "category_id", "name", "description", "available"])
                    for item in subcategory["items"]:
                        upsert(cursor, "items", (item["id"], subcategory["id"], item["name"], item.get("description"), item["price"], item.get("discountPrice"), item["deal"], item["available"]), ["id", "subcategory_id", "name", "description", "price", "discount_price", "deal", "available"])
                        for option in item.get("options", []):
                            upsert(cursor, "option_groups", (option["id"], item["id"], option["name"], option.get("description"), option["required"], option["multiple"], option["minSelections"], option["maxSelections"], option.get("linkedOptionId")), ["id", "item_id", "name", "description", "required", "multiple", "min_selections", "max_selections", "linked_option_id"])
                            for choice in option["choices"]:
                                upsert(cursor, "option_choices", (choice["id"], option["id"], choice["name"], choice.get("description"), choice["price"], choice.get("discountPrice"), choice.get("linkedChoiceId")), ["id", "option_group_id", "name", "description", "price", "discount_price", "linked_choice_id"])
    connection.close()


if __name__ == "__main__":
    data, counts = load_and_check()
    print("JSON is valid:", counts)
    if "--check-only" not in sys.argv:
        import_data(data)
        print(f"Imported into PostgreSQL database: {DB_NAME}")
