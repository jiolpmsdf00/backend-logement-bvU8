"""Database engine, session, and ORM models for StayHaven."""
from __future__ import annotations

import os
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, Boolean,
    DateTime, Date, ForeignKey, Enum as SAEnum, Numeric, UniqueConstraint,
    Index, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from sqlalchemy.exc import OperationalError

Base = declarative_base()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root@localhost:3306/stayhaven",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency: yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── ORM Models ───────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    first_name = Column(String(80), nullable=False)
    last_name = Column(String(80), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    phone = Column(String(32), nullable=True)
    role = Column(SAEnum("user", "host", "admin", name="user_role"),
                  nullable=False, default="user")
    avatar_url = Column(String(512), nullable=True)
    stripe_account_id = Column(String(255), nullable=True)
    stripe_onboarding_completed = Column(Boolean, nullable=False, default=False)
    stripe_charges_enabled = Column(Boolean, nullable=False, default=False)
    stripe_payouts_enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False,
                        default=datetime.utcnow, onupdate=datetime.utcnow)

    listings = relationship("Listing", back_populates="owner",
                            cascade="all, delete-orphan")
    favorites = relationship("Favorite", back_populates="user",
                             cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="user",
                            cascade="all, delete-orphan")


class Listing(Base):
    __tablename__ = "listings"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    owner_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    country = Column(String(100), nullable=False)
    city = Column(String(100), nullable=False, index=True)
    address = Column(String(255), nullable=False)
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)
    price_per_night = Column(Numeric(10, 2), nullable=False, default=0)
    cleaning_fee = Column(Numeric(10, 2), nullable=False, default=0)
    service_fee = Column(Numeric(10, 2), nullable=False, default=0)
    max_guests = Column(Integer, nullable=False, default=1)
    bedrooms = Column(Integer, nullable=False, default=0)
    beds = Column(Integer, nullable=False, default=0)
    bathrooms = Column(Integer, nullable=False, default=0)
    property_type = Column(
        SAEnum("apartment", "house", "cabin", "villa", "loft",
               "studio", "cottage", "chalet", name="property_type"),
        nullable=False, default="apartment")
    status = Column(SAEnum("draft", "published", "unpublished",
                           name="listing_status"),
                    nullable=False, default="draft", index=True)
    stripe_connected_account_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False,
                        default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="listings")
    images = relationship("ListingImage", back_populates="listing",
                          cascade="all, delete-orphan",
                          order_by="ListingImage.position")
    amenities = relationship("Amenity", secondary="listing_amenities",
                             back_populates="listings")
    favorites = relationship("Favorite", back_populates="listing",
                             cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="listing",
                            cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="listing",
                           cascade="all, delete-orphan")


class ListingImage(Base):
    __tablename__ = "listing_images"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    listing_id = Column(BigInteger, ForeignKey("listings.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    image_url = Column(String(512), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    listing = relationship("Listing", back_populates="images")


class Amenity(Base):
    __tablename__ = "amenities"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False, unique=True)
    icon = Column(String(60), nullable=False, default="check")

    listings = relationship("Listing", secondary="listing_amenities",
                            back_populates="amenities")


class ListingAmenity(Base):
    __tablename__ = "listing_amenities"
    listing_id = Column(BigInteger, ForeignKey("listings.id", ondelete="CASCADE"),
                        primary_key=True)
    amenity_id = Column(BigInteger, ForeignKey("amenities.id", ondelete="CASCADE"),
                        primary_key=True)


class Favorite(Base):
    __tablename__ = "favorites"
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
    listing_id = Column(BigInteger, ForeignKey("listings.id", ondelete="CASCADE"),
                        primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="favorites")
    listing = relationship("Listing", back_populates="favorites")


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    listing_id = Column(BigInteger, ForeignKey("listings.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    check_in = Column(Date, nullable=False)
    check_out = Column(Date, nullable=False)
    guests = Column(Integer, nullable=False, default=1)
    nights = Column(Integer, nullable=False)
    price_per_night = Column(Numeric(10, 2), nullable=False)
    cleaning_fee = Column(Numeric(10, 2), nullable=False, default=0)
    service_fee = Column(Numeric(10, 2), nullable=False, default=0)
    subtotal = Column(Numeric(10, 2), nullable=False)
    total = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="usd")
    status = Column(SAEnum("pending", "paid", "failed", "cancelled", "refunded",
                           name="booking_status"),
                    nullable=False, default="pending", index=True)
    stripe_payment_intent_id = Column(String(255), nullable=True)
    stripe_checkout_session_id = Column(String(255), nullable=True)
    stripe_connected_account_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False,
                        default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="bookings")
    listing = relationship("Listing", back_populates="bookings")
    reviews = relationship("Review", back_populates="booking",
                           cascade="all, delete-orphan")


class Review(Base):
    __tablename__ = "reviews"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False)
    listing_id = Column(BigInteger, ForeignKey("listings.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    booking_id = Column(BigInteger, ForeignKey("bookings.id", ondelete="SET NULL"),
                        nullable=True, unique=True)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User")
    listing = relationship("Listing", back_populates="reviews")
    booking = relationship("Booking", back_populates="reviews")
