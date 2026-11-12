"""StayHaven Marketplace — FastAPI application."""
from __future__ import annotations

# ── Charger le .env AVANT tout le reste (db.py et core.py lisent os.getenv à l'import)
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

import os
import json
import asyncio
import logging
from datetime import date, datetime
from typing import Optional, List, Dict, Set
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_, asc, desc, func, inspect, text
from sqlalchemy.exc import IntegrityError

from db import (
    get_db, SessionLocal, User, Listing, ListingImage, Amenity, ListingAmenity,
    Favorite, Booking, Review, PaymentTransaction, StripeEvent, Base, engine,
    Conversation, Message, Notification,
)
from core import (
    hash_password, verify_password, create_access_token, decode_token,
    get_current_user, get_current_user_optional, require_host, require_admin,
    validate_email, validate_password,
    check_date_overlap, calculate_booking_price, validate_booking_dates,
    serialize_user, serialize_listing, serialize_booking, serialize_review,
    serialize_payment,
    serialize_message, serialize_conversation, serialize_notification,
    compute_split, host_stripe_ready, apply_stripe_account, sync_stripe_account,
    ensure_can_publish,
    get_or_create_conversation, user_in_conversation, create_notification,
    FRONTEND_URL, PLATFORM_FEE_PERCENT, SUPPORTED_LANGUAGES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stayhaven")

# Clé Stripe de la plateforme (compte principal Connect). En mode TEST : sk_test_...
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# Optionnel : secret du 2e endpoint webhook ("événements des comptes connectés")
STRIPE_CONNECT_WEBHOOK_SECRET = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET", "")
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "https://book44me.vercel.app").split(",")
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

def _stripe_configured() -> bool:
    key = stripe.api_key
    return bool(key) and not key.startswith("REMPLACEZ") and key != "sk_test_xxx"

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

class LanguageRequest(BaseModel):
    language: str

class SendMessageRequest(BaseModel):
    content: str

class HostProfileUpdate(BaseModel):
    host_bio: Optional[str] = None

class MarkNotificationsRead(BaseModel):
    notification_ids: Optional[List[int]] = None

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

@app.put("/api/auth/language")
def set_language(
    req: LanguageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lang = (req.language or "").strip().lower()[:2]
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    user.language = lang
    db.commit()
    db.refresh(user)
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

# ─── Stripe Connect (Host) ─────────────────────────────────────

@app.post("/api/stripe/connect/onboard")
def stripe_connect_onboard(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    """Crée (si besoin) le compte Express du Host et renvoie le lien d'onboarding Stripe."""
    if not _stripe_configured():
        raise HTTPException(500, "Stripe n'est pas configuré : STRIPE_SECRET_KEY introuvable dans le .env")
    try:
        if not user.stripe_account_id:
            account = stripe.Account.create(
                type="express",
                email=user.email,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                metadata={"user_id": str(user.id)},
                idempotency_key=f"connect-account-user-{user.id}-v2",
            )
            user.stripe_account_id = account.id
            db.commit()
        link = stripe.AccountLink.create(
            account=user.stripe_account_id,
            refresh_url=f"{FRONTEND_URL}/#/host?stripe=refresh",
            return_url=f"{FRONTEND_URL}/#/host?stripe=return",
            type="account_onboarding",
        )
    except stripe.error.StripeError as e:
        logger.error(f"Stripe Connect onboarding failed for user {user.id}: {e}")
        raise HTTPException(502, f"Stripe error: {getattr(e, 'user_message', None) or str(e)}")
    return {"url": link.url}

@app.get("/api/stripe/connect/status")
def stripe_connect_status(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    """Statut RÉEL du compte, relu chez Stripe à chaque appel."""
    due: List[str] = []
    error = None
    if user.stripe_account_id and _stripe_configured():
        try:
            account = sync_stripe_account(user, db)
            req = (account.get("requirements") or {}) if account else {}
            due = list(dict.fromkeys(
                list(req.get("currently_due") or []) + list(req.get("past_due") or [])
            ))
        except stripe.error.StripeError as e:
            logger.error(f"Stripe status check failed for user {user.id}: {e}")
            error = "Could not verify your Stripe account. Please try again."
    return {
        "has_account": bool(user.stripe_account_id),
        "ready": host_stripe_ready(user) and error is None,
        "onboarding_completed": bool(user.stripe_onboarding_completed),
        "charges_enabled": bool(user.stripe_charges_enabled),
        "payouts_enabled": bool(user.stripe_payouts_enabled),
        "requirements_due": due,
        "commission_percent": float(PLATFORM_FEE_PERCENT),
        "error": error,
    }

@app.post("/api/stripe/connect/dashboard")
def stripe_connect_dashboard(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    """Génère un lien sécurisé vers le Stripe Express Dashboard du vendeur.

    Réutilise le compte Stripe Connect existant (acct_...).
    Vérifie côté backend que le compte appartient bien à l'utilisateur connecté.
    Ne crée jamais de nouveau compte. N'expose jamais la clé secrète.
    """
    if not _stripe_configured():
        raise HTTPException(500, "Stripe n'est pas configuré : STRIPE_SECRET_KEY introuvable dans le .env")
    if not user.stripe_account_id:
        raise HTTPException(400, "You do not have a Stripe Connect account yet. Please complete onboarding first.")

    # Vérification : le compte Stripe doit correspondre à l'utilisateur
    try:
        account = stripe.Account.retrieve(user.stripe_account_id)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe dashboard: account retrieval failed for user {user.id}: {e}")
        raise HTTPException(502, "Could not retrieve your Stripe account. Please try again.")

    # Vérifier que les metadata pointent vers cet utilisateur
    meta_user_id = (account.get("metadata") or {}).get("user_id")
    if meta_user_id and str(meta_user_id) != str(user.id):
        logger.error(f"Stripe dashboard: account mismatch for user {user.id}")
        raise HTTPException(403, "This Stripe account does not belong to you.")

    # Générer le login link vers le Express Dashboard
    try:
        login_link = stripe.Account.create_login_link(user.stripe_account_id)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe dashboard: create_login_link failed for user {user.id}: {e}")
        raise HTTPException(502, f"Stripe error: {getattr(e, 'user_message', None) or str(e)}")

    return {"url": login_link.url}

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

# ─── Stripe / paiement : helpers internes ──────────────────────

def _money(cents) -> Decimal:
    return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))

def _expire_checkout_session(booking: Booking) -> None:
    """Ferme (best effort) la session Checkout ouverte d'une réservation abandonnée/annulée."""
    if not booking.stripe_checkout_session_id or not _stripe_configured():
        return
    try:
        stripe.checkout.Session.expire(booking.stripe_checkout_session_id)
    except stripe.error.StripeError:
        pass  # déjà expirée / terminée : sans importance

def _booking_host_id(db: Session, booking: Booking) -> Optional[int]:
    listing = db.query(Listing).filter(Listing.id == booking.listing_id).first()
    return listing.owner_id if listing else None

def _add_ledger(db: Session, booking: Optional[Booking], tx_type: str, amount: Decimal,
                platform_fee: Decimal, host_amount: Decimal, *,
                payment_intent: Optional[str] = None, charge_id: Optional[str] = None,
                transfer_id: Optional[str] = None, refund_id: Optional[str] = None,
                note: Optional[str] = None) -> None:
    db.add(PaymentTransaction(
        booking_id=booking.id if booking else None,
        host_id=_booking_host_id(db, booking) if booking else None,
        type=tx_type,
        amount=amount,
        platform_fee=platform_fee,
        host_amount=host_amount,
        currency=booking.currency if booking else "usd",
        stripe_payment_intent_id=payment_intent,
        stripe_charge_id=charge_id,
        stripe_transfer_id=transfer_id,
        stripe_refund_id=refund_id,
        stripe_connected_account_id=booking.stripe_connected_account_id if booking else None,
        note=(note or "")[:255] or None,
    ))

def _issue_refund(db: Session, booking: Booking, pi_id: str, note: str) -> None:
    """Rembourse intégralement le paiement (+ reprise du transfert Host et de la commission).

    Ne fait PAS de commit : c'est à l'appelant de valider la transaction.
    Le même remboursement ne peut pas être créé deux fois (clé d'idempotence).
    """
    connect_payment = Decimal(str(booking.platform_fee or 0)) > 0
    params = dict(
        payment_intent=pi_id,
        metadata={"booking_id": str(booking.id)},
        idempotency_key=f"refund-booking-{booking.id}-{pi_id}",
    )
    if connect_payment:
        params["reverse_transfer"] = True
        params["refund_application_fee"] = True
    refund = stripe.Refund.create(**params)

    total_cents, fee_cents, host_cents = compute_split(booking.total)
    if connect_payment:
        fee, host = _money(fee_cents), _money(host_cents)
    else:
        fee, host = Decimal("0.00"), Decimal("0.00")
    if not db.query(PaymentTransaction).filter(
        PaymentTransaction.stripe_refund_id == refund.id
    ).first():
        _add_ledger(db, booking, "refund", _money(total_cents), fee, host,
                    payment_intent=pi_id, refund_id=refund.id, note=note)
    booking.status = "refunded"

# ─── Message de réservation automatique (Guest → Host) + temps réel ───

def _format_money(amount, currency: Optional[str]) -> str:
    """Montant lisible : $123.45 pour l'USD, sinon 123.45 EUR, etc."""
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    cur = (currency or "usd").lower()
    return f"${value}" if cur == "usd" else f"{value} {cur.upper()}"

def _build_booking_message(booking: Booking, listing: Listing, guest: Optional[User]) -> str:
    """Texte envoyé automatiquement au Host dans la messagerie quand une réservation est payée."""
    cur = booking.currency
    guest_name = f"{guest.first_name} {guest.last_name}".strip() if guest else "Guest"
    guest_email = (guest.email if guest else None) or "Not provided"
    guest_phone = (guest.phone if guest else None) or "Not provided"
    location = ", ".join(p for p in (listing.address, listing.city, listing.country) if p)
    guests_label = "guest" if booking.guests == 1 else "guests"
    nights_label = "night" if booking.nights == 1 else "nights"

    lines = [
        f"New booking confirmed — Booking #{booking.id}",
        "",
        f"Listing: {listing.title}",
        f"Address: {location}",
        f"Check-in: {booking.check_in.isoformat()}",
        f"Check-out: {booking.check_out.isoformat()}",
        f"Nights: {booking.nights}",
        f"Guests: {booking.guests}",
        "",
        "Price details",
        f"{_format_money(booking.price_per_night, cur)} × {booking.nights} {nights_label} × "
        f"{booking.guests} {guests_label}: {_format_money(booking.subtotal, cur)}",
        f"Cleaning fee: {_format_money(booking.cleaning_fee, cur)}",
        f"Service fee: {_format_money(booking.service_fee, cur)}",
        f"Total paid: {_format_money(booking.total, cur)}",
        f"Platform commission: {_format_money(booking.platform_fee, cur)}",
        f"Your payout: {_format_money(booking.host_amount, cur)}",
        "",
        "Guest information",
        f"Name: {guest_name}",
        f"Email: {guest_email}",
        f"Phone: {guest_phone}",
        "",
        "Payment status: Paid",
    ]
    return "\n".join(lines)

def _queue_post_commit(db: Session, kind: str, user_id: int, obj) -> None:
    """Met en attente un événement temps réel, à envoyer APRÈS le commit de la transaction."""
    db.info.setdefault("post_commit", []).append((kind, user_id, obj))

def _flush_post_commit(db: Session) -> None:
    """Envoie en temps réel (WebSocket) les événements en attente. À appeler après le commit."""
    items = db.info.pop("post_commit", [])
    for kind, user_id, obj in items:
        try:
            if kind == "message":
                data = {
                    "type": "new_message",
                    "conversation_id": obj.conversation_id,
                    "message": serialize_message(obj),
                }
            else:
                data = {
                    "type": "notification",
                    "notification": serialize_notification(obj),
                }
            _broadcast_to_user(user_id, data)
        except Exception as e:
            logger.warning(f"Realtime broadcast failed ({kind} → user {user_id}): {e}")

def _record_payment(db: Session, booking: Booking, pi_id: Optional[str]) -> None:
    """Marque la réservation payée et écrit commission + versement. Idempotent. Sans commit."""
    if booking.status == "paid":
        return
    total_cents, fee_cents, host_cents = compute_split(booking.total)

    charge_id = transfer_id = None
    if pi_id:
        try:
            pi = stripe.PaymentIntent.retrieve(pi_id, expand=["latest_charge"])
            charge = pi.get("latest_charge")
            if charge:
                charge_id = charge.get("id")
                transfer_id = charge.get("transfer")
        except stripe.error.StripeError as e:
            logger.warning(f"Could not read charge for {pi_id}: {e}")

    already = db.query(PaymentTransaction).filter(
        PaymentTransaction.type == "payment",
        PaymentTransaction.stripe_payment_intent_id == pi_id,
    ).first() if pi_id else None

    if booking.status != "pending":
        # Paiement reçu pour une réservation annulée entre-temps : on rembourse automatiquement.
        booking.platform_fee = _money(fee_cents)
        booking.host_amount = _money(host_cents)
        booking.stripe_payment_intent_id = pi_id
        if not already:
            _add_ledger(db, booking, "payment", _money(total_cents), _money(fee_cents),
                        _money(host_cents), payment_intent=pi_id, charge_id=charge_id,
                        transfer_id=transfer_id, note="Payment received on inactive booking")
        _issue_refund(db, booking, pi_id, "Automatic refund: booking no longer active")
        logger.warning(f"Booking {booking.id} was {booking.status} when paid: auto-refunded")
        return

    booking.status = "paid"
    booking.stripe_payment_intent_id = pi_id
    booking.platform_fee = _money(fee_cents)
    booking.host_amount = _money(host_cents)
    if not already:
        _add_ledger(db, booking, "payment", _money(total_cents), _money(fee_cents),
                    _money(host_cents), payment_intent=pi_id, charge_id=charge_id,
                    transfer_id=transfer_id, note="Booking payment")
    logger.info(f"Booking {booking.id} marked as paid "
                f"(fee {booking.platform_fee}, host {booking.host_amount})")

    # ─── Chat auto : créer la conversation Guest/Host après confirmation du paiement ───
    listing = db.query(Listing).filter(Listing.id == booking.listing_id).first()
    if listing and listing.owner_id != booking.user_id:
        try:
            conv = get_or_create_conversation(db, booking.user_id, listing.owner_id)
            # Notification au Host : nouveau voyageur confirmé
            guest = db.query(User).filter(User.id == booking.user_id).first()
            guest_name = f"{guest.first_name} {guest.last_name}" if guest else "A guest"
            host_notif = create_notification(
                db, listing.owner_id, "booking",
                f"New booking confirmed — {listing.title}",
                f"{guest_name} booked your place. You can now chat with them.",
                f"/#/messages/{conv.id}",
                dedup_key=f"booking-paid-host-{booking.id}",
            )
            # Notification au Guest : réservation confirmée
            guest_notif = create_notification(
                db, booking.user_id, "booking",
                f"Booking confirmed — {listing.title}",
                f"Your booking is confirmed. You can now message your host.",
                f"/#/messages/{conv.id}",
                dedup_key=f"booking-paid-guest-{booking.id}",
            )

            # Message automatique envoyé par le Guest au Host avec toutes les infos de la
            # réservation. Créé une seule fois par réservation (même clé anti-doublon que
            # la notification Host).
            if host_notif is not None:
                booking_msg = Message(
                    conversation_id=conv.id,
                    sender_id=booking.user_id,
                    content=_build_booking_message(booking, listing, guest),
                )
                db.add(booking_msg)
                conv.updated_at = datetime.utcnow()
                db.flush()
                # Temps réel : envoyé après le commit de l'appelant
                _queue_post_commit(db, "message", listing.owner_id, booking_msg)
                _queue_post_commit(db, "notification", listing.owner_id, host_notif)
            if guest_notif is not None:
                _queue_post_commit(db, "notification", booking.user_id, guest_notif)
        except Exception as e:
            logger.warning(f"Auto-conversation creation failed for booking {booking.id}: {e}")

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
    if req.guests < 1:
        raise HTTPException(400, "At least 1 guest is required")
    if req.guests > listing.max_guests:
        raise HTTPException(400, f"Maximum guests is {listing.max_guests}")

    owner = db.query(User).filter(User.id == listing.owner_id).first()
    if not host_stripe_ready(owner):
        raise HTTPException(400, "This host cannot accept payments yet.")

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
        for old in abandoned:
            _expire_checkout_session(old)
        logger.info(f"Released {len(abandoned)} abandoned pending booking(s) for user {user.id}")

    if check_date_overlap(db, req.listing_id, req.check_in, req.check_out):
        raise HTTPException(409, "These dates are no longer available")

    pricing = calculate_booking_price(listing, req.check_in, req.check_out, req.guests)

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
        stripe_connected_account_id=owner.stripe_account_id,
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

def _reconcile_pending_booking(db: Session, booking_id: int) -> None:
    """Filet de sécurité si le webhook n'arrive pas : on interroge Stripe directement.
    La réservation n'est marquée payée que si Stripe confirme payment_status == 'paid'."""
    try:
        booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().first()
        if not booking or booking.status != "pending" or not booking.stripe_checkout_session_id:
            db.rollback()
            return
        session = stripe.checkout.Session.retrieve(booking.stripe_checkout_session_id)
        if session.get("payment_status") != "paid":
            db.rollback()
            return
        logger.info(f"Reconcile: Stripe confirms session {session['id']} paid for booking "
                    f"{booking.id} (webhook not received yet)")
        _record_payment(db, booking, session.get("payment_intent"))
        db.commit()
        _flush_post_commit(db)
    except Exception as e:
        db.rollback()
        db.info.pop("post_commit", None)
        logger.warning(f"Reconcile failed for booking {booking_id}: {e}")

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
    if booking.status == "pending" and booking.stripe_checkout_session_id:
        _reconcile_pending_booking(db, booking.id)
        db.refresh(booking)
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
    # Verrou de ligne : évite qu'un webhook et une annulation se marchent dessus
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "You don't have access to this booking")
    if booking.status not in ("pending", "paid"):
        raise HTTPException(400, "This booking cannot be cancelled")

    if booking.status == "paid":
        if not booking.stripe_payment_intent_id:
            booking.status = "cancelled"
            db.commit()
            return {"message": "Booking cancelled", "status": "cancelled"}
        try:
            _issue_refund(db, booking, booking.stripe_payment_intent_id,
                          "Refund: booking cancelled")
        except stripe.error.StripeError as e:
            db.rollback()
            if getattr(e, "code", None) == "charge_already_refunded":
                booking = db.query(Booking).filter(Booking.id == booking_id).first()
                booking.status = "refunded"
                db.commit()
                return {"message": "Booking cancelled and refunded", "status": "refunded"}
            logger.error(f"Refund failed for booking {booking_id}: {e}")
            raise HTTPException(502, f"Refund failed: {getattr(e, 'user_message', None) or str(e)}")
        db.commit()
        return {"message": "Booking cancelled and refunded", "status": "refunded"}

    booking.status = "cancelled"
    db.commit()
    _expire_checkout_session(booking)
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

    # Publication : uniquement si le propriétaire a un compte Stripe Connect valide,
    # vérifié en direct auprès de Stripe (jamais sur la seule foi du frontend).
    new_status = update_data.get("status")
    if new_status is not None:
        if new_status not in ("draft", "published", "unpublished"):
            raise HTTPException(422, "Invalid status")
        if new_status == "published" and listing.status != "published":
            owner = db.query(User).filter(User.id == listing.owner_id).first()
            ensure_can_publish(owner, db)

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

@app.get("/api/host/payments")
def host_payments(
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    """Historique des paiements / commissions / remboursements du Host."""
    rows = db.query(PaymentTransaction).filter(
        PaymentTransaction.host_id == user.id
    ).order_by(PaymentTransaction.created_at.desc(), PaymentTransaction.id.desc()).limit(200).all()
    gross = fee = net = Decimal("0")
    for r in rows:
        sign = 1 if r.type == "payment" else -1 if r.type == "refund" else 0
        gross += sign * Decimal(str(r.amount or 0))
        fee += sign * Decimal(str(r.platform_fee or 0))
        net += sign * Decimal(str(r.host_amount or 0))
    return {
        "payments": [serialize_payment(r) for r in rows],
        "totals": {"gross": float(gross), "platform_fee": float(fee), "net": float(net)},
    }

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

# ─── Stripe Checkout (Connect : 90 % Host / 10 % plateforme) ───

@app.post("/api/stripe/checkout")
def create_checkout_session(
    req: CheckoutCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _stripe_configured():
        raise HTTPException(500, "Stripe n'est pas configuré : STRIPE_SECRET_KEY introuvable dans le .env")

    # Verrou de ligne : deux clics / deux onglets ne peuvent pas créer deux sessions
    booking = db.query(Booking).filter(Booking.id == req.booking_id).with_for_update().first()
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
    owner = db.query(User).filter(User.id == listing.owner_id).first()

    # Le compte du Host est revérifié EN DIRECT chez Stripe avant d'encaisser
    if not owner or not owner.stripe_account_id:
        raise HTTPException(400, "This host cannot accept payments yet.")
    try:
        sync_stripe_account(owner, db)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe account verification failed (checkout): {e}")
        raise HTTPException(502, "Could not verify your Stripe account. Please try again.")
    if not host_stripe_ready(owner):
        raise HTTPException(400, "This host cannot accept payments yet.")

    # Anti double paiement : on réutilise la session ouverte, ou on constate le paiement déjà fait
    if booking.stripe_checkout_session_id:
        try:
            existing = stripe.checkout.Session.retrieve(booking.stripe_checkout_session_id)
        except stripe.error.StripeError:
            existing = None
        if existing:
            if existing.get("payment_status") == "paid":
                _record_payment(db, booking, existing.get("payment_intent"))
                db.commit()
                _flush_post_commit(db)
                raise HTTPException(400, "This booking is already paid")
            if existing.get("status") == "open" and existing.get("url"):
                return {"url": existing["url"], "session_id": existing["id"]}

    total_cents, fee_cents, host_cents = compute_split(booking.total)
    meta = {
        "booking_id": str(booking.id),
        "user_id": str(user.id),
        "listing_id": str(listing.id),
        "host_id": str(owner.id),
        "platform_fee_cents": str(fee_cents),
        "host_amount_cents": str(host_cents),
    }

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": booking.currency,
                    "product_data": {
                        "name": f"StayHaven: {listing.title}",
                        "description": f"{booking.nights} nights • {booking.guests} guests • "
                                       f"{booking.check_in} → {booking.check_out}",
                    },
                    "unit_amount": total_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            # Stripe Connect (destination charge) : commission prélevée automatiquement,
            # le reste est versé au compte du Host de l'annonce.
            payment_intent_data={
                "application_fee_amount": fee_cents,
                "transfer_data": {"destination": owner.stripe_account_id},
                "transfer_group": f"booking_{booking.id}",
                "metadata": meta,
            },
            success_url=f"{FRONTEND_URL}/#/booking/{booking.id}?status=success",
            cancel_url=f"{FRONTEND_URL}/#/booking/{booking.id}?status=cancelled",
            metadata=meta,
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
    booking.stripe_connected_account_id = owner.stripe_account_id
    db.commit()

    return {"url": session.url, "session_id": session.id}

# ─── Stripe Webhook ────────────────────────────────────────────

def _booking_from_object(db: Session, obj) -> Optional[Booking]:
    raw = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("booking_id")
    try:
        booking_id = int(raw)
    except (TypeError, ValueError):
        return None
    return db.query(Booking).filter(Booking.id == booking_id).with_for_update().first()

def _handle_stripe_event(db: Session, event) -> None:
    """Traite un événement. Aucun commit ici : l'appelant valide tout d'un bloc."""
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") != "paid":
            logger.info(f"{event_type} {obj.get('id')}: payment_status="
                        f"{obj.get('payment_status')} — not marked paid")
            return
        booking = _booking_from_object(db, obj)
        if not booking:
            logger.error(f"{event_type} {obj.get('id')}: no booking found "
                         f"(client_reference_id={obj.get('client_reference_id')}, "
                         f"metadata={dict(obj.get('metadata') or {})})")
            return
        logger.info(f"{event_type}: booking {booking.id} status={booking.status} → _record_payment")
        _record_payment(db, booking, obj.get("payment_intent"))

    elif event_type == "checkout.session.expired":
        booking = _booking_from_object(db, obj)
        if booking and booking.status == "pending":
            booking.status = "cancelled"
            logger.info(f"Booking {booking.id} cancelled (session expired)")

    elif event_type == "payment_intent.payment_failed":
        booking = _booking_from_object(db, obj)
        if booking:
            err = (obj.get("last_payment_error") or {}).get("message") or "Payment failed"
            total_cents, fee_cents, host_cents = compute_split(booking.total)
            # La réservation reste "pending" : le client peut réessayer dans Checkout.
            _add_ledger(db, booking, "failed", _money(total_cents), Decimal("0.00"),
                        Decimal("0.00"), payment_intent=obj.get("id"), note=err)
            logger.info(f"Payment failed for booking {booking.id}: {err}")

    elif event_type == "charge.refunded":
        pi_id = obj.get("payment_intent")
        booking = db.query(Booking).filter(
            Booking.stripe_payment_intent_id == pi_id
        ).with_for_update().first() if pi_id else None
        if not booking:
            logger.warning(f"charge.refunded: no booking for payment_intent {pi_id}")
        if booking:
            refunds = stripe.Refund.list(payment_intent=pi_id, limit=100)
            booking_total = Decimal(str(booking.total)) or Decimal("1")
            for r in refunds.get("data", []):
                if r.get("status") not in ("succeeded", "pending"):
                    continue
                rid = r["id"]
                if db.query(PaymentTransaction).filter(
                    PaymentTransaction.stripe_refund_id == rid
                ).first():
                    continue
                amount = _money(r["amount"])
                fee = (Decimal(str(booking.platform_fee or 0)) * amount / booking_total
                       ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                _add_ledger(db, booking, "refund", amount, fee, amount - fee,
                            payment_intent=pi_id, refund_id=rid, note="Refund")
            if obj.get("refunded"):
                booking.status = "refunded"

    elif event_type == "account.updated":
        acct_id = obj.get("id")
        host = db.query(User).filter(User.stripe_account_id == acct_id).first() if acct_id else None
        if host:
            apply_stripe_account(host, obj)

@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()  # corps brut : indispensable pour la signature
    sig_header = request.headers.get("stripe-signature", "")
    logger.info(f"Stripe webhook received: {len(payload)} bytes, "
                f"signature header {'present' if sig_header else 'MISSING'}")

    secrets = [(n, s) for n, s in (("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET),
                                   ("STRIPE_CONNECT_WEBHOOK_SECRET", STRIPE_CONNECT_WEBHOOK_SECRET)) if s]
    if not secrets:
        logger.error("Stripe webhook rejected: no webhook secret configured "
                     "(STRIPE_WEBHOOK_SECRET / STRIPE_CONNECT_WEBHOOK_SECRET empty)")
        raise HTTPException(500, "Webhook secret not configured")

    event = None
    used = None
    for name, secret in secrets:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
            used = name
            break
        except ValueError:
            logger.error("Stripe webhook rejected: payload is not valid JSON")
            raise HTTPException(400, "Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Stripe webhook: signature does not match {name} ({e})")
            continue
    if event is None:
        logger.error("Stripe webhook rejected: signature matches none of the configured secrets "
                     f"({', '.join(n for n, _ in secrets)}). Check the whsec_ of the endpoint "
                     "(test vs live mode, correct endpoint).")
        raise HTTPException(400, "Invalid signature")

    logger.info(f"Stripe webhook verified with {used}: id={event['id']} type={event['type']} "
                f"livemode={event.get('livemode')} account={event.get('account') or 'platform'}")

    # Anti-doublons : un événement n'est traité qu'une seule fois
    if db.query(StripeEvent).filter(StripeEvent.id == event["id"]).first():
        logger.info(f"Stripe event {event['id']} already processed — skipped")
        return {"received": True, "duplicate": True}

    try:
        _handle_stripe_event(db, event)
        db.add(StripeEvent(id=event["id"], type=event["type"]))
        db.commit()
    except IntegrityError:
        db.rollback()
        db.info.pop("post_commit", None)
        logger.info(f"Stripe event {event['id']} processed concurrently — skipped")
        return {"received": True, "duplicate": True}
    except Exception as e:
        db.rollback()
        db.info.pop("post_commit", None)
        logger.exception(f"Webhook processing failed ({event['type']} {event['id']}): {e}")
        raise HTTPException(500, "Webhook processing failed")  # Stripe réessaiera

    logger.info(f"Stripe event {event['id']} ({event['type']}) processed and committed")
    # Temps réel (message de réservation + notifications) : uniquement après le commit
    _flush_post_commit(db)

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

@app.get("/api/admin/payments")
def admin_payments(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Historique global des commissions et versements de la plateforme."""
    rows = db.query(PaymentTransaction).order_by(
        PaymentTransaction.created_at.desc(), PaymentTransaction.id.desc()
    ).limit(500).all()
    gross = fee = net = Decimal("0")
    for r in rows:
        sign = 1 if r.type == "payment" else -1 if r.type == "refund" else 0
        gross += sign * Decimal(str(r.amount or 0))
        fee += sign * Decimal(str(r.platform_fee or 0))
        net += sign * Decimal(str(r.host_amount or 0))
    return {
        "payments": [serialize_payment(r) for r in rows],
        "totals": {"gross": float(gross), "platform_fee": float(fee), "hosts_net": float(net)},
    }

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

# ─── Public Host Profile ───────────────────────────────────────

@app.get("/api/hosts/{host_id}")
def get_public_host_profile(
    host_id: int,
    db: Session = Depends(get_db),
):
    """Profil public du Host : photo, nom, bio, avis, notes, autres annonces.

    N'expose jamais l'e-mail, le téléphone ou d'autres informations privées.
    """
    host = db.query(User).filter(User.id == host_id).first()
    if not host:
        raise HTTPException(404, "Host not found")

    # Annonces publiées du Host
    listings = db.query(Listing).options(
        joinedload(Listing.images), joinedload(Listing.amenities)
    ).filter(
        Listing.owner_id == host_id,
        Listing.status == "published",
    ).order_by(Listing.created_at.desc()).all()

    # Toutes les reviews sur les annonces du Host
    listing_ids = [l.id for l in listings]
    all_reviews = []
    if listing_ids:
        all_reviews = db.query(Review).filter(
            Review.listing_id.in_(listing_ids)
        ).order_by(Review.created_at.desc()).limit(50).all()

    avg_rating = None
    review_count = len(all_reviews)
    if all_reviews:
        avg_rating = round(sum(r.rating for r in all_reviews) / review_count, 1)

    return {
        "id": host.id,
        "first_name": host.first_name,
        "last_name": host.last_name,
        "avatar_url": host.avatar_url,
        "host_bio": getattr(host, "host_bio", None),
        "created_at": host.created_at.isoformat() if host.created_at else None,
        "avg_rating": avg_rating,
        "review_count": review_count,
        "listings": [serialize_listing(l) for l in listings],
        "reviews": [serialize_review(r) for r in all_reviews[:10]],
    }

@app.put("/api/host/profile")
def update_host_profile(
    req: HostProfileUpdate,
    user: User = Depends(require_host),
    db: Session = Depends(get_db),
):
    """Permet au Host de mettre à jour sa bio publique."""
    if req.host_bio is not None:
        user.host_bio = (req.host_bio or "").strip()[:5000] or None
    db.commit()
    db.refresh(user)
    return serialize_user(user)

# ─── Chat / Conversations ──────────────────────────────────────

@app.get("/api/conversations")
def list_conversations(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Liste toutes les conversations de l'utilisateur connecté."""
    convs = db.query(Conversation).filter(
        or_(Conversation.guest_id == user.id, Conversation.host_id == user.id)
    ).order_by(Conversation.updated_at.desc()).all()
    return {"conversations": [serialize_conversation(c, user.id) for c in convs]}

@app.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Détail d'une conversation + messages. Accès réservé au Guest et au Host concernés."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if not user_in_conversation(user, conv):
        raise HTTPException(403, "You don't have access to this conversation")

    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.created_at.asc()).all()

    # Marquer comme lus les messages reçus de l'autre partie
    changed = False
    for m in messages:
        if m.sender_id != user.id and m.read_at is None:
            m.read_at = datetime.utcnow()
            changed = True
    if changed:
        db.commit()

    return {
        **serialize_conversation(conv, user.id),
        "messages": [serialize_message(m) for m in messages],
    }

@app.post("/api/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: int,
    req: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Envoie un message dans une conversation. Accès réservé aux participants."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if not user_in_conversation(user, conv):
        raise HTTPException(403, "You don't have access to this conversation")

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(422, "Message content is required")
    if len(content) > 5000:
        raise HTTPException(422, "Message too long (max 5000 characters)")

    msg = Message(
        conversation_id=conversation_id,
        sender_id=user.id,
        content=content,
    )
    db.add(msg)
    # flush : msg.id doit exister pour que la clé anti-doublon "msg-<id>" soit unique
    # par message (sinon "msg-None" bloquerait toutes les notifications suivantes)
    db.flush()
    conv.updated_at = datetime.utcnow()

    # Notification au destinataire
    recipient_id = conv.host_id if user.id == conv.guest_id else conv.guest_id
    sender_name = f"{user.first_name} {user.last_name}"
    create_notification(
        db, recipient_id, "message",
        f"New message from {sender_name}",
        content[:200],
        f"/#/messages/{conversation_id}",
        dedup_key=f"msg-{msg.id}",
    )
    db.commit()
    db.refresh(msg)

    # Broadcast via WebSocket
    _broadcast_to_user(recipient_id, {
        "type": "new_message",
        "conversation_id": conversation_id,
        "message": serialize_message(msg),
    })
    _broadcast_to_user(user.id, {
        "type": "new_message",
        "conversation_id": conversation_id,
        "message": serialize_message(msg),
    })
    # Notification event
    _broadcast_to_user(recipient_id, {
        "type": "notification",
        "notification": serialize_notification(
            db.query(Notification).filter(
                Notification.dedup_key == f"msg-{msg.id}"
            ).first()
        ) if db.query(Notification).filter(
            Notification.dedup_key == f"msg-{msg.id}"
        ).first() else None,
    })

    return serialize_message(msg)

# ─── Notifications ─────────────────────────────────────────────

@app.get("/api/notifications")
def list_notifications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    notifs = q.order_by(Notification.created_at.desc()).limit(limit).all()
    unread_count = db.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.read_at.is_(None),
    ).count()
    return {"notifications": [serialize_notification(n) for n in notifs], "unread_count": unread_count}

@app.post("/api/notifications/read")
def mark_notifications_read(
    req: MarkNotificationsRead,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if req.notification_ids:
        notifs = db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.id.in_(req.notification_ids),
        ).all()
        for n in notifs:
            if n.read_at is None:
                n.read_at = datetime.utcnow()
    else:
        # Mark all as read
        notifs = db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        ).all()
        for n in notifs:
            n.read_at = datetime.utcnow()
    db.commit()
    return {"message": "Notifications marked as read"}

@app.get("/api/notifications/unread-count")
def unread_notification_count(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = db.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.read_at.is_(None),
    ).count()
    return {"count": count}

# ─── WebSocket: real-time ──────────────────────────────────────

# Connexions actives : user_id → set de WebSockets
_active_connections: Dict[int, Set[WebSocket]] = {}

# Boucle d'événements principale (mémorisée à la première connexion WebSocket) :
# nécessaire pour envoyer des messages temps réel depuis les endpoints synchrones
# (exécutés dans un thread) et depuis le webhook Stripe.
_main_loop: Optional[asyncio.AbstractEventLoop] = None

def _broadcast_to_user(user_id: int, data: dict) -> None:
    """Envoie un message temps réel à toutes les connexions d'un utilisateur."""
    conns = list(_active_connections.get(user_id, set()))
    for ws in conns:
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not None:
                # Appelé depuis la boucle principale (ex. webhook async)
                asyncio.ensure_future(ws.send_json(data))
            elif _main_loop is not None and _main_loop.is_running():
                # Appelé depuis un thread (endpoint synchrone) : on planifie sur la boucle principale
                asyncio.run_coroutine_threadsafe(ws.send_json(data), _main_loop)
        except Exception as e:
            logger.warning(f"WebSocket send failed for user {user_id}: {e}")

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """WebSocket sécurisé : authentifié par token JWT.

    Envoie en temps réel : nouveaux messages, notifications, changements de statut.
    """
    global _main_loop
    payload = decode_token(token)
    if payload is None:
        await websocket.close(code=4401)
        return
    user_id = int(payload.get("sub", 0))
    if user_id == 0:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _main_loop = asyncio.get_running_loop()
    if user_id not in _active_connections:
        _active_connections[user_id] = set()
    _active_connections[user_id].add(websocket)

    try:
        while True:
            # Keep-alive : on attend les messages du client (ping/pong)
            data = await websocket.receive_text()
            # On peut traiter des pings clients ici si besoin
    except WebSocketDisconnect:
        pass
    finally:
        _active_connections.get(user_id, set()).discard(websocket)
        if not _active_connections.get(user_id):
            _active_connections.pop(user_id, None)

# ─── Health ────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "StayHaven API"}

# ─── Startup: schéma (nouvelles tables/colonnes) ───────────────

@app.on_event("startup")
def ensure_schema():
    """Crée les nouvelles tables et ajoute les nouvelles colonnes si elles manquent.

    Aucun import SQL à refaire : db.sql reste valable tel quel.
    """
    try:
        Base.metadata.create_all(bind=engine)
        insp = inspect(engine)
        needed = {
            "users": [
                ("language", "VARCHAR(5) NOT NULL DEFAULT 'en'"),
                ("host_bio", "TEXT NULL"),
            ],
            "bookings": [
                ("platform_fee", "DECIMAL(10,2) NOT NULL DEFAULT 0"),
                ("host_amount", "DECIMAL(10,2) NOT NULL DEFAULT 0"),
            ],
        }
        with engine.begin() as conn:
            for table, cols in needed.items():
                existing = {c["name"] for c in insp.get_columns(table)}
                for name, ddl in cols:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                        logger.info(f"Added column {table}.{name}")
    except Exception as e:
        logger.warning(f"Could not ensure schema (DB may not be up): {e}")

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
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
