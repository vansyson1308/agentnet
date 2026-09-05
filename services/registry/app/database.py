from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# DATABASE_URL is built in app.config which validates secrets fail-fast.
from .config import DATABASE_URL

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,        # Detect stale connections before use
    pool_recycle=3600,         # Recycle connections after 1 hour (prevent PG idle kill)
    pool_size=10,
    max_overflow=20,
)

# Create sessionmaker
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Declarative base, SQLAlchemy 2.0 style (the 1.x factory function lived in
# a namespace that now raises MovedIn20Warning). Models keep their
# ``Column()`` attributes; DeclarativeBase supports them unchanged.
class Base(DeclarativeBase):
    pass


# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
