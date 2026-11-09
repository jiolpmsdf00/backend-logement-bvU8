"""Security, auth, and shared business logic for StayHaven."""
from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, date
from typing import Optional
from decimal import Decimal

from passlib.context import CryptContext
from jose import jwt, JWTError
from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, select

from db import User, Listing, Booking, Favorite, SessionLocal, get_db

logger = logging.getLogger("stayhaven")


pwd_context = CryptContext(
    schemes=["bcrypt_sha256"],
    deprecated="auto"
)

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://book44me.vercel.app")

security_scheme = HTTPBearer(auto_error=False)


# ─── Password ──────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


# ─── JWT ───────────────────────────────────────────────────────

def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": str(user_id), "role": role, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


# ─── Auth Dependencies ─────────────────────────────────────────

def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(creds.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = int(payload.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_current_user_optional(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if creds is None:
        return None
    payload = decode_token(creds.credentials)
    if payload is None:
        return None
    user_id = int(payload.get("sub", 0))
    return db.query(User).filter(User.id == user_id).first()


def require_host(user: User = Depends(get_current_user)) -> User:
    if user.role not in ("host", "admin"):
        raise HTTPException(status_code=403, detail="Host access required")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ─── Validation ────────────────────────────────────────────────

def validate_email(email: str) -> bool:
    return "@" in email and "." in email and len(email) <= 255


def validate_password(password: str) -> bool:
    return len(password) >= 6


# ─── Booking helpers ───────────────────────────────────────────

def check_date_overlap(db: Session, listing_id: int,
                       check_in: date, check_out: date,
                       exclude_booking_id: Optional[int] = None) -> bool:
    """Return True if any confirmed/pending booking overlaps the given dates."""
    q = db.query(Booking).filter(
        Booking.listing_id == listing_id,
        Booking.status.in_(["pending", "paid"]),
        Booking.check_in < check_out,
        Booking.check_out > check_in,
    )
    if exclude_booking_id:
        q = q.filter(Booking.id != exclude_booking_id)
    return q.first() is not None


def calculate_booking_price(listing: Listing, check_in: date,
                            check_out: date) -> dict:
    nights = (check_out - check_in).days
    price_per_night = Decimal(str(listing.price_per_night))
    cleaning_fee = Decimal(str(listing.cleaning_fee))
    service_fee = Decimal(str(listing.service_fee))
    subtotal = price_per_night * nights
    total = subtotal + cleaning_fee + service_fee
    return {
        "nights": nights,
        "price_per_night": float(price_per_night),
        "cleaning_fee": float(cleaning_fee),
        "service_fee": float(service_fee),
        "subtotal": float(subtotal),
        "total": float(total),
    }


def validate_booking_dates(check_in: date, check_out: date) -> None:
    today = date.today()
    if check_in < today:
        raise HTTPException(400, "Check-in date cannot be in the past")
    if check_out <= check_in:
        raise HTTPException(400, "Check-out must be after check-in")
    if (check_out - check_in).days > 365:
        raise HTTPException(400, "Booking cannot exceed 365 nights")


# ─── Serialization ─────────────────────────────────────────────

def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def serialize_listing(listing: Listing, include_owner: bool = False) -> dict:
    result = {
        "id": listing.id,
        "owner_id": listing.owner_id,
        "title": listing.title,
        "description": listing.description,
        "country": listing.country,
        "city": listing.city,
        "address": listing.address,
        "latitude": float(listing.latitude) if listing.latitude else None,
        "longitude": float(listing.longitude) if listing.longitude else None,
        "price_per_night": float(listing.price_per_night),
        "cleaning_fee": float(listing.cleaning_fee),
        "service_fee": float(listing.service_fee),
        "max_guests": listing.max_guests,
        "bedrooms": listing.bedrooms,
        "beds": listing.beds,
        "bathrooms": listing.bathrooms,
        "property_type": listing.property_type,
        "status": listing.status,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "updated_at": listing.updated_at.isoformat() if listing.updated_at else None,
        "images": [
            {"id": img.id, "image_url": img.image_url, "position": img.position}
            for img in (listing.images or [])
        ],
        "amenities": [
            {"id": a.id, "name": a.name, "icon": a.icon}
            for a in (listing.amenities or [])
        ],
    }
    if include_owner and listing.owner:
        result["owner"] = {
            "id": listing.owner.id,
            "first_name": listing.owner.first_name,
            "last_name": listing.owner.last_name,
            "avatar_url": listing.owner.avatar_url,
        }
    return result


def serialize_booking(booking: Booking, listing: Optional[Listing] = None) -> dict:
    data = {
        "id": booking.id,
        "user_id": booking.user_id,
        "listing_id": booking.listing_id,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "guests": booking.guests,
        "nights": booking.nights,
        "price_per_night": float(booking.price_per_night),
        "cleaning_fee": float(booking.cleaning_fee),
        "service_fee": float(booking.service_fee),
        "subtotal": float(booking.subtotal),
        "total": float(booking.total),
        "currency": booking.currency,
        "status": booking.status,
        "stripe_payment_intent_id": booking.stripe_payment_intent_id,
        "stripe_checkout_session_id": booking.stripe_checkout_session_id,
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
    }
    if listing:
        data["listing"] = {
            "id": listing.id,
            "title": listing.title,
            "city": listing.city,
            "country": listing.country,
            "images": [
                {"image_url": img.image_url} for img in (listing.images or [])
            ],
        }
    return data


def serialize_review(review: Review) -> dict:
    return {
        "id": review.id,
        "user_id": review.user_id,
        "listing_id": review.listing_id,
        "booking_id": review.booking_id,
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "user": {
            "first_name": review.user.first_name if review.user else "",
            "last_name": review.user.last_name if review.user else "",
        } if review.user else None,
    }


from db import Review  # noqa: E402
