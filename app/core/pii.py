"""Enmascarado de PII (teléfonos) en logs y en errores persistidos.

El teléfono del destinatario nunca se loguea ni se guarda completo (RNF-08, S6 SEC-1).
Módulo neutro: lo usan el dispatcher y los notifiers sin acoplarse entre sí.
"""

import re

#: Separadores que un proveedor puede intercalar entre los dígitos de un teléfono.
_SEPARATORS = r"[\s\-.()/]*"

#: Prefijo internacional, tal cual o URL-encoded (`%2B`), siempre opcional.
_INTERNATIONAL_PREFIX = r"(?:\+|%2b)"

#: Dígitos del número nacional significativo (código de área + abonado, sin país).
_NATIONAL_DIGITS = 10

#: Largos posibles del código de área dentro del número nacional argentino.
_AREA_CODE_LENGTHS = range(2, 5)


def mask_phone(phone_number: str) -> str:
    """Enmascara un teléfono dejando solo sus últimos 4 dígitos (ej. `***1234`)."""
    return "***" + phone_number[-4:]


def _digits_pattern(digits: str) -> str:
    """Patrón de una secuencia de dígitos con separadores tolerados *entre* ellos."""
    return _SEPARATORS.join(re.escape(digit) for digit in digits)


def _phone_patterns(digits: str) -> list[str]:
    """Patrones de todas las variantes con que un proveedor puede devolver el número.

    - E.164 completo, con o sin `+` (o `%2B`) y con cualquier separador.
    - Formato nacional móvil argentino (`0` + área + `15` + abonado, ej. `011 15-2233-4455`).
    - Número nacional significativo (últimos 10 dígitos): cubre el `wa_id` de Meta sin `+`
      ni el `9` móvil (`541122334455` para `+5491122334455`).
    """
    national = digits[-_NATIONAL_DIGITS:]
    patterns = [f"(?:{_INTERNATIONAL_PREFIX}{_SEPARATORS})?{_digits_pattern(digits)}"]
    if len(national) == _NATIONAL_DIGITS:
        for area_length in _AREA_CODE_LENGTHS:
            area, local = national[:area_length], national[area_length:]
            patterns.append(
                f"(?:0{_SEPARATORS})?"
                f"{_digits_pattern(area)}{_SEPARATORS}15{_SEPARATORS}{_digits_pattern(local)}"
            )
    if national != digits:
        patterns.append(_digits_pattern(national))
    return patterns


def mask_phone_in_text(text: str, phone_number: str) -> str:
    """Enmascara todas las variantes del teléfono presentes en `text`.

    Se arma el patrón con los dígitos del número configurado, tolerando un `+` (o su forma
    URL-encoded `%2B`) opcional y separadores —espacios, guiones, puntos, paréntesis y
    barras— entre dígitos. Además del número completo se cubren sus formas nacionales
    (últimos 10 dígitos y `0` + área + `15` + abonado), que son las que devuelven los
    proveedores (p. ej. el `wa_id` de Meta). Los patrones más específicos corren primero.
    """
    digits = "".join(char for char in phone_number if char.isdigit())
    if not digits:
        return text
    masked = mask_phone(phone_number)
    for pattern in _phone_patterns(digits):
        text = re.sub(pattern, masked, text, flags=re.IGNORECASE)
    return text
