
import os
from sqlalchemy import text
from passlib.context import CryptContext
from db import engine

pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")
DEMO_PASSWORD = os.getenv("SEED_DEMO_PASSWORD", "host1234")


# --------------------------------------------------
# 0. DATABASE SCHEMA SYNC
# --------------------------------------------------

def sync_database_schema(conn):

    migrations = {
        "notifications": {
            "title": "VARCHAR(255) NULL",
            "body": "TEXT NULL",
            "read_at": "DATETIME NULL",
            "dedup_key": "VARCHAR(255) NULL",
        },
        "conversations": {
            "updated_at": "DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
        },
        "messages": {
            "content": "TEXT NULL",
        },
        "users": {
            "language": "VARCHAR(10) NULL DEFAULT 'fr'",
        },
    }

    database_name = conn.execute(
        text("SELECT DATABASE()")
    ).scalar()

    for table_name, columns in migrations.items():

        table_exists = conn.execute(text("""
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = :database_name
            AND TABLE_NAME = :table_name
        """), {
            "database_name": database_name,
            "table_name": table_name
        }).scalar()

        if not table_exists:
            print(f"Table {table_name} inexistante, migration ignorée.")
            continue

        for column_name, column_definition in columns.items():

            column_exists = conn.execute(text("""
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :database_name
                AND TABLE_NAME = :table_name
                AND COLUMN_NAME = :column_name
            """), {
                "database_name": database_name,
                "table_name": table_name,
                "column_name": column_name
            }).scalar()

            if not column_exists:
                conn.execute(text(
                    f"ALTER TABLE `{table_name}` "
                    f"ADD COLUMN `{column_name}` {column_definition}"
                ))

                print(f"Colonne ajoutée : {table_name}.{column_name}")

            else:
                print(f"Colonne déjà existante : {table_name}.{column_name}")

    print("Database schema synchronization completed!")


# --------------------------------------------------
# 1. AMENITIES
# --------------------------------------------------

AMENITIES = [
    ("Wifi", "wifi"),
    ("Pool", "waves"),
    ("Parking", "car"),
    ("Air conditioning", "snowflake"),
    ("Kitchen", "utensils"),
    ("Washing machine", "shirt"),
    ("Television", "tv"),
    ("Workspace", "briefcase"),
    ("Heating", "flame"),
    ("Hot tub", "bath"),
    ("Gym", "dumbbell"),
    ("Breakfast", "coffee"),
    ("Fireplace", "flame"),
    ("Sea view", "sailboat"),
    ("Garden", "trees"),
]

# --------------------------------------------------
# 2. USERS
# --------------------------------------------------

USERS = [
    ("Emma", "Laurent", "emma.laurent@book4me.com", "+33612345678", "host"),
    ("James", "Carter", "james.carter@gmail.com", "+12125551234", "host"),
    ("Sofia", "Russo", "sofia.russo@book4me.com", "+393331234567", "host"),
    ("Yuki", "Tanaka", "yuki.tanaka@gmail.com", "+819012345678", "host"),
    ("Liam", "O'Brien", "liam.obrien@book4me.com", "+353871234567", "host"),
    ("Lucas", "Moreau", "admin@book4me.com", "+33698765432", "admin"),
]

# --------------------------------------------------
# 3. LISTINGS
# --------------------------------------------------

LISTINGS = [
    (
        "emma.laurent@book4me.com",
        "Chic Parisian Loft Near Montmartre",
        "Stylish loft in the heart of Montmartre with panoramic city views. Recently renovated with a modern kitchen and elegant living space.",
        "France", "Paris", "12 Rue des Abbesses, 75018 Paris",
        48.8842, 2.3394, 145, 35, 20, 4, 2, 2, 1, "loft"
    ),
    (
        "emma.laurent@book4me.com",
        "Bright Studio Steps from the Seine",
        "A bright, modern studio apartment just minutes from the Seine. Perfect for couples exploring the city.",
        "France", "Paris", "8 Rue de Buci, 75006 Paris",
        48.8542, 2.3350, 98, 25, 15, 2, 1, 1, 1, "studio"
    ),
    (
        "james.carter@gmail.com",
        "Brooklyn Heights Luxury Apartment",
        "Spacious apartment with stunning Manhattan skyline views. Located in the historic Brooklyn Heights neighborhood.",
        "United States", "New York", "75 Hicks St, Brooklyn, NY 11201",
        40.6962, -73.9962, 210, 45, 25, 6, 3, 3, 2, "apartment"
    ),
    (
        "james.carter@gmail.com",
        "SoHo Designer Loft",
        "A beautifully designed loft in SoHo with exposed brick walls and high ceilings. Walk to the best galleries and restaurants.",
        "United States", "New York", "92 Greene St, New York, NY 10012",
        40.7222, -74.0036, 285, 50, 30, 4, 2, 2, 1, "loft"
    ),
    (
        "sofia.russo@book4me.com",
        "Tuscan Villa with Infinity Pool",
        "A stunning villa in the Tuscan hills with an infinity pool overlooking vineyards. The perfect Italian retreat.",
        "Italy", "Florence", "Via di Castello 15, 50041 Calenzano",
        43.8400, 11.1700, 320, 60, 35, 8, 4, 5, 3, "villa"
    ),
    (
        "sofia.russo@book4me.com",
        "Cozy Cabin in the Dolomites",
        "A warm and inviting cabin in the Dolomites. Perfect for a ski trip or a summer hiking adventure.",
        "Italy", "Cortina", "Località Pocol 5, 32043 Cortina d'Ampezzo",
        46.5167, 12.1167, 130, 30, 18, 6, 3, 4, 2, "cabin"
    ),
    (
        "yuki.tanaka@gmail.com",
        "Modern Tokyo Apartment in Shibuya",
        "Sleek, modern apartment in the heart of Shibuya. Walking distance to shops, restaurants, and the famous crossing.",
        "Japan", "Tokyo", "2-10-12 Shibuya, Shibuya-ku 150-0002",
        35.6612, 139.6995, 175, 30, 20, 4, 2, 2, 1, "apartment"
    ),
    (
        "yuki.tanaka@gmail.com",
        "Traditional Kyoto Townhouse",
        "A beautifully restored traditional Kyoto machiya. Experience the charm of old Japan with modern comforts.",
        "Japan", "Kyoto", "432 Furumatsucho, Nakagyo-ku 604-8005",
        35.0036, 135.7672, 155, 25, 18, 6, 3, 4, 2, "house"
    ),
    (
        "liam.obrien@book4me.com",
        "Cliffside Cottage in Galway",
        "A charming cottage on the cliffs of Galway with breathtaking ocean views. The perfect romantic getaway.",
        "Ireland", "Galway", "15 Salthill Road, Galway H91",
        53.2697, -9.0644, 120, 25, 15, 4, 2, 2, 1, "cottage"
    ),
    (
        "liam.obrien@book4me.com",
        "Dublin Georgian Townhouse",
        "An elegant Georgian townhouse in Dublin's city center. Beautifully decorated with period features and modern amenities.",
        "Ireland", "Dublin", "42 Fitzwilliam Square South, Dublin 2",
        53.3350, -6.2580, 190, 40, 22, 8, 4, 5, 3, "house"
    ),
]

# --------------------------------------------------
# 4. LISTING IMAGES
# --------------------------------------------------

IMAGE_IDS = [
    [8135492, 6434592, 7195739, 6957081, 13722886],
    [6585598, 6782473, 4740488, 6076850],
    [7167073, 8089172, 7745929, 17000989, 7166942],
    [13722886, 7546323, 8089081, 6538943],
    [2476632, 1488327, 14025040, 261101, 14024788],
    [10988050, 7746560, 14295344, 13221462],
    [7173666, 6920439, 6434612, 8082195],
    [7174113, 8251591, 19836844, 6903210],
    [20397881, 28873269, 10882330, 16681604],
    [7195739, 6265836, 7031840, 7511693],
]


def get_image_url(image_id):
    extension = "png" if image_id == 1488327 else "jpeg"
    return (
        f"https://images.pexels.com/photos/{image_id}/"
        f"pexels-photo-{image_id}.{extension}"
        "?auto=compress&cs=tinysrgb&h=650&w=940"
    )


# --------------------------------------------------
# 5. LISTING AMENITIES
# --------------------------------------------------

LISTING_AMENITIES = {
    1: [1, 4, 5, 6, 7, 8, 9],
    2: [1, 4, 5, 7, 9],
    3: [1, 3, 4, 5, 6, 7, 8, 9],
    4: [1, 4, 5, 7, 8, 9, 13],
    5: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 15],
    6: [1, 4, 5, 6, 7, 9, 13],
    7: [1, 3, 4, 5, 6, 7, 8, 9],
    8: [1, 4, 5, 6, 7, 9, 15],
    9: [1, 4, 5, 6, 9, 13, 14],
    10: [1, 3, 4, 5, 6, 7, 8, 9, 13],
}


# --------------------------------------------------
# 6. INSERT EVERYTHING
# --------------------------------------------------

def _seed_database_once():

    with engine.begin() as conn:

        # DATABASE SCHEMA SYNC
        sync_database_schema(conn)

        # AMENITIES
        for name, icon in AMENITIES:
            conn.execute(text("""
                INSERT INTO amenities (name, icon)
                VALUES (:name, :icon)
                ON DUPLICATE KEY UPDATE icon = VALUES(icon)
            """), {
                "name": name,
                "icon": icon
            })

        amenity_ids = {
            row.name: row.id
            for row in conn.execute(
                text("SELECT id, name FROM amenities")
            )
        }

        # USERS
        password_hash = pwd_context.hash(DEMO_PASSWORD)

        for first, last, email, phone, role in USERS:

            existing = conn.execute(text("""
                SELECT id, password_hash
                FROM users
                WHERE email = :email
            """), {
                "email": email
            }).first()

            if existing is None:
                conn.execute(text("""
                    INSERT INTO users
                    (first_name, last_name, email, password_hash, phone, role)
                    VALUES
                    (:first, :last, :email, :password, :phone, :role)
                """), {
                    "first": first,
                    "last": last,
                    "email": email,
                    "password": password_hash,
                    "phone": phone,
                    "role": role
                })

            elif "placeholder_replaced_at_runtime" in existing.password_hash:
                conn.execute(text("""
                    UPDATE users
                    SET password_hash = :password
                    WHERE email = :email
                """), {
                    "password": password_hash,
                    "email": email
                })

        user_ids = {
            row.email: row.id
            for row in conn.execute(
                text("SELECT id, email FROM users")
            )
        }

        # LISTINGS
        listing_ids = {}

        for index, item in enumerate(LISTINGS, start=1):

            (
                owner_email, title, description, country, city,
                address, lat, lng, price, cleaning, service,
                guests, bedrooms, beds, bathrooms, property_type
            ) = item

            owner_id = user_ids[owner_email]

            existing = conn.execute(text("""
                SELECT id FROM listings
                WHERE owner_id = :owner_id AND title = :title
                LIMIT 1
            """), {
                "owner_id": owner_id,
                "title": title
            }).first()

            if existing:
                listing_id = existing.id
            else:
                result = conn.execute(text("""
                    INSERT INTO listings (
                        owner_id, title, description, country, city,
                        address, latitude, longitude, price_per_night,
                        cleaning_fee, service_fee, max_guests,
                        bedrooms, beds, bathrooms, property_type, status
                    ) VALUES (
                        :owner_id, :title, :description, :country, :city,
                        :address, :lat, :lng, :price,
                        :cleaning, :service, :guests,
                        :bedrooms, :beds, :bathrooms, :property_type,
                        'published'
                    )
                """), {
                    "owner_id": owner_id,
                    "title": title,
                    "description": description,
                    "country": country,
                    "city": city,
                    "address": address,
                    "lat": lat,
                    "lng": lng,
                    "price": price,
                    "cleaning": cleaning,
                    "service": service,
                    "guests": guests,
                    "bedrooms": bedrooms,
                    "beds": beds,
                    "bathrooms": bathrooms,
                    "property_type": property_type
                })

                listing_id = result.lastrowid

            listing_ids[index] = listing_id

        # IMAGES
        for index, image_ids in enumerate(IMAGE_IDS, start=1):

            listing_id = listing_ids[index]

            for position, image_id in enumerate(image_ids):

                url = get_image_url(image_id)

                conn.execute(text("""
                    INSERT INTO listing_images
                    (listing_id, image_url, position)
                    SELECT :listing_id, :url, :position
                    WHERE NOT EXISTS (
                        SELECT 1 FROM listing_images
                        WHERE listing_id = :listing_id
                        AND image_url = :url
                    )
                """), {
                    "listing_id": listing_id,
                    "url": url,
                    "position": position
                })

        # LISTING AMENITIES
        amenity_names_by_id = {
            index: name
            for index, (name, icon) in enumerate(AMENITIES, start=1)
        }

        for listing_index, amenity_indexes in LISTING_AMENITIES.items():

            listing_id = listing_ids[listing_index]

            for amenity_index in amenity_indexes:

                name = amenity_names_by_id[amenity_index]
                amenity_id = amenity_ids[name]

                conn.execute(text("""
                    INSERT INTO listing_amenities
                    (listing_id, amenity_id)
                    SELECT :listing_id, :amenity_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM listing_amenities
                        WHERE listing_id = :listing_id
                        AND amenity_id = :amenity_id
                    )
                """), {
                    "listing_id": listing_id,
                    "amenity_id": amenity_id
                })

        print("StayHaven seed completed successfully!")
        print("Users:", len(USERS))
        print("Listings:", len(LISTINGS))
        print("Amenities:", len(AMENITIES))
        print("Images:", sum(len(x) for x in IMAGE_IDS))


def seed_database(max_attempts=5):
    """Exécute le seed avec un verrou MySQL global (un seul seed à la fois,
    même si deux déploiements se chevauchent) et réessaie en cas de
    lock wait timeout (1205) ou de deadlock (1213)."""
    import time
    from sqlalchemy.exc import OperationalError

    for attempt in range(1, max_attempts + 1):
        lock_conn = engine.connect()
        try:
            got = lock_conn.execute(
                text("SELECT GET_LOCK('stayhaven_seed', 120)")
            ).scalar()
            if not got:
                raise RuntimeError("Impossible d'obtenir le verrou de seed (120s)")
            _seed_database_once()
            return
        except OperationalError as e:
            code = e.orig.args[0] if getattr(e, "orig", None) and e.orig.args else None
            if code in (1205, 1213) and attempt < max_attempts:
                print(f"Seed: erreur MySQL {code}, nouvelle tentative {attempt}/{max_attempts}...")
                time.sleep(5 * attempt)
                continue
            raise
        finally:
            try:
                lock_conn.execute(text("SELECT RELEASE_LOCK('stayhaven_seed')"))
            except Exception:
                pass
            lock_conn.close()


if __name__ == "__main__":
    seed_database()
