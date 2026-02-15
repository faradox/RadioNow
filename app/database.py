import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure

logger = logging.getLogger("radionow.database")

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/")
DATABASE_NAME = "radionow"


class Database:
    client: AsyncIOMotorClient = None
    db = None

    @classmethod
    async def connect(cls):
        """Connect to MongoDB."""
        try:
            cls.client = AsyncIOMotorClient(MONGO_URL)
            # Verify connection
            await cls.client.admin.command("ping")
            cls.db = cls.client[DATABASE_NAME]
            logger.info("Connected to MongoDB at %s", MONGO_URL.split("@")[-1])

            # Create indexes
            await cls.create_indexes()
        except ConnectionFailure as e:
            logger.error("Failed to connect to MongoDB: %s", e)
            raise

    @classmethod
    async def create_indexes(cls):
        """Create database indexes."""
        # Voices collection indexes
        await cls.db.voices.create_index("slug", unique=True)
        await cls.db.voices.create_index("created_at")

        # Broadcasts collection indexes
        await cls.db.broadcasts.create_index("created_at")
        await cls.db.broadcasts.create_index("voice_id")

        logger.info("Database indexes created")

    @classmethod
    async def disconnect(cls):
        """Disconnect from MongoDB."""
        if cls.client:
            cls.client.close()
            logger.info("Disconnected from MongoDB")

    @classmethod
    def get_db(cls):
        """Get database instance."""
        return cls.db


# Convenience accessors
def get_voices_collection():
    return Database.get_db().voices


def get_broadcasts_collection():
    return Database.get_db().broadcasts
