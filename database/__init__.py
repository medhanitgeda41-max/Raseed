"""Database package exports."""
from database.connection import (
    get_db_connection,
    init_db,
    create_business,
    create_user,
    list_businesses,
    get_or_create_default_business,
    reset_db,
    save_chat_message,
    get_chat_history,
    add_memory_note,
    get_memory_notes,
    delete_memory_note,
    is_postgres_configured,
    set_tenant_context,
)

__all__ = [
    "get_db_connection",
    "init_db",
    "create_business",
    "create_user",
    "list_businesses",
    "get_or_create_default_business",
    "reset_db",
    "save_chat_message",
    "get_chat_history",
    "add_memory_note",
    "get_memory_notes",
    "delete_memory_note",
    "is_postgres_configured",
    "set_tenant_context",
]
