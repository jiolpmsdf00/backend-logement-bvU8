"""StayHaven Marketplace — FastAPI application."""
from __future__ import annotations

# ── Charger le .env AVANT tout le reste (db.py et core.py lisent os.getenv à l'import)
from pathlib import Path
from dotenv import load_dotenv

# override=False : les variables Railway priment sur le .env local
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

import os
import json
import logging
from datetime import date, datetime
from typing import Optional, List
from decimal import Decimal

import stripe
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_, asc, desc, func

from db import (
    get_db, SessionLocal, User, Listing, ListingImage, Amenity, ListingAmenity,
    Favorite, Booking, Review, Base, engine,
)
from core import (
    hash_password, verify_password, create_access_token, decode_token,
    get_current_user, get_current_user_optional, require_host, require_admin,
    validate_email, validate_password,
    check_date_overlap, calculate_booking_price, validate_booking_dates,
    serialize_user, serialize_listing, serialize_booking, serialize_review,
    FRONTEND_URL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stayhaven")

# Clé Stripe de la plateforme : TOUS les paiements sont encaissés sur ce compte.
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

if not stripe.api_key or stripe.api_key.startswith("REMPLACEZ") or stripe.api_key == "sk_test_xxx":
    logger.error("STRIPE_SECRET_KEY manquante ou invalide : vérifiez le fichier .env à côté de app.py")

app = FastAPI(title="StayHaven API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic Schemas ──────────────────────────────────────────

class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    password: str
    phone: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ListingCreate(BaseModel):
    title: str
    description: Optional[str] = None
    country: str
    city: str
    address: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_night: float
    cleaning_fee: float = 0
    service_fee: float = 0
    max_guests: int = 1
    bedrooms: int = 0
    beds: int = 0
    bathrooms: int = 0
    property_type: str = "apartment"
    amenity_ids: List[int] = []
    image_urls: List[str] = []


class ListingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_night: Optional[float] = None
    cleaning_fee: Optional[float] = None
    service_fee: Optional[float] = None
    max_guests: Optional[int] = None
    bedrooms: Optional[int] = None
    beds: Optional[int] = None
    bathrooms: Optional[int] = None
    property_type: Optional[str] = None
    status: Optional[str] = None
    amenity_ids: Optional[List[int]] = None
    image_urls: Optional[List[str]] = None


class BookingCreate(BaseModel):
    listing_id: int
    check_in: date
    check_out: date
    guests: int = 1


class ReviewCreate(BaseModel):
    booking_id: int
    rating: int
    comment: Optional[str] = None

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v):
        if not 1 <= v <= 5:
            raise ValueError("Rating must be 1-5")
        return v


class CheckoutCreate(BaseModel):
    booking_id: int


class BecomeHostRequest(BaseModel):
    pass


# ─── Auth Endpoints ────────────────────────────────────────────

@app.post("/api/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if not validate_email(email):
        raise HTTPException(422, "Invalid email address")
    if not validate_password(req.password):
        raise HTTPException(422, "Password must be at least 6 characters")
    if not req.first_name.strip() or not req.last_name.strip():
        raise HTTPException(422, "First and last name are required")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(409, "An account with this email already exists")

    user = User(
        first_name=req.first_name.strip(),
        last_name=req.last_name.strip(),
        email=email,
        password_hash=hash_password(req.password),
        phone=req.phone,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.role)
    logger.info(f"User registered: {email}")
    return {"token": token, "user": serialize_user(user)}


@app.post("/api/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    token = create_access_token(user.id, user.role)
    logger.info(f"User logged in: {email}")
    return {"token": token, "user": serialize_user(user)}


@app.post("/api/auth/logout")
def logout():
    return {"message": "Logged out"}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"user": serialize_user(user)}


@app.post("/api/auth/become-host")
def become_host(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role == "admin":
        raise HTTPException(400, "Admins cannot change role")
    user.role = "host"
    db.commit()
    db.refresh(user)
    return {"user": serialize_user(user)}


# ─── Listings (Public) ─────────────────────────────────────────

@app.get("/api/listings")
def search_listings(
    destination: Optional[str] = Query(None),
    check_in: Optional[str] = Query(None),
    check_out: Optional[str] = Query(None),
    guests: Optional[int] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    property_type: Optional[str] = Query(None),
    bedrooms: Optional[int] = Query(None),
    beds: Optional[int] = Query(None),
    bathrooms: Optional[int] = Query(None),
    amenities: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(Listing).options(
        joinedload(Listing.images), joinedload(Listing.amenities)
    ).filter(Listing.status == "published")

    if destination:
        dest = f"%{destination.strip()}%"
        q = q.filter(or_(
            Listing.city.ilike(dest),
            Listing.country.ilike(dest),
            Listing.address.ilike(dest),
        ))
    if min_price is not None:
        q = q.filter(Listing.price_per_night >= min_price)
    if max_price is not None:
        q = q.filter(Listing.price_per_night <= max_price)
    if property_type:
        q = q.filter(Listing.property_type == property_type)
    if bedrooms is not None:
        q = q.filter(Listing.bedrooms >= bedrooms)
    if beds is not None:
        q = q.filter(Listing.beds >= beds)
    if bathrooms is not None:
        q = q.filter(Listing.bathrooms >= bathrooms)
    if guests is not None:
        q = q.filter(Listing.max_guests >= guests)
    if amenities:
        amenity_ids = [int(x) for x in amenities.split(",") if x.strip()]
        for aid in amenity_ids:
            q = q.filter(
                Listing.amenities.any(Amenity.id == aid)
            )

    # Date availability filter
    excluded_ids = set()
    if check_in and check_out:
        try:
            ci = datetime.strptime(check_in, "%Y-%m-%d").date()
            co = datetime.strptime(check_out, "%Y-%m-%d").date()
            overlapping = db.query(Booking.listing_id).filter(
                Booking.status.in_(["pending", "paid"]),
                Booking.check_in < co,
                Booking.check_out > ci,
            ).distinct().all()
            excluded_ids = {r[0] for r in overlapping}
        except ValueError:
            pass

    total = q.count()
    offset = (page - 1) * limit
    listings = q.order_by(Listing.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for l in listings:
        if l.id in excluded_ids:
            continue
        result.append(serialize_listing(l))

    return {"listings": result, "total": total, "page": page, "limit": limit}


@app.get("/api/listings/{listing_id}")
def get_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    listing = db.query(Listing).options(
        joinedload(Listing.images),
        joinedload(Listing.amenities),
        joinedload(Listing.owner),
    ).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    if listing.status != "published" and (not user or user.id != listing.owner_id):
        if not user or user.role != "admin":
            raise HTTPException(404, "Listing not found")

    is_favorite = False
    if user:
        fav = db.query(Favorite).filter(
            Favorite.user_id == user.id, Favorite.listing_id == listing_id
        ).first()
        is_favorite = fav is not None

    reviews = db.query(Review).filter(Review.listing_id == listing_id).all()
    avg_rating = sum(r.rating for r in reviews) / len(reviews) if reviews else None

    data = serialize_listing(listing, include_owner=True)
    data["is_favorite"] = is_favorite
    data["reviews"] = [serialize_review(r) for r in reviews]
    data["avg_rating"] = round(avg_rating, 1) if avg_rating else None
    data["review_count"] = len(reviews)
    return data


# ─── Amenities (Public) ────────────────────────────────────────

@app.get("/api/amenities")
def list_amenities(db: Session = Depends(get_db)):
    amenities = db.query(Amenity).order_by(Amenity.name).all()
    return {"amenities": [
        {"id": a.id, "name": a.name, "icon": a.icon} for a in amenities
    ]}


# ─── Favorites ─────────────────────────────────────────────────

@app.get("/api/favorites")
def list_favorites(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    favs = db.query(Favorite).filter(Favorite.user_id == user.id).all()
    result = []
    for fav in favs:
        listing = db.query(Listing).options(
            joinedload(Listing.images), joinedload(Listing.amenities)
        ).filter(Listing.id == fav.listing_id).first()
        if listing:
            data = serialize_listing(listing)
            data["is_favorite"] = True
            result.append(data)
    return {"favorites": result}


@app.post("/api/favorites/{listing_id}")
def add_favorite(
    listing_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    existing = db.query(Favorite).filter(
        Favorite.user_id == user.id, Favorite.listing_id == listing_id
    ).first()
    if existing:
        return {"message": "Already in favorites"}
    fav = Favorite(user_id=user.id, listing_id=listing_id)
    db.add(fav)
    db.commit()
    return {"message": "Added to favorites"}


@app.delete("/api/favorites/{listing_id}")
def remove_favorite(
    listing_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fav = db.query(Favorite).filter(
        Favorite.user_id == user.id, Favorite.listing_id == listing_id
    ).first()
    if not fav:
        raise HTTPException(404, "Favorite not found")
    db.delete(fav)
    db.commit()
    return {"message": "Removed from favorites"}


# ─── Bookings ──────────────────────────────────────────────────

@app.post("/api/bookings")
def create_booking(
    req: BookingCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == req.listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    if listing.status != "published":
        raise HTTPException(400, "This listing is not available for booking")
    if listing.owner_id == user.id:
        raise HTTPException(400, "You cannot book your own listing")
    if req.guests > listing.max_guests:
        raise HTTPException(400, f"Maximum guests is {listing.max_guests}")

    validate_booking_dates(req.check_in, req.check_out)

    # Libérer les anciennes réservations "pending" abandonnées de CET utilisateur
    # sur cette annonce (ex. paiement Stripe échoué ou fenêtre fermée), sinon
    # elles bloquent les dates et l'utilisateur se bloque lui-même.
    abandoned = db.query(Booking).filter(
        Booking.user_id == user.id,
        Booking.listing_id == req.listing_id,
        Booking.status == "pending",
    ).all()
    for old in abandoned:
        old.status = "cancelled"
    if abandoned:
        db.commit()
        logger.info(f"Released {len(abandoned)} abandoned pending booking(s) for user {user.id}")

    if check_date_overlap(db, req.listing_id, req.check_in, req.check_out):
        raise HTTPException(409, "These dates are no longer available")

    pricing = calculate_booking_price(listing, req.check_in, req.check_out)

    booking = Booking(
        user_id=user.id,
        listing_id=req.listing_id,
        check_in=req.check_in,
        check_out=req.check_out,
        guests=req.guests,
        nights=pricing["nights"],
        price_per_night=Decimal(str(pricing["price_per_night"])),
        cleaning_fee=Decimal(str(pricing["cleaning_fee"])),
        service_fee=Decimal(str(pricing["service_fee"])),
        subtotal=Decimal(str(pricing["subtotal"])),
        total=Decimal(str(pricing["total"])),
        status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    logger.info(f"Booking created: {booking.id} for listing {req.listing_id}")
    return serialize_booking(booking, listing)


@app.get("/api/bookings")
def list_bookings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bookings = db.query(Booking).filter(Booking.user_id == user.id).order_by(
        Booking.created_at.desc()
    ).all()
    result = []
    for b in bookings:
        listing = db.query(Listing).options(joinedload(Listing.images)).filter(
            Listing.id == b.listing_id
        ).first()
        result.append(serialize_booking(b, listing))
    return {"bookings": result}


@app.get("/api/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "You don't have access to this booking")
    listing = db.query(Listing).options(joinedload(Listing.images)).filter(
        Listing.id == booking.listing_id
    ).first()
    return serialize_booking(booking, listing)


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "You don't have access to this booking")
    if booking.status not in ("pending", "paid"):
        raise HTTPException(400, "This booking cannot be cancelled")
    booking.status = "cancelled"
    db.commit()
    return {"message": "Booking cancelled", "status": "cancelled"}


# ─── Host Listings ─────────────────────────────────────────────

@app.get("/api/host/listings")
def host_listings(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    listings = db.query(Listing).options(
        joinedload(Listing.images), joinedload(Listing.amenities)
    ).filter(Listing.owner_id == user.id).order_by(Listing.created_at.desc()).all()
    return {"listings": [serialize_listing(l) for l in listings]}


@app.post("/api/host/listings")
def create_listing(
    req: ListingCreate,
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    listing = Listing(
        owner_id=user.id,
        title=req.title.strip(),
        description=req.description,
        country=req.country.strip(),
        city=req.city.strip(),
        address=req.address.strip(),
        latitude=req.latitude,
        longitude=req.longitude,
        price_per_night=Decimal(str(req.price_per_night)),
        cleaning_fee=Decimal(str(req.cleaning_fee)),
        service_fee=Decimal(str(req.service_fee)),
        max_guests=req.max_guests,
        bedrooms=req.bedrooms,
        beds=req.beds,
        bathrooms=req.bathrooms,
        property_type=req.property_type,
        status="draft",
    )
    db.add(listing)
    db.flush()

    for i, url in enumerate(req.image_urls):
        db.add(ListingImage(listing_id=listing.id, image_url=url, position=i))

    if req.amenity_ids:
        for aid in req.amenity_ids:
            db.add(ListingAmenity(listing_id=listing.id, amenity_id=aid))

    db.commit()
    db.refresh(listing)
    return serialize_listing(listing)


@app.put("/api/host/listings/{listing_id}")
def update_listing(
    listing_id: int,
    req: ListingUpdate,
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    if listing.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "You don't own this listing")

    update_data = req.model_dump(exclude_unset=True)
    amenity_ids = update_data.pop("amenity_ids", None)
    image_urls = update_data.pop("image_urls", None)

    for key, val in update_data.items():
        if key in ("price_per_night", "cleaning_fee", "service_fee"):
            val = Decimal(str(val))
        setattr(listing, key, val)

    if image_urls is not None:
        db.query(ListingImage).filter(ListingImage.listing_id == listing_id).delete()
        for i, url in enumerate(image_urls):
            db.add(ListingImage(listing_id=listing_id, image_url=url, position=i))

    if amenity_ids is not None:
        db.query(ListingAmenity).filter(
            ListingAmenity.listing_id == listing_id
        ).delete()
        for aid in amenity_ids:
            db.add(ListingAmenity(listing_id=listing_id, amenity_id=aid))

    db.commit()
    db.refresh(listing)
    return serialize_listing(listing)


@app.delete("/api/host/listings/{listing_id}")
def delete_listing(
    listing_id: int,
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    if listing.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "You don't own this listing")
    db.delete(listing)
    db.commit()
    return {"message": "Listing deleted"}


# ─── Host Bookings (reservations received) ─────────────────────

@app.get("/api/host/bookings")
def host_bookings(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    listing_ids = [l.id for l in db.query(Listing).filter(Listing.owner_id == user.id).all()]
    if not listing_ids:
        return {"bookings": []}
    bookings = db.query(Booking).filter(Booking.listing_id.in_(listing_ids)).order_by(
        Booking.created_at.desc()
    ).all()
    result = []
    for b in bookings:
        listing = db.query(Listing).options(joinedload(Listing.images)).filter(
            Listing.id == b.listing_id
        ).first()
        data = serialize_booking(b, listing)
        guest = db.query(User).filter(User.id == b.user_id).first()
        data["guest"] = {
            "first_name": guest.first_name if guest else "",
            "last_name": guest.last_name if guest else "",
            "email": guest.email if guest else "",
        } if guest else None
        result.append(data)
    return {"bookings": result}


# ─── Reviews ───────────────────────────────────────────────────

@app.post("/api/reviews")
def create_review(
    req: ReviewCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(Booking).filter(Booking.id == req.booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.user_id != user.id:
        raise HTTPException(403, "You can only review your own bookings")
    if booking.status != "paid":
        raise HTTPException(400, "You can only review completed stays")
    existing = db.query(Review).filter(Review.booking_id == req.booking_id).first()
    if existing:
        raise HTTPException(409, "You have already reviewed this booking")
    review = Review(
        user_id=user.id,
        listing_id=booking.listing_id,
        booking_id=req.booking_id,
        rating=req.rating,
        comment=req.comment,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return serialize_review(review)


@app.get("/api/listings/{listing_id}/reviews")
def listing_reviews(listing_id: int, db: Session = Depends(get_db)):
    reviews = db.query(Review).filter(Review.listing_id == listing_id).order_by(
        Review.created_at.desc()
    ).all()
    return {"reviews": [serialize_review(r) for r in reviews]}


# ─── Stripe Checkout (paiement sur le compte Stripe principal) ─

@app.post("/api/stripe/checkout")
def create_checkout_session(
    req: CheckoutCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not stripe.api_key or stripe.api_key.startswith("REMPLACEZ") or stripe.api_key == "sk_test_xxx":
        raise HTTPException(500, "Stripe n'est pas configuré : STRIPE_SECRET_KEY introuvable dans le .env")

    booking = db.query(Booking).filter(Booking.id == req.booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.user_id != user.id:
        raise HTTPException(403, "You don't own this booking")
    if booking.status == "paid":
        raise HTTPException(400, "This booking is already paid")
    if booking.status in ("cancelled", "refunded", "failed"):
        raise HTTPException(400, "This booking can no longer be paid")

    listing = db.query(Listing).filter(Listing.id == booking.listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")

    total_cents = int(round(Decimal(str(booking.total)) * 100))

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": booking.currency,
                    "product_data": {
                        "name": f"StayHaven: {listing.title}",
                        "description": f"{booking.nights} nights • {booking.check_in} → {booking.check_out}",
                    },
                    "unit_amount": total_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{FRONTEND_URL}/#/booking/{booking.id}?status=success",
            cancel_url=f"{FRONTEND_URL}/#/booking/{booking.id}?status=cancelled",
            metadata={
                "booking_id": str(booking.id),
                "user_id": str(user.id),
                "listing_id": str(listing.id),
            },
            client_reference_id=str(booking.id),
        )
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout failed: {e}")
        # Ne pas laisser la réservation bloquer les dates si Stripe échoue
        if booking.status == "pending":
            booking.status = "cancelled"
            db.commit()
        raise HTTPException(500, f"Stripe checkout failed: {str(e)}")

    booking.stripe_checkout_session_id = session.id
    db.commit()

    return {"url": session.url, "session_id": session.id}


# ─── Stripe Webhook ────────────────────────────────────────────

@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not configured — webhook rejected")
        raise HTTPException(500, "Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(400, "Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    event_type = event["type"]
    data = event["data"]["object"]
    logger.info(f"Stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        session = data
        booking_id = int(session.get("client_reference_id", 0) or
                         session.get("metadata", {}).get("booking_id", 0))
        if booking_id:
            booking = db.query(Booking).filter(Booking.id == booking_id).first()
            if booking and booking.status != "paid":
                booking.status = "paid"
                booking.stripe_payment_intent_id = session.get("payment_intent")
                db.commit()
                logger.info(f"Booking {booking_id} marked as paid")

    elif event_type == "checkout.session.expired":
        session = data
        booking_id = int(session.get("client_reference_id", 0) or
                         session.get("metadata", {}).get("booking_id", 0))
        if booking_id:
            booking = db.query(Booking).filter(Booking.id == booking_id).first()
            if booking and booking.status == "pending":
                booking.status = "cancelled"
                db.commit()
                logger.info(f"Booking {booking_id} cancelled (session expired)")

    return {"received": True}


# ─── Admin ─────────────────────────────────────────────────────

@app.get("/api/admin/users")
def admin_users(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {"users": [serialize_user(u) for u in users]}


@app.put("/api/admin/users/{user_id}/role")
def admin_update_role(
    user_id: int,
    role: str = Query(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if role not in ("user", "host", "admin"):
        raise HTTPException(422, "Invalid role")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    target.role = role
    db.commit()
    return {"user": serialize_user(target)}


@app.get("/api/admin/listings")
def admin_listings(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listings = db.query(Listing).options(
        joinedload(Listing.images), joinedload(Listing.amenities)
    ).order_by(Listing.created_at.desc()).all()
    return {"listings": [serialize_listing(l) for l in listings]}


@app.get("/api/admin/bookings")
def admin_bookings(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    bookings = db.query(Booking).order_by(Booking.created_at.desc()).all()
    result = []
    for b in bookings:
        listing = db.query(Listing).filter(Listing.id == b.listing_id).first()
        result.append(serialize_booking(b, listing))
    return {"bookings": result}


@app.delete("/api/admin/listings/{listing_id}")
def admin_delete_listing(
    listing_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(404, "Listing not found")
    db.delete(listing)
    db.commit()
    return {"message": "Listing deleted"}


# ─── Health ────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "StayHaven API"}


# ─── Startup: seed password hashes ─────────────────────────────

@app.on_event("startup")
def fix_seed_passwords():
    """Replace placeholder password hashes with real bcrypt hashes."""
    try:
        db = SessionLocal()
        users = db.query(User).filter(User.password_hash.like("placeholder%")).all()
        if users:
            real_hash = hash_password("host1234")
            admin_hash = hash_password("admin1234")
            for u in users:
                u.password_hash = admin_hash if u.role == "admin" else real_hash
            db.commit()
            logger.info(f"Fixed {len(users)} seed password hashes")
        db.close()
    except Exception as e:
        logger.warning(f"Could not fix seed passwords (DB may not be up): {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=not os.getenv("RAILWAY_ENVIRONMENT"))