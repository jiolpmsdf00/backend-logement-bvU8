"""Au déploiement : importe db.sql si le schéma est absent, puis insère insert.sql
uniquement si amenities, users et listings sont vides (ne touche jamais une base remplie)."""
import re
from sqlalchemy import inspect, text
from db import engine


def run(path):
    sql = open(path, encoding="utf-8").read()
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.M)
    with engine.begin() as c:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            c.exec_driver_sql(stmt.replace("%", "%%"))


if not inspect(engine).has_table("users"):
    run("db.sql")
    print("Schéma db.sql importé.")

with engine.connect() as c:
    empty = all(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0
                for t in ("amenities", "users", "listings"))
if empty:
    run("insert.sql")
    print("insert.sql importé.")
else:
    print("Données déjà présentes, rien à faire.")
