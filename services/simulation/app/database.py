from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# DATABASE_URL is built in app.config, which validates POSTGRES_PASSWORD
# fail-fast (no hard-coded fallback password / host in this module).
from .config import DATABASE_URL

# Create SQLAlchemy engine (same resilience settings as the registry).
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # Detect stale connections before use
    pool_recycle=3600,   # Recycle connections after 1 hour (prevent PG idle kill)
)

# Create sessionmaker
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()


# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
