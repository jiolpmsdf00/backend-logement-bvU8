"""Security, auth, and shared business logic for StayHaven."""
from __future__ import annotations

import os
import io
import time
import base64
import hashlib
import hmac
import secrets
import struct
import logging
from urllib.parse import quote
from datetime import datetime, timedelta, date
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN

import stripe
from passlib.context import CryptContext
from jose import jwt, JWTError
from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, select, func

from db import User, Listing, Booking, Favorite, PaymentTransaction, SessionLocal, get_db
from db import Conversation, Message, Notification, Review, BlockedDate, CancellationRequest

logger = logging.getLogger("stayhaven")

pwd_context = CryptContext(
    schemes=["bcrypt_sha256"],
    deprecated="auto"
)

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Commission de la plateforme (en %) prélevée sur chaque réservation
PLATFORM_FEE_PERCENT = Decimal(os.getenv("PLATFORM_FEE_PERCENT", "10"))

SUPPORTED_LANGUAGES = ("en", "fr", "es", "de", "it", "pt")

# Nom affiché dans l'application d'authentification (Google Authenticator, Authy...)
TOTP_ISSUER = os.getenv("TOTP_ISSUER", "Book4Me")

STRIPE_REQUIRED_MESSAGE = (
    "Stripe Connect setup is required to publish a listing. "
    "Please complete it from your host dashboard."
)

# Politique d'annulation commune à tous les logements
CANCEL_FULL_REFUND_HOURS = 7 * 24   # > 7 jours avant l'arrivée : 100 %
CANCEL_PARTIAL_REFUND_HOURS = 24    # entre 24 h et 7 jours : 50 %, en dessous : 0 %
DEFAULT_CHECK_IN_TIME = "15:00"
DEFAULT_CHECK_OUT_TIME = "11:00"

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

# ─── TOTP (Google Authenticator / Authy, RFC 6238) ─────────────

def generate_totp_secret() -> str:
    """Secret aléatoire de 160 bits, encodé en base32 (32 caractères, sans padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")

def _totp_at(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)

def verify_totp(secret: str, code: str, last_step: int = 0, window: int = 1) -> Optional[int]:
    """Vérifie un code à 6 chiffres (tolérance ±window pas de 30 s).

    Retourne le pas de temps accepté, ou None. Un pas déjà utilisé
    (<= last_step) est refusé : un même code ne sert qu'une fois.
    """
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6) or not secret:
        return None
    current = int(time.time() // 30)
    try:
        for step in range(current - window, current + window + 1):
            if step <= int(last_step or 0):
                continue
            if hmac.compare_digest(_totp_at(secret, step), code):
                return step
    except Exception:
        return None
    return None

def totp_provisioning_uri(secret: str, email: str) -> str:
    issuer = quote(TOTP_ISSUER)
    return (
        f"otpauth://totp/{issuer}:{quote(email)}"
        f"?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
    )

def make_qr_data_url(data: str) -> Optional[str]:
    """QR code (SVG, data URL). Retourne None si la librairie 'qrcode' est absente."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        logger.warning("Package 'qrcode' not installed: QR code unavailable (manual key only)")
        return None
    try:
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning(f"QR code generation failed: {e}")
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

# ─── Stripe Connect helpers ────────────────────────────────────

def compute_split(total) -> tuple[int, int, int]:
    """Retourne (total_cents, commission_cents, host_cents).

    La commission est de PLATFORM_FEE_PERCENT % du total (arrondi au centime),
    le Host reçoit le reste : la somme est toujours exacte.
    """
    total_cents = int((Decimal(str(total)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    fee_cents = int(
        (Decimal(total_cents) * PLATFORM_FEE_PERCENT / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return total_cents, fee_cents, total_cents - fee_cents

def host_stripe_ready(user: Optional[User]) -> bool:
    """Vrai si le compte Connect du Host peut encaisser et recevoir des versements."""
    return bool(
        user
        and user.stripe_account_id
        and user.stripe_onboarding_completed
        and user.stripe_charges_enabled
        and user.stripe_payouts_enabled
    )

def apply_stripe_account(user: User, account) -> None:
    """Copie l'état RÉEL du compte Stripe dans la base."""
    caps = account.get("capabilities") or {}
    transfers_active = caps.get("transfers") == "active"
    # charges_enabled n'est considéré vrai que si la capacité de transfert est active
    user.stripe_charges_enabled = bool(account.get("charges_enabled")) and transfers_active
    user.stripe_payouts_enabled = bool(account.get("payouts_enabled"))
    user.stripe_onboarding_completed = bool(account.get("details_submitted"))

def sync_stripe_account(user: User, db: Session):
    """Interroge Stripe côté serveur et met à jour les drapeaux du Host."""
    if not user.stripe_account_id:
        return None
    account = stripe.Account.retrieve(user.stripe_account_id)
    apply_stripe_account(user, account)
    db.commit()
    return account

def ensure_can_publish(owner: Optional[User], db: Session) -> None:
    """Bloque la publication sans compte Stripe Connect valide (vérifié chez Stripe)."""
    if owner is None or not owner.stripe_account_id:
        raise HTTPException(403, STRIPE_REQUIRED_MESSAGE)
    try:
        sync_stripe_account(owner, db)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe account verification failed for user {owner.id}: {e}")
        raise HTTPException(502, "Could not verify your Stripe account. Please try again.")
    if not host_stripe_ready(owner):
        raise HTTPException(403, STRIPE_REQUIRED_MESSAGE)

# ─── Booking helpers ───────────────────────────────────────────

def check_date_overlap(db: Session, listing_id: int,
                       check_in: date, check_out: date,
                       exclude_booking_id: Optional[int] = None) -> bool:
    """Return True if any confirmed/pending booking overlaps the given dates,
    or if the host blocked at least one of the requested nights."""
    q = db.query(Booking).filter(
        Booking.listing_id == listing_id,
        Booking.status.in_(["pending", "paid"]),
        Booking.check_in < check_out,
        Booking.check_out > check_in,
    )
    if exclude_booking_id:
        q = q.filter(Booking.id != exclude_booking_id)
    if q.first() is not None:
        return True
    blocked = db.query(BlockedDate.id).filter(
        BlockedDate.listing_id == listing_id,
        BlockedDate.blocked_on >= check_in,
        BlockedDate.blocked_on < check_out,
    ).first()
    return blocked is not None

def calculate_booking_price(listing: Listing, check_in: date,
                            check_out: date, guests: int = 1) -> dict:
    """Prix = (prix/nuit × nuits × voyageurs) + ménage + service.

    Avec guests=1 le résultat est identique à l'ancien calcul.
    """
    nights = (check_out - check_in).days
    guests = max(1, int(guests or 1))
    price_per_night = Decimal(str(listing.price_per_night))
    cleaning_fee = Decimal(str(listing.cleaning_fee))
    service_fee = Decimal(str(listing.service_fee))
    subtotal = price_per_night * nights * guests
    total = subtotal + cleaning_fee + service_fee
    return {
        "nights": nights,
        "guests": guests,
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

# ─── Cancellation policy helpers ───────────────────────────────

def normalize_hhmm(value: Optional[str], default: str) -> str:
    """Retourne 'HH:MM' valide, sinon la valeur par défaut."""
    try:
        h, m = (value or default).strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except Exception:
        pass
    return default

def arrival_datetime(check_in: date, check_in_time: Optional[str]) -> datetime:
    """Date + heure d'arrivée du logement (heure locale du serveur, comme date.today())."""
    hhmm = normalize_hhmm(check_in_time, DEFAULT_CHECK_IN_TIME)
    h, m = hhmm.split(":")
    return datetime(check_in.year, check_in.month, check_in.day, int(h), int(m))

def cancellation_policy(arrival: datetime, now: datetime) -> tuple[int, float]:
    """Retourne (pourcentage remboursable, heures restantes avant l'arrivée).

    > 7 jours : 100 % · de 24 h à 7 jours (inclus) : 50 % · < 24 h : 0 %.
    """
    hours = (arrival - now).total_seconds() / 3600
    if hours > CANCEL_FULL_REFUND_HOURS:
        return 100, hours
    if hours >= CANCEL_PARTIAL_REFUND_HOURS:
        return 50, hours
    return 0, hours

def policy_refund_cents(total_cents: int, percent: int) -> int:
    """Montant dû au client selon la politique (arrondi inférieur au centime)."""
    return int((Decimal(total_cents) * Decimal(percent) / 100).quantize(
        Decimal("1"), rounding=ROUND_DOWN))

def refundable_cents(total_cents: int, percent: int, already_refunded_cents: int) -> int:
    """Reste à rembourser : montant dû selon la politique moins ce qui a déjà été remboursé,
    sans jamais dépasser ce qui reste remboursable sur le paiement."""
    entitled = policy_refund_cents(total_cents, percent)
    remaining = max(0, total_cents - already_refunded_cents)
    return max(0, min(entitled - already_refunded_cents, remaining))

def ledger_refunded_cents(db: Session, booking_id: int) -> int:
    """Somme des remboursements déjà enregistrés dans le ledger pour cette réservation."""
    total = db.query(func.coalesce(func.sum(PaymentTransaction.amount), 0)).filter(
        PaymentTransaction.booking_id == booking_id,
        PaymentTransaction.type == "refund",
    ).scalar() or 0
    return int((Decimal(str(total)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

# ─── Serialization ─────────────────────────────────────────────

def _iso(v):
    return v.isoformat() if v else None

def _flt(v):
    return float(v) if v is not None else None

def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role,
        "avatar_url": user.avatar_url,
        "language": getattr(user, "language", None) or "en",
        "stripe_ready": host_stripe_ready(user),
        "totp_enabled": bool(getattr(user, "totp_enabled", False)),
        "created_at": (
    user.created_at.isoformat()
    if hasattr(user.created_at, "isoformat")
    else user.created_at
    if user.created_at
    else None
),
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
        "latitude": float(listing.latitude) if listing.latitude is not None else None,
        "longitude": float(listing.longitude) if listing.longitude is not None else None,
        "price_per_night": float(listing.price_per_night),
        "cleaning_fee": float(listing.cleaning_fee),
        "service_fee": float(listing.service_fee),
        "max_guests": listing.max_guests,
        "bedrooms": listing.bedrooms,
        "beds": listing.beds,
        "bathrooms": listing.bathrooms,
        "property_type": listing.property_type,
        "status": listing.status,

        # Règles du logement
        "check_in_time": normalize_hhmm(listing.check_in_time, DEFAULT_CHECK_IN_TIME),
        "check_out_time": normalize_hhmm(listing.check_out_time, DEFAULT_CHECK_OUT_TIME),
        "pets_allowed": bool(listing.pets_allowed),
        "smoking_allowed": bool(listing.smoking_allowed),
        "parties_allowed": bool(listing.parties_allowed),
        "events_allowed": bool(listing.events_allowed),
        "children_allowed": True if listing.children_allowed is None else bool(listing.children_allowed),
        "arrival_instructions": listing.arrival_instructions,
        "additional_rules": listing.additional_rules,

        "created_at": (
            listing.created_at.isoformat()
            if hasattr(listing.created_at, "isoformat")
            else listing.created_at
            if listing.created_at
            else None
        ),

        "updated_at": (
            listing.updated_at.isoformat()
            if hasattr(listing.updated_at, "isoformat")
            else listing.updated_at
            if listing.updated_at
            else None
        ),

        "images": [
            {
                "id": img.id,
                "image_url": img.image_url,
                "position": img.position
            }
            for img in (listing.images or [])
        ],

        "amenities": [
            {
                "id": a.id,
                "name": a.name,
                "icon": a.icon
            }
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
        "platform_fee": float(booking.platform_fee or 0),
        "host_amount": float(booking.host_amount or 0),
        "currency": booking.currency,
        "status": booking.status,
        "stripe_payment_intent_id": booking.stripe_payment_intent_id,
        "stripe_checkout_session_id": booking.stripe_checkout_session_id,
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
    }
    # Avis éventuellement déjà laissé pour cette réservation (un seul par réservation)
    booking_reviews = booking.reviews or []
    data["has_review"] = len(booking_reviews) > 0
    data["review"] = {
        "id": booking_reviews[0].id,
        "rating": booking_reviews[0].rating,
        "comment": booking_reviews[0].comment,
    } if booking_reviews else None
    # Dernière demande d'annulation (résumé)
    requests_ = booking.cancellation_requests or []
    if requests_:
        last = max(requests_, key=lambda r: r.id)
        data["cancellation"] = {
            "id": last.id,
            "status": last.status,
            "refund_percent": last.refund_percent,
            "refundable_amount": float(last.refundable_amount or 0),
            "refunded_amount": _flt(last.refunded_amount),
        }
    else:
        data["cancellation"] = None
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

def serialize_cancellation(
    creq: CancellationRequest,
    booking: Optional[Booking] = None,
    listing: Optional[Listing] = None,
    guest: Optional[User] = None,
) -> dict:
    data = {
        "id": creq.id,
        "booking_id": creq.booking_id,
        "user_id": creq.user_id,
        "status": creq.status,
        "reason": creq.reason,
        "hours_before_arrival": float(creq.hours_before_arrival or 0),
        "refund_percent": creq.refund_percent,
        "total_paid": float(creq.total_paid or 0),
        "already_refunded": float(creq.already_refunded or 0),
        "refundable_amount": float(creq.refundable_amount or 0),
        "approved_amount": _flt(creq.approved_amount),
        "refunded_amount": _flt(creq.refunded_amount),
        "currency": creq.currency,
        "admin_note": creq.admin_note,
        "decided_at": _iso(creq.decided_at),
        "processed_at": _iso(creq.processed_at),
        "stripe_refund_id": creq.stripe_refund_id,
        "failure_reason": creq.failure_reason,
        "created_at": _iso(creq.created_at),
    }
    if booking:
        data["booking"] = {
            "id": booking.id,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "status": booking.status,
            "total": float(booking.total),
        }
    if listing:
        data["listing"] = {
            "id": listing.id, "title": listing.title,
            "city": listing.city, "country": listing.country,
        }
    if guest:
        data["guest"] = {
            "first_name": guest.first_name, "last_name": guest.last_name,
            "email": guest.email,
        }
    return data

def serialize_payment(tx: PaymentTransaction) -> dict:
    return {
        "id": tx.id,
        "booking_id": tx.booking_id,
        "host_id": tx.host_id,
        "type": tx.type,
        "amount": float(tx.amount or 0),
        "platform_fee": float(tx.platform_fee or 0),
        "host_amount": float(tx.host_amount or 0),
        "currency": tx.currency,
        "stripe_payment_intent_id": tx.stripe_payment_intent_id,
        "stripe_transfer_id": tx.stripe_transfer_id,
        "stripe_refund_id": tx.stripe_refund_id,
        "note": tx.note,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }

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

# ─── Chat / Notification serializers ───────────────────────────

def serialize_message(msg: Message) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "sender_id": msg.sender_id,
        "content": msg.content,
        "read_at": msg.read_at.isoformat() if msg.read_at else None,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "sender": {
            "id": msg.sender.id,
            "first_name": msg.sender.first_name,
            "last_name": msg.sender.last_name,
            "avatar_url": msg.sender.avatar_url,
        } if msg.sender else None,
    }

def serialize_conversation(conv: Conversation, current_user_id: int) -> dict:
    other = conv.host if conv.guest_id == current_user_id else conv.guest
    unread_count = sum(
        1 for m in (conv.messages or [])
        if m.sender_id != current_user_id and m.read_at is None
    )
    last_msg = conv.messages[-1] if conv.messages else None
    return {
        "id": conv.id,
        "guest_id": conv.guest_id,
        "host_id": conv.host_id,
        "other_user": {
            "id": other.id,
            "first_name": other.first_name,
            "last_name": other.last_name,
            "avatar_url": other.avatar_url,
        } if other else None,
        "unread_count": unread_count,
        "last_message": serialize_message(last_msg) if last_msg else None,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }

def serialize_notification(notif: Notification) -> dict:
    return {
        "id": notif.id,
        "type": notif.type,
        "title": notif.title,
        "body": notif.body,
        "link": notif.link,
        "read_at": notif.read_at.isoformat() if notif.read_at else None,
        "created_at": notif.created_at.isoformat() if notif.created_at else None,
    }

# ─── Chat helpers ──────────────────────────────────────────────

def get_or_create_conversation(db: Session, guest_id: int, host_id: int) -> Conversation:
    """Get existing conversation for a guest+host pair, or create one.

    The unique constraint on (guest_id, host_id) prevents duplicates at DB level.
    """
    if guest_id == host_id:
        raise HTTPException(400, "Cannot create conversation with yourself")
    conv = db.query(Conversation).filter(
        Conversation.guest_id == guest_id,
        Conversation.host_id == host_id,
    ).first()
    if conv:
        return conv
    conv = Conversation(guest_id=guest_id, host_id=host_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv

def user_in_conversation(user: User, conv: Conversation) -> bool:
    return user.id in (conv.guest_id, conv.host_id)

def create_notification(
    db: Session,
    user_id: int,
    ntype: str,
    title: str,
    body: Optional[str] = None,
    link: Optional[str] = None,
    dedup_key: Optional[str] = None,
) -> Optional[Notification]:
    """Create a notification if not a duplicate (dedup_key).

    Does NOT commit — caller commits.
    """
    if dedup_key:
        existing = db.query(Notification).filter(
            Notification.dedup_key == dedup_key
        ).first()
        if existing:
            return None
    notif = Notification(
        user_id=user_id,
        type=ntype,
        title=title,
        body=body,
        link=link,
        dedup_key=dedup_key,
    )
    db.add(notif)
    return notif
