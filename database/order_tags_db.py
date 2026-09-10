"""User annotations for broker orders — a Type tag + a free-text description.

The broker orderbook has no place for the trader's own notes, so we keep a
local table keyed by ``orderid``. These are pure UI annotations (never sent to
the broker) and persist across sessions so a tagged order stays tagged.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from database.backtest_db import Base, db_session, engine
from utils.logging import get_logger

logger = get_logger(__name__)

# Predefined tag types offered in the dropdown (free-text still allowed via API).
TAG_TYPES = [
    "Test",
    "Scalping",
    "Indexing",
    "Momentum",
    "Hedge",
    "Arbitrage",
    "Investment",
    "Other",
]


class OrderTag(Base):
    __tablename__ = "order_tags"

    orderid = Column(String(64), primary_key=True)
    tag_type = Column(String(40), default="")
    description = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def get_all_order_tags() -> dict[str, dict[str, str]]:
    """Return every tag keyed by orderid → {tag_type, description}."""
    try:
        return {
            r.orderid: {"tag_type": r.tag_type or "", "description": r.description or ""}
            for r in db_session.query(OrderTag).all()
        }
    except Exception:
        logger.exception("Failed to read order tags")
        return {}
    finally:
        db_session.remove()


def upsert_order_tag(orderid: str, tag_type: str, description: str) -> bool:
    """Create or update the tag for one order. Returns success."""
    if not orderid:
        return False
    try:
        row = db_session.query(OrderTag).filter_by(orderid=orderid).first()
        if row:
            row.tag_type = tag_type or ""
            row.description = description or ""
        else:
            db_session.add(OrderTag(
                orderid=orderid, tag_type=tag_type or "", description=description or ""
            ))
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to upsert order tag for %s", orderid)
        return False
    finally:
        db_session.remove()
