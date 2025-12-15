# database.py
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# если есть переменная окружения DB_FILE (например, /data/employee_stats.db) — используем её
db_file = os.getenv("DB_FILE", "employee_stats.db")
DB_PATH = Path(db_file)
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Lightweight migrations / seed data
    with engine.begin() as conn:
        # Teams table (older DBs may have only key+name)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS teams (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                target_daily INTEGER NOT NULL DEFAULT 4000000,
                target_weekly INTEGER NOT NULL DEFAULT 24000000
            )
        """))

        # Add missing columns if upgrading an existing DB
        tinfo = conn.execute(text("PRAGMA table_info(teams)")).fetchall()
        tcols = [r[1] for r in tinfo]
        if "target_daily" not in tcols:
            conn.execute(text("ALTER TABLE teams ADD COLUMN target_daily INTEGER DEFAULT 4000000"))
        if "target_weekly" not in tcols:
            conn.execute(text("ALTER TABLE teams ADD COLUMN target_weekly INTEGER DEFAULT 24000000"))

        # Seed teams (with defaults)
        conn.execute(text("""
            INSERT OR IGNORE INTO teams (key, name, target_daily, target_weekly) VALUES
            ('left','Левая команда', 4000000, 24000000),
            ('right','Правая команда', 4200000, 25000000)
        """))

        # Ensure non-null targets
        conn.execute(text("UPDATE teams SET target_daily = COALESCE(target_daily, 4000000) WHERE target_daily IS NULL"))
        conn.execute(text("UPDATE teams SET target_weekly = COALESCE(target_weekly, 24000000) WHERE target_weekly IS NULL"))

        # If the DB comes from an older version (global targets), set new defaults for the right team
        conn.execute(text("""
            UPDATE teams
               SET target_daily = 4200000,
                   target_weekly = 25000000
             WHERE key = 'right'
               AND (target_daily IS NULL OR target_daily = 4000000)
               AND (target_weekly IS NULL OR target_weekly = 24000000)
        """))

        # Employees table migration: team_key
        einfo = conn.execute(text("PRAGMA table_info(employees)")).fetchall()
        ecols = [r[1] for r in einfo]
        if "team_key" not in ecols:
            conn.execute(text("ALTER TABLE employees ADD COLUMN team_key TEXT DEFAULT 'left'"))
            conn.execute(text("UPDATE employees SET team_key = COALESCE(team_key, 'left')"))
