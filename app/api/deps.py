"""Dependencies compartidas de la API.

TODO(S2): implementar
    SessionDep = Annotated[AsyncSession, Depends(get_session)]

    async def get_current_user(
        session: SessionDep,
        x_user_id: Annotated[int | None, Header(alias="X-User-Id")] = None,
    ) -> User
        - Header ausente o usuario inexistente -> HTTPException 401
          {"detail": "Missing or invalid X-User-Id header"}.
        - Ver docs/API.md §Autenticación (simulada).

    CurrentUser = Annotated[User, Depends(get_current_user)]
"""
