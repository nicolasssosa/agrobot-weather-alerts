"""Casos de uso de usuarios. TODO(S2).

async def create_user(session: AsyncSession, data: UserCreate) -> User
    - phone_number duplicado -> ConflictError("User with this phone number already exists").
    - Commitea.

async def get_user(session: AsyncSession, user_id: int) -> User | None
"""
