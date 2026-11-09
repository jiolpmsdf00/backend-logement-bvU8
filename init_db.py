"""Importe db.sql une seule fois (si la table users n'existe pas). Ne touche jamais aux données existantes."""
import re
from sqlalchemy import inspect, text
from db import engine

if inspect(engine).has_table("users"):
    print("Schéma déjà présent, rien à faire.")
else:
    sql = open("db.sql", encoding="utf-8").read()
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.M)
    with engine.begin() as c:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            c.exec_driver_sql(stmt.replace("%", "%%"))
    print("Schéma db.sql importé.")
